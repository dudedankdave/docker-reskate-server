package main

// Discord feeds and the ReSkate update policy (as notifier.py), running as goroutines next to
// the supervisor. Nothing here may disturb the server: failures go to /data/DiscordWebhook.log.
//
//	DISCORD_WEBHOOK_ESSENTIALS  server up + join code, problems, crashes, releases, approvals, mods (pings)
//	DISCORD_WEBHOOK_LOG         the whole console (with the chat, as before)
//	DISCORD_WEBHOOK_PUBLIC      joins, leaves, throwdowns, server up, update countdowns
//	DISCORD_WEBHOOK_CHAT        in-game chat only
//
// Older names: DISCORD_WEBHOOK_ADMIN / DISCORD_WEBHOOK = essentials + log + chat (DISCORD_CONSOLE=false:
// no log or chat), DISCORD_WEBHOOK_USER = public. The log scope itself never has the in-game chat:
// that is the chat scope. The updates scope gets news once per version (a new ReSkate release, its
// image on Docker Hub), updates-admin this server's update messages (else they go to essentials).
// UPDATE_POLICY instant|timed|scheduled|ask, see notifier.py.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"
)

const (
	logPath        = "/data/ReSkateServer.log"
	notesPath      = "/data/DiscordWebhook.log"
	modsState      = "/data/.discord-mods-notified"
	repo           = "Dingo-Shenanigans/ReSkate"
	hub            = "dudedankdave/reskate-server"
	discordAPI     = "https://discord.com/api/v10"
	yesEmoji       = "\u2705"
	noEmoji        = "\u274c"
	modsCheckEvery = time.Hour // Thunderstore mods are checked hourly
	flushAfter     = 3 * time.Second
	maxPending     = 300
)

