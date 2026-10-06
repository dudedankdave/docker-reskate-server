package main

// Discord webhook sidecar: forwards the server console (the lines the server also writes
// to /data/ReSkateServer.log) as batched code blocks, and announces new ReSkate releases
// once, mentioning DISCORD_MENTION_IDS. Only the update message can ping anyone.
// Nothing here may disturb the server: failures go to /data/DiscordWebhook.log.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

const (
	logPath        = "/data/ReSkateServer.log"
	statePath      = "/data/.discord-update-notified"
	notesPath      = "/data/DiscordWebhook.log"
	modsState      = "/data/.discord-mods-notified"
	repo           = "Dingo-Shenanigans/ReSkate"
	hub            = "dudedankdave/reskate-server"
	checkEvery     = 3 * time.Hour
	modsCheckEvery = time.Hour // Thunderstore mods are checked hourly
	flushAfter     = 3 * time.Second
	maxPending     = 300
)

var (
	dateRe    = regexp.MustCompile(`^\[\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})\]`)
	badNameRe = regexp.MustCompile(`(?i)\S*(discord|clyde)\S*`)
	spacesRe  = regexp.MustCompile(`\s+`)
	trailRe   = regexp.MustCompile(`(\s*\|\s*)+$`)
	versionRe = regexp.MustCompile(`^\d+(\.\d+)+$`)
	digitsRe  = regexp.MustCompile(`\d+`)
	client    = &http.Client{Timeout: 20 * time.Second}
)

func note(format string, a ...any) {
	if st, err := os.Stat(notesPath); err == nil && st.Size() > 50_000 {
		_ = os.Truncate(notesPath, 0)
	}
	f, err := os.OpenFile(notesPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "[%s] %s\n", time.Now().Format("2006-01-02 15:04:05"), fmt.Sprintf(format, a...))
}

