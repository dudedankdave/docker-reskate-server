package main

// Ranked play and the leaderboard (LEADERBOARD=true), as leaderboard.py: players are told by DM
// whether they are ranked (no scoring/physics mods, no sped-up game this session), finished
// throwdowns with two or more players give points by place, and the board goes to chat and
// Discord every LEADERBOARD_INTERVAL minutes. LEADERBOARD_SCOPE=shared (default) keeps one board in
// /shared for every server that mounts it there (else this server's own); server: /data only.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	rankCheckAfter = 15 * time.Second // the mod report arrives after the join line
	chatMax        = 200              // bytes in one chat line (multiplayer_chat_max_bytes)
	rankedText     = "You are RANKED: your throwdown results count for the leaderboard."
	unrankedMods   = "You are NOT ranked: your mods change scoring or physics ({mods}). " +
		"Restart Skate without them to earn leaderboard points."
	unrankedSpeed = "You are NOT ranked until you rejoin: your game ran faster than normal."
)

var (
	lbJoinedRe   = regexp.MustCompile(`^(.+) joined \((\d+)(?:, admin)?\), \d+/\d+ players`)
	lbModdedRe   = regexp.MustCompile(`^\[anticheat\] (.+)'s mods change scoring or physics: (.*) \(scoring [^)]*\)\.$`)
	lbClearedRe  = regexp.MustCompile(`^\[anticheat\] (.+) may take part in throwdowns again `)
	lbSpeedingRe = regexp.MustCompile(`^\[anticheat\] (.+)'s game is running at [\d.]+x speed`)
	lbFinishedRe = regexp.MustCompile(`^\[throwdown\] .+'s (Jam|Spot Battle|S\.K\.A\.T\.E\.|throwdown) has finished: (.+)$`)
	lbEntryRe    = regexp.MustCompile(`^(.+) (-?\d{1,3}(?:,\d{3})*)( \(quit\))?$`)
	lbTriedRe    = regexp.MustCompile(`(?: \d+ landed / \d+ missed)( \(quit\))?(?:, |$)`)
)

type lbPlayer struct {
	Name   string `json:"name"`
	Points int    `json:"points"`
	Wins   int    `json:"wins"`
	Played int    `json:"played"`
}

type lbData struct {
	Players       map[string]*lbPlayer `json:"players"`
	Version       int                  `json:"version,omitempty"`
	PostedAt      float64              `json:"posted_at,omitempty"`
	PostedVersion int                  `json:"posted_version,omitempty"`
}

type lbResult struct {
	name  string
	place int // 0: S.K.A.T.E., no places
	quit  bool
}

type leaderboard struct {
	send             func(string) bool
	post             func(string)
	path, label      string
	interval         time.Duration
	size             int
	points           []int
	ranked, unranked string

	mu       sync.Mutex
	online   map[string]string    // name -> SteamID64
	waiting  map[string]time.Time // name -> when the ranked DM is due
	modded   map[string]string
	speeding map[string]bool
	told     map[string]bool
	chatAt   time.Time
	postAt   time.Time
}

func lbEnv(name, def string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return def
}

// fit cuts text to n bytes without splitting a character.
func fit(text string, n int) string {
	if len(text) <= n {
		return text
	}
	text = text[:n]
	for len(text) > 0 && !utf8ValidEnd(text) {
		text = text[:len(text)-1]
	}
	return text
}

func utf8ValidEnd(s string) bool {
	return strings.ToValidUTF8(s, "�") == s
}

// newLeaderboard starts the leaderboard when LEADERBOARD is on; nil otherwise.
func newLeaderboard(send func(string) bool, post func(string)) *leaderboard {
	if !truthy(os.Getenv("LEADERBOARD"), false) {
		return nil
	}
	b := &leaderboard{
		send: send, post: post,
		path: "/data/leaderboard.json", label: "this server",
		interval: 60 * time.Minute, size: 5,
		ranked: lbEnv("RANKED_MESSAGE", rankedText), unranked: lbEnv("UNRANKED_MESSAGE", unrankedMods),
		online: map[string]string{}, waiting: map[string]time.Time{}, modded: map[string]string{},
		speeding: map[string]bool{}, told: map[string]bool{},
		chatAt: time.Now(), postAt: time.Now(),
	}
	if strings.ToLower(lbEnv("LEADERBOARD_SCOPE", "shared")) != "server" {
		path := lbEnv("LEADERBOARD_FILE", "/shared/leaderboard.json")
		dir := filepath.Dir(path)
		if st, err := os.Stat(dir); err == nil && st.IsDir() {
			b.path, b.label = path, "all servers"
		} else {
			fmt.Printf("[leaderboard] %s is not mounted, so the leaderboard is this server's own "+
				"(LEADERBOARD_SCOPE=server); mount one folder there on every server to share it\n", dir)
		}
	} else {
		b.path = lbEnv("LEADERBOARD_FILE", b.path)
	}
	if v, err := strconv.Atoi(lbEnv("LEADERBOARD_INTERVAL", "60")); err == nil && v > 0 {
		b.interval = time.Duration(v) * time.Minute
	}
	if v, err := strconv.Atoi(lbEnv("LEADERBOARD_SIZE", "5")); err == nil {
		b.size = max(1, min(10, v))
	}
	for _, item := range asList(lbEnv("LEADERBOARD_POINTS", "10,6,4,2")) {
		if v, err := strconv.Atoi(item); err == nil {
			b.points = append(b.points, v)
		}
	}
	guarded(b.loop)
	return b
}