var (
	updateState = "/data/.update-state.json"
	pinnedState = "/data/.discord-update-notified"
	newsState   = "/data/.discord-updates-notified"
	tailPath    = logPath
	dateRe      = regexp.MustCompile(`^\[\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})\]`)
	badNameRe   = regexp.MustCompile(`(?i)\S*(discord|clyde)\S*`)
	spacesRe    = regexp.MustCompile(`\s+`)
	trailRe     = regexp.MustCompile(`(\s*\|\s*)+$`)
	versionRe   = regexp.MustCompile(`^\d+(\.\d+)+$`)
	digitsRe    = regexp.MustCompile(`\d+`)
	scheduleRe  = regexp.MustCompile(`^(?:([a-z]{3}(?:,[a-z]{3})*) )?(\d{1,2}):(\d{2})$`)
	essentialRe = regexp.MustCompile(`(?i)\bcode\b|Config problem|\bWARNING\b|\bERROR\b|\bfailed\b`)
	// Console commands whose answer is posted to the log feed as its own titled block.
	infoCommands = map[string]bool{"help": true, "status": true, "players": true, "reserved": true, "net": true,
		"bans": true, "maps": true, "map-pool": true, "rotation": true, "votes": true, "parties": true,
		"score-check": true, "score-allow": true, "announcements": true, "objects": true, "admin": true}
	client = &http.Client{Timeout: 20 * time.Second}
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

func truthy(v string, def bool) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "":
		return def
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// request sends JSON (payload may be nil) with Discord's rate limits honoured; the reply is
// unmarshalled into out when given. False on failure.
func request(method, u string, payload any, headers []string, out any) bool {
	var data []byte
	if payload != nil {
		data, _ = json.Marshal(payload)
	}
	for i := 0; i < 4; i++ {
		req, _ := http.NewRequest(method, u, bytes.NewReader(data))
		if payload != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		for _, h := range headers {
			k, v, _ := strings.Cut(h, ": ")
			req.Header.Set(k, v)
		}
		resp, err := client.Do(req)
		if err != nil {
			note("%s failed: %v", method, err)
			return false
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			if out != nil && len(bytes.TrimSpace(body)) > 0 {
				_ = json.Unmarshal(body, out)
			}
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
		path, _, _ := strings.Cut(u, "?")
		note("%s %s failed: HTTP %d %s", method, trunc(path, 60), resp.StatusCode, trunc(string(body), 200))
		return false
	}
	note("still rate limited after retries")
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

// hook is one feed; it posts to every webhook URL given for its scope (url is the first, more the rest).
type hook struct {
	url, name string
	mentions  []string
	more      []string
}

func newHook(urls []string, name string, mentions []string) *hook {
	h := &hook{name: name, mentions: mentions}
	if len(urls) > 0 {
		h.url, h.more = urls[0], urls[1:]
	}
	return h
}

// webhookScopes maps WEBHOOK_SCOPE_<n> names to feeds.
var (
	webhookScopes = map[string]string{"admin": "essentials", "essentials": "essentials", "log": "log",
		"console": "log", "public": "public", "chat": "chat", "leaderboard": "leaderboard",
		"updates": "updates", "updates-admin": "updates-admin"}
	webhookURLRe = regexp.MustCompile(`^WEBHOOK_URL_(.+)$`)
)

// webhooks: feed -> URLs, from WEBHOOK_URL_<n>/WEBHOOK_SCOPE_<n> (any number, <n> any name, scopes
// comma-separated) and the older DISCORD_WEBHOOK_* names.
func webhooks() map[string][]string {
	get := func(name string) string { return strings.TrimSpace(os.Getenv(name)) }
	or := func(a, b string) string {
		if a != "" {
			return a
		}
		return b
	}
	legacy := or(get("DISCORD_WEBHOOK_ADMIN"), get("DISCORD_WEBHOOK"))
	logURL := get("DISCORD_WEBHOOK_LOG")
	if logURL == "" && truthy(os.Getenv("DISCORD_CONSOLE"), true) {
		logURL = legacy
	}
	hooks := map[string][]string{
		"essentials":    {or(get("DISCORD_WEBHOOK_ESSENTIALS"), legacy)},
		"log":           {logURL},
		"public":        {or(get("DISCORD_WEBHOOK_PUBLIC"), get("DISCORD_WEBHOOK_USER"))},
		"chat":          {get("DISCORD_WEBHOOK_CHAT"), logURL}, // the older log names keep their chat
		"leaderboard":   {get("LEADERBOARD_WEBHOOK")},
		"updates":       {},
		"updates-admin": {},
	}
	var keys []string
	for _, kv := range os.Environ() {
		keys = append(keys, strings.SplitN(kv, "=", 2)[0])
	}
	sort.Strings(keys)
	for _, key := range keys {
		m := webhookURLRe.FindStringSubmatch(key)
		if m == nil || get(key) == "" {
			continue
		}
		for _, scope := range strings.Split(strings.ToLower(get("WEBHOOK_SCOPE_"+m[1])), ",") {
			if feed, ok := webhookScopes[strings.TrimSpace(scope)]; ok {
				hooks[feed] = append(hooks[feed], get(key))
			}
		}
	}
	for feed, urls := range hooks {
		var out []string
		seen := map[string]bool{}
		for _, u := range urls {
			if u != "" && !seen[u] {
				seen[u] = true
				out = append(out, u)
			}
		}
		hooks[feed] = out
	}
	return hooks
}

func (h *hook) send(text string, ping bool) bool {
	if h.url == "" {
		return true
	}
	users := []string{}
	if ping && len(h.mentions) > 0 {
		users = h.mentions
		var tags []string
		for _, id := range users {
			tags = append(tags, "<@"+id+">")
		}
		text = strings.Join(tags, " ") + " " + text
	}
	ok := true
	for _, u := range append([]string{h.url}, h.more...) {
		ok = request("POST", u, map[string]any{"username": h.name, "content": trunc(text, 2000),
			"allowed_mentions": map[string]any{"parse": []string{}, "users": users}}, nil, nil) && ok
	}
	return ok
}

func clean(line string) string {
	line = strings.TrimRight(line, "\r")
	line = dateRe.ReplaceAllString(line, "[$1]")
	return trunc(strings.ReplaceAll(line, "```", "'''"), 500)
}

var (
	joinedRe = regexp.MustCompile(`^(.+) joined \(\d+(?:, admin)?\), (\d+/\d+) players, loaded in \d+ s$`)
	leftRe   = regexp.MustCompile(`^(.+?) left \(.*\)(?: \[.*\])?$`) // "Name left (reason) [connection details]"
	upRe     = regexp.MustCompile(`^.+ is up on .+ for \d+ players\.$`)
	taggedRe = regexp.MustCompile(`^\[(?:chat|admin|join|objects)\] `)
	lineRe   = regexp.MustCompile(`^\[(\d\d:\d\d:\d\d)\] (.*)$`)
)

func lineText(line string) string {
	if m := lineRe.FindStringSubmatch(line); m != nil {
		return m[2]
	}
	return line
}

// publicLine is the player-facing version of a cleaned console line, or "" when it is admin-only.
func publicLine(line string) string {
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

// chatLine is an in-game chat line without its [chat] tag, or "" for anything else.
func chatLine(line string) string {
	m := lineRe.FindStringSubmatch(line)
	if m == nil || !strings.HasPrefix(m[2], "[chat] ") {
		return ""
	}
	return fmt.Sprintf("[%s] %s", m[1], strings.TrimPrefix(m[2], "[chat] "))
}

// logLine is a console line for the log scope: every line but in-game chat (the chat scope has that).
func logLine(line string) string {
	if strings.HasPrefix(lineText(line), "[chat] ") {
		return ""
	}
	return line
}

// essentialLine: server up, the join code, config problems, warnings and errors; never chat or players.
func essentialLine(line string) string {
	text := lineText(line)
	if taggedRe.MatchString(text) || joinedRe.MatchString(text) || leftRe.MatchString(text) || strings.HasPrefix(text, "[throwdown] ") {
		return ""
	}
	if upRe.MatchString(text) || essentialRe.MatchString(text) {
		return line
	}
	return ""
}

// feed batches console lines for one webhook; convert filters/rewrites them (nil = every line).
type feed struct {
	h       *hook
	convert func(string) string
	pending []string
	first   time.Time
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
	if len(f.pending) == 0 || (time.Since(f.first) < flushAfter && total <= 1700) {
		return
	}
	batch := f.pending
	f.pending = nil
	var chunk []string
	size := 0
	flush := func() {
		if len(chunk) > 0 {
			f.h.send("```\n"+strings.Join(chunk, "\n")+"\n```", false)
		}
	}
	for _, line := range batch {
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

type tailState struct {
	ino uint64
	pos int64
}

func readNew(s *tailState) []string {
	st, err := os.Stat(tailPath)
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
	f, err := os.Open(tailPath)
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

func numbers(v string) []int {
	var out []int
	for _, m := range digitsRe.FindAllString(v, 4) {
		n, _ := strconv.Atoi(m)
		out = append(out, n)
	}
	return out
}

type schedule struct {
	days         map[time.Weekday]bool // nil = every day
	hour, minute int
}

// parseSchedule reads "04:00" or "sat,sun 04:00" (UTC).
func parseSchedule(v string) (schedule, bool) {
	m := scheduleRe.FindStringSubmatch(strings.ToLower(strings.TrimSpace(v)))
	if m == nil {
		return schedule{}, false
	}
	s := schedule{}
	s.hour, _ = strconv.Atoi(m[2])
	s.minute, _ = strconv.Atoi(m[3])
	if s.hour > 23 || s.minute > 59 {
		return schedule{}, false
	}
	if m[1] != "" {
		names := map[string]time.Weekday{"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
		s.days = map[time.Weekday]bool{}
		for _, d := range strings.Split(m[1], ",") {
			wd, ok := names[d]
			if !ok {
				return schedule{}, false
			}
			s.days[wd] = true
		}
	}
	return s, true
}

type updState struct {
	Version    string `json:"version,omitempty"`
	Announced  bool   `json:"announced,omitempty"`
	Declined   bool   `json:"declined,omitempty"`
	AskMessage string `json:"ask_message,omitempty"`
	Installed  string `json:"installed,omitempty"`
}

func readState() updState {
	var s updState
	if raw, err := os.ReadFile(updateState); err == nil {
		_ = json.Unmarshal(raw, &s)
	}
	return s
}

func writeState(s updState) {
	raw, _ := json.Marshal(s)
	if err := os.WriteFile(updateState+".tmp", raw, 0o644); err == nil {
		_ = os.Rename(updateState+".tmp", updateState)
	}
}

type notifier struct {
	sup                     *supervisor
	mode, imageVersion      string
	essentials, log, public *hook
	chat, updates, upd      *hook
	mentions                []string
	policy                  string
	countdown               int
	sched                   schedule
	checkEvery              time.Duration
	bot, channel            string
	mu                      sync.Mutex
	players                 map[string]bool
	busy                    string
	installing              sync.Mutex
	installingHeld          bool
	leaderboard             *leaderboard
}

func newNotifier(sup *supervisor, mode, imageVersion string) *notifier {
	name := username()
	var mentions []string
	for _, id := range strings.Split(os.Getenv("DISCORD_MENTION_IDS"), ",") {
		if id = strings.TrimSpace(id); id != "" {
			mentions = append(mentions, id)
		}
	}
	first := func(vs ...string) string {
		for _, v := range vs {
			if v = strings.TrimSpace(v); v != "" {
				return v
			}
		}
		return ""
	}
	hooks := webhooks()
	n := &notifier{
		sup: sup, mode: mode, imageVersion: imageVersion, mentions: mentions,
		essentials: newHook(hooks["essentials"], name, mentions),
		log:        newHook(hooks["log"], name, nil),
		public:     newHook(hooks["public"], name, nil),
		chat:       newHook(hooks["chat"], name, nil),
		updates:    newHook(hooks["updates"], name, nil),
		upd:        newHook(hooks["updates-admin"], name, mentions),
		policy:     strings.ToLower(first(os.Getenv("UPDATE_POLICY"), "timed")),
		countdown:  10,
		bot:        first(os.Getenv("DISCORD_BOT_TOKEN")),
		channel:    first(os.Getenv("DISCORD_APPROVAL_CHANNEL")),
		players:    map[string]bool{},
	}
	if v, err := strconv.Atoi(first(os.Getenv("UPDATE_COUNTDOWN"))); err == nil {
		n.countdown = v
	}
	if n.upd.url == "" { // without an updates-admin webhook, update messages stay in essentials
		n.upd = n.essentials
	}
	n.sched, _ = parseSchedule(os.Getenv("UPDATE_SCHEDULE"))
	every := 180
	if mode == "auto" || n.updates.url != "" {
		every = 30
	}
	if v, err := strconv.Atoi(first(os.Getenv("UPDATE_CHECK_MINUTES"))); err == nil {
		every = v
	}
	n.checkEvery = time.Duration(every) * time.Minute
	boardURLs := hooks["leaderboard"]
	if len(boardURLs) == 0 {
		boardURLs = hooks["public"]
	}
	boardHook := newHook(boardURLs, name, nil)
	var post func(string)
	if boardHook.url != "" {
		post = func(text string) { boardHook.send(text, false) }
	}
	n.leaderboard = newLeaderboard(sup.send, post)
	sup.notify = n.event
	if mode == "auto" {
		sup.onUpdate = func() { guarded(func() { n.check(true) }) }
	}
	return n
}

func (n *notifier) playersOn() bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	return len(n.players) > 0
}

func (n *notifier) track(line string) {
	if n.leaderboard != nil {
		n.leaderboard.feed(line)
	}
	text := lineText(line)
	n.mu.Lock()
	defer n.mu.Unlock()
	if upRe.MatchString(text) {
		n.players = map[string]bool{}
	} else if j := joinedRe.FindStringSubmatch(text); j != nil {
		n.players[j[1]] = true
	} else if l := leftRe.FindStringSubmatch(text); l != nil {
		delete(n.players, l[1])
	}
}

func (n *notifier) consoleLoop() {
	var feeds []*feed
	for _, f := range []*feed{{h: n.essentials, convert: essentialLine}, {h: n.log, convert: logLine}, {h: n.public, convert: publicLine}, {h: n.chat, convert: chatLine}} {
		if f.h.url != "" {
			feeds = append(feeds, f)
		}
	}
	s := &tailState{}
	if st, err := os.Stat(tailPath); err == nil { // only what the server writes from now on
		s.ino, s.pos = st.Sys().(*syscall.Stat_t).Ino, st.Size()
	}
	var answer []string
	answered := ""
	for {
		nw := readNew(s)
		for _, l := range nw {
			n.track(l)
		}
		n.sup.mu.Lock()
		cmd, at := n.sup.lastCmd, n.sup.lastAt
		n.sup.mu.Unlock()
		word, _, _ := strings.Cut(cmd, " ")
		if infoCommands[strings.ToLower(word)] && time.Since(at) < 3*time.Second {
			answer, answered, nw = append(answer, nw...), cmd, nil
		} else if len(answer) > 0 {
			n.postAnswer(answered, answer)
			answer = nil
		}
		for _, f := range feeds {
			f.add(nw)
			f.flushIfDue()
		}
		time.Sleep(time.Second)
	}
}

// postAnswer posts a console command's answer as one titled block in the log feed
// (essentials if there is no log feed).
func (n *notifier) postAnswer(cmd string, lines []string) {
	h := n.log
	if h.url == "" {
		h = n.essentials
	}
	title := "**Console:** `" + trunc(strings.ReplaceAll(cmd, "`", "'"), 100) + "`"
	var chunk []string
	size := 0
	flush := func() {
		if len(chunk) > 0 {
			h.send(title+"\n```ini\n"+strings.Join(chunk, "\n")+"\n```", false)
			title = "**Console:** `" + trunc(strings.ReplaceAll(cmd, "`", "'"), 100) + "` (continued)"
		}
	}
	for _, l := range lines {
		if size+len(l)+1 >= 1800 {
			flush()
			chunk, size = nil, 0
		}
		chunk = append(chunk, l)
		size += len(l) + 1
	}
	flush()
}

func (n *notifier) releaseInstalling() {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.installingHeld {
		n.installingHeld = false
		n.installing.Unlock()
	}
}

// event handles what the supervisor reports.
func (n *notifier) event(kind string, d map[string]string) {
	if kind == "installed" || kind == "failed" {
		n.releaseInstalling()
	}
	switch kind {
	case "installed":
		n.upd.send(fmt.Sprintf("**UPDATED** to ReSkate **%s**, the server is starting again.", d["version"]), false)
		n.public.send(fmt.Sprintf("The server is updated to ReSkate **%s** and starting again.", d["version"]), false)
		st := readState()
		st.Installed = d["version"]
		writeState(st)
	case "failed":
		n.upd.send(fmt.Sprintf("**UPDATE FAILED**: ReSkate %s: %s", d["version"], d["error"]), true)
	case "rolled_back":
		n.upd.send(fmt.Sprintf("**ROLLED BACK**: ReSkate %s stopped (exit %s) right after the update, back on **%s**. "+
			"It will not be installed again by itself; type `update` in the console to retry.", d["version"], d["code"], d["now"]), true)
		st := readState()
		st.Version, st.Declined = d["version"], true
		writeState(st)
	case "exited":
		n.essentials.send(fmt.Sprintf("**SERVER STOPPED** (exit code %s). Docker restarts it if the restart policy allows.", d["code"]), d["code"] != "0")
	}
}

func (n *notifier) running() string {
	if v := versionOf(n.sup.folder); v != "" {
		return v
	}
	return n.imageVersion
}

func (n *notifier) outdated(version string) bool { return cmpVersion(n.running(), version) < 0 }

func (n *notifier) updateLoop() {
	time.Sleep(time.Minute)
	for {
		n.check(false)
		time.Sleep(n.checkEvery)
	}
}

func (n *notifier) check(now bool) {
	r, err := latestRelease()
	if err != nil {
		note("release check failed: %v", err)
		if now {
			fmt.Printf("[update] release check failed: %v\n", err)
		}
		return
	}
	have := n.running()
	n.news(r.Version)
	if cmpVersion(r.Version, have) <= 0 {
		if now {
			fmt.Printf("[update] already on the latest ReSkate release (%s)\n", have)
		}
		return
	}
	if n.mode != "auto" {
		n.pinnedNotice(r.Version, have)
		return
	}
	if now {
		guarded(func() { n.apply(r, 0) })
		return
	}
	if st := readState(); st.Version == r.Version && st.Declined {
		return
	}
	n.mu.Lock()
	if n.busy == r.Version {
		n.mu.Unlock()
		return
	}
	n.busy = r.Version
	n.mu.Unlock()
	guarded(func() { n.handle(r, have) })
}

func (n *notifier) handle(r release, have string) {
	defer func() {
		n.mu.Lock()
		if n.busy == r.Version {
			n.busy = ""
		}
		n.mu.Unlock()
	}()
	st := readState()
	if st.Version != r.Version {
		st = updState{Version: r.Version}
	}
	how := map[string]string{
		"instant":   "Installing it now.",
		"timed":     fmt.Sprintf("Installing it in %d min (sooner if nobody is on).", n.countdown),
		"scheduled": fmt.Sprintf("Installing it at the next update slot (%s UTC).", os.Getenv("UPDATE_SCHEDULE")),
		"ask":       "Waiting for approval.",
	}[n.policy]
	if !st.Announced && n.upd.send(fmt.Sprintf("**UPDATE AVAILABLE**: ReSkate **%s** is out, this server runs **%s**. %s "+
		"`update` in the console installs it now.\nhttps://github.com/%s/releases/tag/v%s", r.Version, have, how, repo, r.Version), n.policy == "ask") {
		st.Announced = true
		writeState(st)
	}
	switch n.policy {
	case "scheduled":
		for n.outdated(r.Version) {
			t := time.Now().UTC()
			if (n.sched.days == nil || n.sched.days[t.Weekday()]) && t.Hour() == n.sched.hour && t.Minute() == n.sched.minute {
				break
			}
			time.Sleep(20 * time.Second)
		}
	case "ask":
		if !n.approved(r, have, &st) {
			return
		}
	}
	countdown := n.countdown
	if n.policy == "instant" {
		countdown = 0
	}
	if n.outdated(r.Version) {
		n.apply(r, countdown)
	}
}

func (n *notifier) apply(r release, countdown int) {
	if !n.outdated(r.Version) || !n.installing.TryLock() {
		return // released by event() once the install is done or failed
	}
	n.mu.Lock()
	n.installingHeld = true
	n.mu.Unlock()
	fmt.Printf("[update] downloading ReSkate %s\n", r.Version)
	staged, err := stageRelease(r)
	if err != nil {
		fmt.Printf("[update] ReSkate %s not installed: %v\n", r.Version, err)
		n.event("failed", map[string]string{"version": r.Version, "error": err.Error()})
		return
	}
	seen := map[int]bool{}
	var marks []int
	for _, m := range []int{countdown, 5, 1} {
		if m > 0 && m <= countdown && !seen[m] {
			seen[m] = true
			marks = append(marks, m)
		}
	}
	sort.Sort(sort.Reverse(sort.IntSlice(marks)))
	deadline := time.Now().Add(time.Duration(countdown) * time.Minute)
	for i, minutes := range marks {
		if !n.playersOn() {
			break
		}
		n.sup.send(fmt.Sprintf("announce Server restarts in %d min to update ReSkate to %s", minutes, r.Version))
		n.public.send(fmt.Sprintf("Restarting in **%d min** to update ReSkate to **%s**.", minutes, r.Version), false)
		if i == 0 {
			n.upd.send(fmt.Sprintf("Countdown started: installing ReSkate **%s** in %d min.", r.Version, minutes), false)
		}
		next := 0
		if i+1 < len(marks) {
			next = marks[i+1]
		}
		until := deadline.Add(-time.Duration(next) * time.Minute)
		for time.Now().Before(until) && n.playersOn() {
			time.Sleep(5 * time.Second)
		}
	}
	if n.playersOn() {
		n.sup.send(fmt.Sprintf("announce Restarting now for ReSkate %s, rejoin in a minute!", r.Version))
		time.Sleep(5 * time.Second)
	}
	n.public.send(fmt.Sprintf("Restarting now to update ReSkate to **%s**.", r.Version), false)
	n.upd.send(fmt.Sprintf("Installing ReSkate **%s** (was %s).", r.Version, n.running()), false)
	n.sup.install(staged, r.Version)
}

// approved (ask policy): true once an approver reacted ✅, false on ❌ (that version is skipped).
func (n *notifier) approved(r release, have string, st *updState) bool {
	waitConsole := func() bool {
		for n.outdated(r.Version) {
			time.Sleep(time.Minute)
		}
		return false
	}
	if n.bot == "" || n.channel == "" {
		note("UPDATE_POLICY=ask without DISCORD_BOT_TOKEN/DISCORD_APPROVAL_CHANNEL: waiting for `update` in the console")
		return waitConsole()
	}
	auth := []string{"Authorization: Bot " + n.bot, "User-Agent: DiscordBot (reskate-server-image, 1)"}
	base := discordAPI + "/channels/" + n.channel + "/messages"
	if st.AskMessage == "" {
		var tags []string
		for _, id := range n.mentions {
			tags = append(tags, "<@"+id+">")
		}
		users := n.mentions
		if users == nil {
			users = []string{}
		}
		var reply struct {
			ID string `json:"id"`
		}
		text := strings.TrimSpace(fmt.Sprintf("%s **APPROVE UPDATE?** %s: ReSkate **%s** (running %s). React %s to install (with a %d min "+
			"countdown for players) or %s to skip this version.", strings.Join(tags, " "), username(), r.Version, have, yesEmoji, n.countdown, noEmoji))
		if !request("POST", base, map[string]any{"content": text, "allowed_mentions": map[string]any{"parse": []string{}, "users": users}}, auth, &reply) || reply.ID == "" {
			n.upd.send("**APPROVAL FAILED**: the bot could not post in DISCORD_APPROVAL_CHANNEL (see /data/DiscordWebhook.log). "+
				"`update` in the console installs it.", true)
			return waitConsole()
		}
		st.AskMessage = reply.ID
		writeState(*st)
		for _, e := range []string{yesEmoji, noEmoji} {
			request("PUT", base+"/"+st.AskMessage+"/reactions/"+url.PathEscape(e)+"/@me", nil, auth, nil)
			time.Sleep(time.Second)
		}
	}
	allowed := map[string]bool{}
	for _, id := range n.mentions {
		allowed[id] = true
	}
	for n.outdated(r.Version) {
		for _, e := range []struct {
			emoji   string
			verdict bool
		}{{yesEmoji, true}, {noEmoji, false}} {
			var users []struct {
				ID  string `json:"id"`
				Bot bool   `json:"bot"`
			}
			request("GET", base+"/"+st.AskMessage+"/reactions/"+url.PathEscape(e.emoji)+"?limit=100", nil, auth, &users)
			for _, u := range users {
				if u.Bot || (len(allowed) > 0 && !allowed[u.ID]) {
					continue
				}
				word := "Approved"
				if !e.verdict {
					word = "Skipped"
				}
				request("PATCH", base+"/"+st.AskMessage, map[string]any{"content": fmt.Sprintf("**%s** by <@%s>: ReSkate **%s**.", word, u.ID, r.Version),
					"allowed_mentions": map[string]any{"parse": []string{}}}, auth, nil)
				if !e.verdict {
					st.Declined = true
					writeState(*st)
				}
				return e.verdict
			}
		}
		time.Sleep(20 * time.Second)
	}
	return false
}

func (n *notifier) pinnedNotice(latest, running string) {
	published := onHub(latest)
	var st struct {
		Version   string `json:"version"`
		Published bool   `json:"published"`
	}
	if raw, err := os.ReadFile(pinnedState); err == nil {
		_ = json.Unmarshal(raw, &st)
	}
	if st.Version == latest && (st.Published || !published) {
		return // already announced, nothing new to say
	}
	how := "Update with `docker compose pull && docker compose up -d`, or set `UPDATE_MODE=auto`. The restart kicks everyone and changes the join code."
	if !published {
		how = fmt.Sprintf("`%s:%s` is not on Docker Hub yet; `UPDATE_MODE=auto` would install it without a new image.", hub, latest)
	}
	text := fmt.Sprintf("**UPDATE AVAILABLE**: ReSkate **%s** is out, this server is pinned to **%s**. Players on the new version can't join until the server is updated.\n%s\nhttps://github.com/%s/releases/tag/v%s",
		latest, running, how, repo, latest)
	if st.Version == latest { // earlier message said "not published yet"
		text = fmt.Sprintf("**Image available**: `%s:%s` is on Docker Hub now. %s", hub, latest, how)
	}
	if n.upd.send(text, true) {
		st.Version, st.Published = latest, published
		raw, _ := json.Marshal(st)
		_ = os.WriteFile(pinnedState, raw, 0o644)
	}
}

func onHub(tag string) bool {
	r, err := client.Get("https://hub.docker.com/v2/repositories/" + hub + "/tags/" + tag)
	if err != nil {
		return false
	}
	r.Body.Close()
	return r.StatusCode == 200
}

// news (updates scope): a release newer than this image, then its image on Docker Hub, once each.
func (n *notifier) news(latest string) {
	base := n.imageVersion
	if !versionRe.MatchString(base) {
		base = n.running()
	}
	if n.updates.url == "" || cmpVersion(latest, base) <= 0 {
		return
	}
	var seen struct {
		Release string `json:"release,omitempty"`
		Image   string `json:"image,omitempty"`
	}
	if raw, err := os.ReadFile(newsState); err == nil {
		_ = json.Unmarshal(raw, &seen)
	}
	save := func() {
		raw, _ := json.Marshal(seen)
		_ = os.WriteFile(newsState, raw, 0o644)
	}
	if cmpVersion(latest, seen.Release) > 0 &&
		n.updates.send(fmt.Sprintf("**ReSkate %s is out.**\nhttps://github.com/%s/releases/tag/v%s", latest, repo, latest), false) {
		seen.Release = latest
		save()
	}
	if cmpVersion(latest, seen.Image) > 0 && onHub(latest) {
		slim := ""
		if onHub(latest + "-slim") {
			slim = fmt.Sprintf(" (also `%s-slim`)", latest)
		}
		if n.updates.send(fmt.Sprintf("**New image** on Docker Hub: `%s:%s`%s.", hub, latest, slim), false) {
			seen.Image = latest
			save()
		}
	}
}

func (n *notifier) postModEvents() {
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
		n.essentials.send(text, false)
	}
}

func (n *notifier) checkMods() {
	update := truthy(os.Getenv("MODS_UPDATE"), true)
	seen := map[string]string{}
	if raw, err := os.ReadFile(modsState); err == nil {
		_ = json.Unmarshal(raw, &seen)
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
		if n.essentials.send(fmt.Sprintf("**MOD UPDATE AVAILABLE**: %s **%s** is out, this server has **%s**. %s\nhttps://thunderstore.io/c/reskate/p/%s/%s/",
			key, u.latest, u.have, how, u.owner, u.name), true) {
			seen[key] = u.latest
			raw, _ := json.Marshal(seen)
			_ = os.WriteFile(modsState, raw, 0o644)
		}
	}
}

func (n *notifier) modsLoop() {
	time.Sleep(5 * time.Second)
	n.postModEvents()
	time.Sleep(85 * time.Second)
	for {
		n.checkMods()
		time.Sleep(modsCheckEvery)
	}
}

func guarded(fn func()) {
	go func() {
		defer func() {
			if r := recover(); r != nil {
				note("notifier: %v", r)
			}
		}()
		fn()
	}()
}

func (n *notifier) start() {
	guarded(n.consoleLoop)
	if n.essentials.url != "" && strings.TrimSpace(os.Getenv("MODS")) != "" {
		guarded(n.modsLoop)
	}
	if n.mode == "auto" || ((n.upd.url != "" || n.updates.url != "") && versionRe.MatchString(n.imageVersion)) {
		guarded(n.updateLoop)
	}
}