func trunc(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

func post(url string, payload map[string]any) bool {
	data, _ := json.Marshal(payload)
	for i := 0; i < 4; i++ {
		resp, err := client.Post(url, "application/json", bytes.NewReader(data))
		if err != nil {
			note("webhook post failed: %v", err)
			return false
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return true
		}
		if resp.StatusCode == 429 {
			wait := 2.0
			var r struct {
				RetryAfter float64 `json:"retry_after"`
			}
			if json.Unmarshal(body, &r) == nil && r.RetryAfter > 0 {
				wait = r.RetryAfter
			}
			time.Sleep(time.Duration(min(max(wait, 1), 30) * float64(time.Second)))
			continue
		}
		note("webhook post failed: HTTP %d %s", resp.StatusCode, trunc(string(body), 200))
		return false
	}
	note("webhook post failed: still rate limited after retries")
	return false
}

func username() string {
	name := os.Getenv("DISCORD_USERNAME")
	if name == "" {
		name = os.Getenv("SERVER_NAME")
	}
	// Discord rejects webhook names containing these words (server names often hold a discord.gg link)
	name = badNameRe.ReplaceAllString(name, "")
	name = spacesRe.ReplaceAllString(name, " ")
	name = trailRe.ReplaceAllString(name, "")
	name = strings.Trim(name, " |-")
	if name == "" {
		name = "ReSkate server"
	}
	return trunc(name, 80)
}

func sendConsole(url, name string, lines []string) {
	var chunk []string
	size := 0
	flush := func() {
		if len(chunk) > 0 {
			post(url, map[string]any{"username": name, "content": "```\n" + strings.Join(chunk, "\n") + "\n```",
				"allowed_mentions": map[string]any{"parse": []string{}}})
		}
	}
	for _, line := range lines {
		n := utf8.RuneCountInString(line) + 1
		if size+n <= 1850 {
			chunk = append(chunk, line)
			size += n
			continue
		}
		flush()
		chunk, size = []string{line}, n
	}
	flush()
}

func clean(line string) string {
	line = strings.TrimRight(line, "\r")
	line = dateRe.ReplaceAllString(line, "[$1]")
	return trunc(strings.ReplaceAll(line, "```", "'''"), 500)
}

type tailState struct {
	ino uint64
	pos int64
}

var (
	joinedRe = regexp.MustCompile(`^(.+) joined \(\d+(?:, admin)?\), (\d+/\d+) players, loaded in \d+ s$`)
	leftRe   = regexp.MustCompile(`^(.+) left \(.*\)$`)
	upRe     = regexp.MustCompile(`^.+ is up on .+ for \d+ players\.$`)
	taggedRe = regexp.MustCompile(`^\[(?:chat|admin|join|objects)\] `)
	lineRe   = regexp.MustCompile(`^\[(\d\d:\d\d:\d\d)\] (.*)$`)
)

// userLine is the player-facing version of a cleaned console line, or "" when it is admin-only:
// joins and leaves (no Steam IDs, no leave reasons), throwdowns and the server-up line.
func userLine(line string) string {
	m := lineRe.FindStringSubmatch(line)
	if m == nil {
		return ""
	}
	stamp, text := m[1], m[2]
	if taggedRe.MatchString(text) {
		return ""
	}
	if strings.HasPrefix(text, "[throwdown] ") || upRe.MatchString(text) {
		return line
	}
	if j := joinedRe.FindStringSubmatch(text); j != nil {
		return fmt.Sprintf("[%s] %s joined, %s players", stamp, j[1], j[2])
	}
	if l := leftRe.FindStringSubmatch(text); l != nil {
		return fmt.Sprintf("[%s] %s left", stamp, l[1])
	}
	return ""
}

// feed batches console lines for one webhook; convert filters/rewrites them (nil = every line).
type feed struct {
	url, name string
	convert   func(string) string
	pending   []string
	first     time.Time
}

func (f *feed) add(lines []string) {
	if f.convert != nil {
		var out []string
		for _, l := range lines {
			if c := f.convert(l); c != "" {
				out = append(out, c)
			}
		}
		lines = out
	}
	if len(lines) > 0 && len(f.pending) == 0 {
		f.first = time.Now()
	}
	f.pending = append(f.pending, lines...)
	if len(f.pending) > maxPending {
		skipped := len(f.pending) - maxPending
		f.pending = append([]string{fmt.Sprintf("... %d lines skipped", skipped)}, f.pending[skipped:]...)
	}
}

func (f *feed) flushIfDue() {
	total := 0
	for _, l := range f.pending {
		total += len(l)
	}
	if len(f.pending) > 0 && (time.Since(f.first) >= flushAfter || total > 1700) {
		batch := f.pending
		f.pending = nil
		sendConsole(f.url, f.name, batch)
	}
}

func readNew(s *tailState) []string {
	st, err := os.Stat(logPath)
	if err != nil {
		return nil
	}
	ino := st.Sys().(*syscall.Stat_t).Ino
	if ino != s.ino || st.Size() < s.pos {
		s.ino, s.pos = ino, 0 // log was replaced or truncated
	}
	if st.Size() == s.pos {
		return nil
	}
	f, err := os.Open(logPath)
	if err != nil {
		return nil
	}
	defer f.Close()
	data := make([]byte, st.Size()-s.pos)
	n, _ := f.ReadAt(data, s.pos)
	data = data[:n]
	end := bytes.LastIndexByte(data, '\n')
	if end < 0 {
		return nil // wait for the rest of the line
	}
	s.pos += int64(end + 1)
	var out []string
	for _, line := range strings.Split(string(data[:end]), "\n") {
		if strings.TrimSpace(line) != "" {
			out = append(out, clean(line))
		}
	}
	return out
}

func consoleLoop(feeds []*feed) {
	s := &tailState{}
	if st, err := os.Stat(logPath); err == nil { // only what the server writes from now on
		s.ino, s.pos = st.Sys().(*syscall.Stat_t).Ino, st.Size()
	}
	for {
		nw := readNew(s)
		for _, f := range feeds {
			f.add(nw)
			f.flushIfDue()
		}
		time.Sleep(time.Second)
	}
}

func numbers(v string) []int {
	var out []int
	for _, m := range digitsRe.FindAllString(v, 4) {
		n, _ := strconv.Atoi(m)
		out = append(out, n)
	}
	return out
}

// newer reports whether version a is greater than b (tuple comparison, like Python).
func newer(a, b string) bool {
	x, y := numbers(a), numbers(b)
	for i := 0; i < len(x) && i < len(y); i++ {
		if x[i] != y[i] {
			return x[i] > y[i]
		}
	}
	return len(x) > len(y)
}

type notified struct {
	Version   string `json:"version"`
	Published bool   `json:"published"`
}

func checkOnce(url, name string, mentions []string, running string) {
	req, _ := http.NewRequest("GET", "https://api.github.com/repos/"+repo+"/releases/latest", nil)
	req.Header.Set("User-Agent", "reskate-server-image")
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := client.Do(req)
	if err != nil {
		note("release check failed: %v", err)
		return
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	if resp.StatusCode != 200 {
		note("release check failed: HTTP %d %s", resp.StatusCode, trunc(string(body), 200))
		return
	}
	var rel struct {
		Tag string `json:"tag_name"`
	}
	if json.Unmarshal(body, &rel) != nil || rel.Tag == "" {
		note("release check failed: unreadable response")
		return
	}
	latest := strings.TrimPrefix(rel.Tag, "v")
	if !newer(latest, running) {
		return
	}
	published := false
	if r, err := client.Get("https://hub.docker.com/v2/repositories/" + hub + "/tags/" + latest); err == nil {
		r.Body.Close()
		published = r.StatusCode == 200
	}
	var st notified
	if raw, err := os.ReadFile(statePath); err == nil {
		_ = json.Unmarshal(raw, &st)
	}
	if st.Version == latest && (st.Published || !published) {
		return // already announced, nothing new to say
	}
	var tags []string
	for _, id := range mentions {
		tags = append(tags, "<@"+id+">")
	}
	var how string
	if published {
		how = "Update with `docker compose pull && docker compose up -d`. The restart kicks everyone and changes the join code."
	} else {
		how = fmt.Sprintf("`%s:%s` is not on Docker Hub yet, the image has to be built first.", hub, latest)
	}
	var text string
	if st.Version == latest { // earlier message said "not published yet"
		text = fmt.Sprintf("%s **Image available**: `%s:%s` is on Docker Hub now. %s", strings.Join(tags, " "), hub, latest, how)
	} else {
		text = fmt.Sprintf("%s **UPDATE AVAILABLE**: ReSkate **%s** is out, this server runs **%s**. Players on the new version can't join until the server is updated.\n%s\nhttps://github.com/%s/releases/tag/v%s",
			strings.Join(tags, " "), latest, running, how, repo, latest)
	}
	users := mentions
	if users == nil {
		users = []string{}
	}
	if post(url, map[string]any{"username": name, "content": strings.TrimSpace(text),
		"allowed_mentions": map[string]any{"parse": []string{}, "users": users}}) {
		raw, _ := json.Marshal(notified{Version: latest, Published: published})
		_ = os.WriteFile(statePath, raw, 0o644)
	}
}

func postModEvents(url, name string) {
	raw, err := os.ReadFile(modsEvents)
	if err != nil {
		return
	}
	_ = os.Remove(modsEvents)
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var e struct {
			Owner, Name, From, To string
			Maps                  []string
		}
		if json.Unmarshal([]byte(line), &e) != nil {
			continue
		}
		label := e.Owner + "-" + e.Name
		maps := ""
		if len(e.Maps) > 0 {
			maps = " Maps: " + strings.Join(e.Maps, ", ") + "."
		}
		text := fmt.Sprintf("**MOD INSTALLED**: %s **%s**.%s", label, e.To, maps)
		if e.From != "" {
			text = fmt.Sprintf("**MOD UPDATED**: %s %s to **%s**.%s", label, e.From, e.To, maps)
		}
		post(url, map[string]any{"username": name, "content": text,
			"allowed_mentions": map[string]any{"parse": []string{}}})
	}
}

func checkMods(url, name string, mentions []string) {
	update := true
	if v := strings.ToLower(strings.TrimSpace(os.Getenv("MODS_UPDATE"))); v == "0" || v == "false" || v == "no" || v == "off" {
		update = false
	}
	seen := map[string]string{}
	if raw, err := os.ReadFile(modsState); err == nil {
		_ = json.Unmarshal(raw, &seen)
	}
	var tags []string
	for _, id := range mentions {
		tags = append(tags, "<@"+id+">")
	}
	users := mentions
	if users == nil {
		users = []string{}
	}
	for _, u := range pendingModUpdates(os.Getenv("MODS")) {
		key := u.owner + "-" + u.name
		if seen[key] == u.latest {
			continue
		}
		how := "Restart the server to install it. The restart kicks everyone and changes the join code."
		if !update {
			how = "`MODS_UPDATE` is false, so it will not be installed automatically."
		}
		text := fmt.Sprintf("%s **MOD UPDATE AVAILABLE**: %s **%s** is out, this server has **%s**. %s\nhttps://thunderstore.io/c/reskate/p/%s/%s/",
			strings.Join(tags, " "), key, u.latest, u.have, how, u.owner, u.name)
		if post(url, map[string]any{"username": name, "content": strings.TrimSpace(text),
			"allowed_mentions": map[string]any{"parse": []string{}, "users": users}}) {
			seen[key] = u.latest
			raw, _ := json.Marshal(seen)
			_ = os.WriteFile(modsState, raw, 0o644)
		}
	}
}

func modsLoop(url, name string, mentions []string) {
	time.Sleep(5 * time.Second)
	postModEvents(url, name)
	time.Sleep(85 * time.Second)
	for {
		checkMods(url, name, mentions)
		time.Sleep(modsCheckEvery)
	}
}

func updateLoop(url, name string, mentions []string, running string) {
	time.Sleep(time.Minute)
	for {
		checkOnce(url, name, mentions, running)
		time.Sleep(checkEvery)
	}
}

func notifierMain() {
	// DISCORD_WEBHOOK_ADMIN (DISCORD_WEBHOOK is its older name): everything, plus the update messages.
	// DISCORD_WEBHOOK_USER: the player-facing lines only.
	admin := strings.TrimSpace(os.Getenv("DISCORD_WEBHOOK_ADMIN"))
	if admin == "" {
		admin = strings.TrimSpace(os.Getenv("DISCORD_WEBHOOK"))
	}
	user := strings.TrimSpace(os.Getenv("DISCORD_WEBHOOK_USER"))
	if admin == "" && user == "" {
		return
	}
	name := username()
	var mentions []string
	for _, id := range strings.Split(os.Getenv("DISCORD_MENTION_IDS"), ",") {
		if id = strings.TrimSpace(id); id != "" {
			mentions = append(mentions, id)
		}
	}
	running := os.Getenv("RESKATE_IMAGE_VERSION")
	var feeds []*feed
	if admin != "" {
		if strings.TrimSpace(os.Getenv("MODS")) != "" {
			go modsLoop(admin, name, mentions)
		}
		if versionRe.MatchString(running) {
			go updateLoop(admin, name, mentions, running)
		} else {
			note("update check off: image version %q is not a release number", running)
		}
		switch strings.ToLower(strings.TrimSpace(os.Getenv("DISCORD_CONSOLE"))) {
		case "", "1", "true", "yes", "on":
			feeds = append(feeds, &feed{url: admin, name: name})
		}
	}
	if user != "" {
		feeds = append(feeds, &feed{url: user, name: name, convert: userLine})
	}
	if len(feeds) > 0 {
		consoleLoop(feeds)
	}
	select {}
}