// ---- the points file, changed under an exclusive lock ------------------------------------

func (b *leaderboard) locked(exclusive bool, fn func(*lbData) bool) {
	lock, err := os.OpenFile(b.path+".lock", os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		fmt.Printf("[leaderboard] cannot use %s: %v\n", b.path, err)
		return
	}
	defer lock.Close()
	how := syscall.LOCK_SH
	if exclusive {
		how = syscall.LOCK_EX
	}
	_ = syscall.Flock(int(lock.Fd()), how)
	d := &lbData{}
	if raw, err := os.ReadFile(b.path); err == nil {
		_ = json.Unmarshal(raw, d)
	}
	if d.Players == nil {
		d.Players = map[string]*lbPlayer{}
	}
	if !fn(d) || !exclusive {
		return
	}
	raw, _ := json.MarshalIndent(d, "", " ")
	if err := os.WriteFile(b.path+".tmp", raw, 0o644); err == nil {
		err = os.Rename(b.path+".tmp", b.path)
	}
	if err != nil {
		fmt.Printf("[leaderboard] cannot write %s: %v\n", b.path, err)
	}
}

func lbTop(d *lbData, n int) []*lbPlayer {
	var rows []*lbPlayer
	for _, p := range d.Players {
		if p.Points > 0 {
			rows = append(rows, p)
		}
	}
	sort.Slice(rows, func(i, j int) bool {
		a, c := rows[i], rows[j]
		if a.Points != c.Points {
			return a.Points > c.Points
		}
		if a.Wins != c.Wins {
			return a.Wins > c.Wins
		}
		return strings.ToLower(a.Name) < strings.ToLower(c.Name)
	})
	if len(rows) > n {
		rows = rows[:n]
	}
	return rows
}

// ---- log lines ---------------------------------------------------------------------------

func (b *leaderboard) feed(line string) {
	text := lineText(line)
	b.mu.Lock()
	defer b.mu.Unlock()
	forget := func(name string) {
		delete(b.waiting, name)
		delete(b.modded, name)
		delete(b.speeding, name)
		delete(b.told, name)
	}
	if upRe.MatchString(text) {
		b.online, b.waiting, b.modded = map[string]string{}, map[string]time.Time{}, map[string]string{}
		b.speeding, b.told = map[string]bool{}, map[string]bool{}
	} else if m := lbJoinedRe.FindStringSubmatch(text); m != nil {
		forget(m[1])
		b.online[m[1]] = m[2]
		b.waiting[m[1]] = time.Now().Add(rankCheckAfter)
	} else if m := leftRe.FindStringSubmatch(text); m != nil {
		forget(m[1])
		delete(b.online, m[1])
	} else if m := lbModdedRe.FindStringSubmatch(text); m != nil {
		b.modded[m[1]] = m[2]
		b.tell(m[1])
	} else if m := lbClearedRe.FindStringSubmatch(text); m != nil {
		delete(b.modded, m[1])
		b.tell(m[1])
	} else if m := lbSpeedingRe.FindStringSubmatch(text); m != nil {
		b.speeding[m[1]] = true
		b.tell(m[1])
	} else if m := lbFinishedRe.FindStringSubmatch(text); m != nil {
		b.score(lbResults(m[1], m[2]))
	}
}

func (b *leaderboard) isRanked(name string) bool {
	_, modded := b.modded[name]
	return !modded && !b.speeding[name]
}

// tell DMs the player whether they are ranked, unless the join check is pending or nothing changed.
func (b *leaderboard) tell(name string) {
	id, on := b.online[name]
	if _, pending := b.waiting[name]; !on || pending {
		return
	}
	ranked := b.isRanked(name)
	if was, ok := b.told[name]; ok && was == ranked {
		return
	}
	b.told[name] = ranked
	text := b.ranked
	if !ranked {
		if mods, ok := b.modded[name]; ok {
			if mods == "" {
				mods = "your mods"
			}
			text = strings.ReplaceAll(b.unranked, "{mods}", mods)
		} else {
			text = unrankedSpeed
		}
	}
	b.send(fit("msg "+id+" "+text, chatMax+30))
}

// lbResults reads the results of a finished line: "1. Anna 12,345, 2. Ben 9,876 (quit)" or,
// for S.K.A.T.E., "Anna 7 landed / 3 missed, Ben 2 landed / 5 missed (quit)".
func lbResults(mode, results string) []lbResult {
	var out []lbResult
	if mode == "S.K.A.T.E." {
		start := 0
		for _, m := range lbTriedRe.FindAllStringSubmatchIndex(results, -1) {
			out = append(out, lbResult{name: results[start:m[0]], quit: m[2] >= 0})
			start = m[1]
		}
		return out
	}
	rest := results
	for place := 1; strings.HasPrefix(rest, strconv.Itoa(place)+". "); place++ {
		rest = rest[len(strconv.Itoa(place))+2:]
		entry := rest
		if next := strings.Index(rest, ", "+strconv.Itoa(place+1)+". "); next >= 0 {
			entry, rest = rest[:next], rest[next+2:]
		} else {
			rest = ""
		}
		if m := lbEntryRe.FindStringSubmatch(entry); m != nil {
			out = append(out, lbResult{name: m[1], place: place, quit: m[3] != ""})
		}
	}
	return out
}

type lbAward struct {
	name, key string
	points    int
	won       bool
}

func (b *leaderboard) score(results []lbResult) {
	var finished []lbResult
	for _, r := range results {
		if !r.quit {
			finished = append(finished, r)
		}
	}
	if len(finished) < 2 || len(b.points) == 0 {
		return
	}
	var awards []lbAward
	for _, r := range finished {
		if !b.isRanked(r.name) {
			continue
		}
		i := len(b.points) - 1
		if r.place > 0 {
			i = min(r.place, len(b.points)) - 1
		}
		key := b.online[r.name]
		if key == "" {
			key = "name:" + r.name
		}
		awards = append(awards, lbAward{r.name, key, b.points[i], r.place == 1})
	}
	if len(awards) == 0 {
		return
	}
	b.locked(true, func(d *lbData) bool {
		for _, a := range awards {
			p := d.Players[a.key]
			if p == nil {
				p = &lbPlayer{}
				d.Players[a.key] = p
			}
			p.Name = a.name
			p.Points += a.points
			p.Played++
			if a.won {
				p.Wins++
			}
		}
		d.Version++
		return true
	})
	var parts []string
	for _, a := range awards {
		parts = append(parts, fmt.Sprintf("%s +%d", a.name, a.points))
	}
	fmt.Println("[leaderboard] " + strings.Join(parts, ", "))
}

// ---- timers ------------------------------------------------------------------------------

func (b *leaderboard) tick() {
	now := time.Now()
	b.mu.Lock()
	for name, due := range b.waiting {
		if !now.Before(due) {
			delete(b.waiting, name)
			b.tell(name)
		}
	}
	playersOn := len(b.online) > 0
	b.mu.Unlock()
	if now.Sub(b.chatAt) >= b.interval {
		b.chatAt = now
		if playersOn {
			b.postChat()
		}
	}
	if b.post != nil && now.Sub(b.postAt) >= time.Minute {
		b.postAt = now
		b.postDiscord(now)
	}
}

func (b *leaderboard) postChat() {
	var rows []*lbPlayer
	b.locked(false, func(d *lbData) bool { rows = lbTop(d, b.size); return false })
	if len(rows) == 0 {
		return
	}
	var entries []string
	for i, p := range rows {
		entries = append(entries, fmt.Sprintf("%d. %s %d", i+1, p.Name, p.Points))
	}
	b.send("announce " + fit("Leaderboard: "+strings.Join(entries[:min(3, len(entries))], " | "), chatMax))
	line := "Leaderboard (" + b.label + ", ranked throwdowns):"
	for _, e := range entries {
		if len(line)+2+len(e) > chatMax {
			b.send("say " + line)
			line = e
		} else {
			line += "  " + e
		}
	}
	b.send("say " + line)
}

func (b *leaderboard) postDiscord(now time.Time) {
	stamp := float64(now.UnixNano()) / 1e9
	due := func(d *lbData) bool {
		return stamp-d.PostedAt >= b.interval.Seconds() && d.Version != d.PostedVersion
	}
	ready := false
	b.locked(false, func(d *lbData) bool { ready = due(d); return false })
	if !ready {
		return
	}
	var rows []*lbPlayer
	b.locked(true, func(d *lbData) bool {
		if !due(d) {
			return false // another server posted meanwhile
		}
		d.PostedAt, d.PostedVersion = stamp, d.Version
		rows = lbTop(d, 10)
		return true
	})
	if len(rows) == 0 {
		return
	}
	width := 0
	for _, p := range rows {
		width = max(width, len(p.Name))
	}
	var lines []string
	for i, p := range rows {
		lines = append(lines, fmt.Sprintf("%2d. %-*s  %5d pts  %d won / %d played", i+1, width, p.Name, p.Points, p.Wins, p.Played))
	}
	b.post("**Leaderboard** (" + b.label + ", ranked throwdowns)\n```\n" + strings.ReplaceAll(strings.Join(lines, "\n"), "```", "'''") + "\n```")
}

func (b *leaderboard) loop() {
	for {
		func() {
			defer func() {
				if r := recover(); r != nil {
					fmt.Printf("[leaderboard] %v\n", r)
				}
			}()
			b.tick()
		}()
		time.Sleep(time.Second)
	}
}
