package main

// Ranked play and the leaderboards (LEADERBOARD=true), as leaderboard.py: players are told by DM
// whether they are ranked (no scoring/physics mods, no sped-up game this session, and the server
// not letting players skate with their own physics tuning, which it cannot check), and finished
// throwdowns with two or more players give points by place. The points live in a SQLite database
// (leaderboard.db, with a ledger of every award) on several boards at once: All Time,
// the current Week and the Events admins have open (LEADERBOARD_BOARDS). Chat commands /top, /rank,
// /boards and, for admins, /lb answer by DM to the "[command] Name /top week" line the server
// logs for each allowed command. The board goes to chat and Discord every LEADERBOARD_INTERVAL
// minutes. LEADERBOARD_SCOPE=shared (default) keeps one database in /shared for every server
// that mounts it there (else this server's own); server: /data only.

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

const (
	rankCheckAfter = 15 * time.Second // the mod report arrives after the join line
	chatMax        = 200              // bytes in one chat line (multiplayer_chat_max_bytes)
	lbAskEvery     = 3 * time.Second  // between two answers to the same player
	rankedText     = "You are RANKED: your throwdown results count for the leaderboard."
	unrankedMods   = "You are NOT ranked: your mods change scoring or physics ({mods}). " +
		"Restart Skate without them to earn leaderboard points."
	unrankedServer = "This is an UNRANKED server: your results count on its own Unranked boards, apart from the Ranked ones."
	unrankedSpeed  = "You are NOT ranked until you rejoin: your game ran faster than normal."
	unrankedTuning = "Nobody is ranked right now: this server lets players skate with their own physics tuning. " +
		"Points count again once it is back on the game's own."
)

// lbKinds are the board kinds LEADERBOARD_BOARDS may name.
var lbKinds = []string{"general", "year", "month", "week", "event"}

// Every board also has one per game mode: "All Time - Jam", "Week 41 - S.K.A.T.E.".
var (
	lbModeNames = map[string]string{"jam": "Jam", "spot": "Spot Battle", "skate": "S.K.A.T.E.", "props": "Props"}
	lbModeOf    = map[string]string{"Jam": "jam", "throwdown": "jam", "Spot Battle": "spot", "S.K.A.T.E.": "skate"}
	lbModeWords = map[string]string{"jam": "jam", "jams": "jam", "skate": "skate", "s.k.a.t.e": "skate", "s.k.a.t.e.": "skate",
		"game of skate": "skate", "spot": "spot", "spot battle": "spot", "spotbattle": "spot", "battle": "spot", "spot battles": "spot", "props": "props", "prop": "props"}
)

// lbCommands are added to the server's "commands" (entrypoint.go): the reply is the header, the
// rows follow as a DM from the leaderboard.
var lbCommands = []map[string]any{
	{"name": "top", "reply": lbRule},
	{"name": "rank", "reply": lbRule},
	{"name": "boards", "reply": lbRule},
	{"name": "props", "reply": lbRule},
	{"name": "lb", "admin": true, "reply": lbRule},
}

// lbTeamCommand is added with TEAMS=true: the leaderboard module answers it through the bridge (teams).
var lbTeamCommand = map[string]any{"name": "team", "reply": lbRule}

// lbOldReplies are the replies of the same commands in earlier builds: a command with one of them is ours, to be replaced.
var lbOldReplies = map[string]bool{"Leaderboard (/top week, /top event, /boards):": true,
	"Leaderboard (/top week, /top skate, /top week jam, /boards):": true, "Your rank (/rank week, /rank event):": true,
	"Leaderboards:": true, "Leaderboard admin (/lb help):": true}

var (
	lbJoinedRe   = regexp.MustCompile(`^(.+) joined \((\d+)(?:, ([a-z]+))?\), \d+/\d+ players`)
	lbModdedRe   = regexp.MustCompile(`^\[anticheat\] (.+)'s mods change scoring or physics: (.*) \(scoring [^)]*\)\.$`)
	lbClearedRe  = regexp.MustCompile(`^\[anticheat\] (.+) may take part in throwdowns again `)
	lbSpeedingRe = regexp.MustCompile(`^\[anticheat\] (.+)'s game is running at [\d.]+x speed`)
	lbFinishedRe = regexp.MustCompile(`^\[throwdown\] .+'s (Jam|Spot Battle|S\.K\.A\.T\.E\.|throwdown) has finished: (.+)$`)
	lbEntryRe    = regexp.MustCompile(`^(.+) (-?\d{1,3}(?:,\d{3})*)( \(quit\))?$`)
	lbTriedRe    = regexp.MustCompile(`(?: (\d+) landed / (\d+) missed)( \(quit\))?(?:, |$)`)
	lbTuningRe   = regexp.MustCompile(`^Players skate with (the game's|their) own physics tuning\.$`)
	lbCommandRe  = regexp.MustCompile(`^\[command\] (.+)$`)
	lbControl    = regexp.MustCompile(`[\x00-\x1f\x7f]+`)
	lbWeekNumRe  = regexp.MustCompile(`^(?:week[ -]?|w)?(\d{1,2})$`)
	lbAwardRe    = regexp.MustCompile(`^(-?\d{1,6})\s+(.+?)(?:\s+board:(\S+))?$`)
	lbSteamRe    = regexp.MustCompile(`^\d{17}$`)
	lbSlugRe     = regexp.MustCompile(`[^a-z0-9]+`)
	lbSpaceRe    = regexp.MustCompile(`\s+`)
)

type lbResult struct {
	name           string
	place          int // 0: S.K.A.T.E., no places
	quit           bool
	missed, landed int // S.K.A.T.E.: placed by the fewest missed, then the most landed
}

type lbBoard struct {
	id, name, kind      string
	closed              bool
	version, postedVers int
}

type lbRow struct {
	name, steam          string
	points, wins, played int
}

type leaderboard struct {
	send             func(string) bool
	post             func(string)
	db               *sql.DB
	path, label      string
	league           string // "Ranked" or "Unranked": whose boards these are
	server           string
	kinds            map[string]bool
	interval         time.Duration
	size             int
	propsPerDay      int             // /props: most a player gives in 24 hours; 0 = off
	propsAnnounce    bool            // say who gave props to whom in chat
	discordScopes    map[string]bool // boards Discord lists (LEADERBOARD_DISCORD)
	discordGames     map[string]bool // and which game boards of them
	points           []int
	ranked, unranked string
	tuning, speed    string

	mu                      sync.Mutex
	online                  map[string]string    // name -> SteamID64
	admins                  map[string]bool      // SteamID64s that joined as admin
	waiting                 map[string]time.Time // name -> when the ranked DM is due
	modded                  map[string]string
	speeding                map[string]bool
	told                    map[string]bool
	asked                   map[string]time.Time // SteamID64 -> their last command answer
	free                    bool                 // own physics tuning allowed: nobody is ranked
	chatAt                  map[string]time.Time // kind -> the last chat post
	postAt                  time.Time
	started                 time.Time
	chatEvery, discordEvery map[string]time.Duration // per board kind; 0 = never
}

// lbIntervals reads "general=60,week=30,event=0" (minutes, 0 = never); kinds not named use def.
func lbIntervals(name string, def time.Duration) map[string]time.Duration {
	out := map[string]time.Duration{}
	for _, k := range lbKinds {
		out[k] = def
	}
	for _, item := range asList(os.Getenv(name)) {
		kind, minutes, _ := strings.Cut(item, "=")
		kind = strings.ToLower(strings.TrimSpace(kind))
		if _, known := out[kind]; known {
			if n, err := strconv.Atoi(strings.TrimSpace(minutes)); err == nil && n >= 0 {
				out[kind] = time.Duration(n) * time.Minute
			}
		}
	}
	return out
}

// lbKindOf: general / year / month / week / event, also for a game board ("week-2026-41:jam").
func lbKindOf(id string) string {
	id, _, _ = strings.Cut(id, ":")
	kind, _, _ := strings.Cut(id, "-")
	return kind
}

func lbEnv(name, def string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return def
}

// fit cuts text to n bytes without splitting a character.
func fit(text string, n int) string {
	text = lbControl.ReplaceAllString(text, " ")
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

// freeTuning: the config lets players skate with their own physics tuning (enforce_tuning off).
func freeTuning() bool {
	var cfg struct {
		AntiCheat struct {
			EnforceTuning *bool `json:"enforce_tuning"`
		} `json:"anti_cheat"`
	}
	raw, err := os.ReadFile(configPath)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		return false
	}
	return cfg.AntiCheat.EnforceTuning != nil && !*cfg.AntiCheat.EnforceTuning
}

// newLeaderboard starts the leaderboard when LEADERBOARD is on; nil otherwise.
func newLeaderboard(send func(string) bool, post func(string)) *leaderboard {
	if !truthy(os.Getenv("LEADERBOARD"), false) {
		return nil
	}
	b := &leaderboard{
		send: send, post: post,
		path: "/data/leaderboard.db", label: "this server", server: lbEnv("SERVER_NAME", "server"),
		interval: 60 * time.Minute, size: 5, discordScopes: map[string]bool{}, discordGames: map[string]bool{}, kinds: map[string]bool{},
		ranked: lbEnv("RANKED_MESSAGE", rankedText), unranked: lbEnv("UNRANKED_MESSAGE", unrankedMods),
		tuning: lbEnv("UNRANKED_TUNING_MESSAGE", unrankedTuning), speed: lbEnv("UNRANKED_SPEED_MESSAGE", unrankedSpeed),
		online: map[string]string{}, admins: map[string]bool{}, waiting: map[string]time.Time{}, modded: map[string]string{},
		speeding: map[string]bool{}, told: map[string]bool{}, asked: map[string]time.Time{}, free: freeTuning(),
		chatAt: map[string]time.Time{}, postAt: time.Now(), started: time.Now(),
	}
	b.league = "Ranked"
	dbName := "leaderboard.db"
	if strings.ToLower(lbEnv("LEADERBOARD_LEAGUE", "ranked")) == "unranked" {
		b.league, dbName = "Unranked", "leaderboard-unranked.db"
	}
	b.path = "/data/" + dbName
	if strings.ToLower(lbEnv("LEADERBOARD_SCOPE", "shared")) != "server" {
		path := lbEnv("LEADERBOARD_FILE", "/shared/"+dbName)
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
	legacy := filepath.Join(filepath.Dir(b.path), "leaderboard.json")
	if b.league == "Unranked" { // the old points belong to the Ranked league
		legacy = ""
	}
	if strings.HasSuffix(b.path, ".json") { // an old LEADERBOARD_FILE: the points move to a .db beside it
		legacy, b.path = b.path, strings.TrimSuffix(b.path, ".json")+".db"
	}
	if v, err := strconv.Atoi(lbEnv("LEADERBOARD_INTERVAL", "60")); err == nil && v > 0 {
		b.interval = time.Duration(v) * time.Minute
	}
	if v, err := strconv.Atoi(lbEnv("LEADERBOARD_SIZE", "5")); err == nil {
		b.size = max(1, min(10, v))
	}
	b.propsPerDay, b.propsAnnounce = 5, truthy(lbEnv("LEADERBOARD_PROPS_ANNOUNCE", "true"), true)
	if v, err := strconv.Atoi(lbEnv("LEADERBOARD_PROPS_PER_DAY", "5")); err == nil && v >= 0 {
		b.propsPerDay = v
	}
	b.chatEvery = lbIntervals("LEADERBOARD_INTERVALS", b.interval)
	b.discordEvery = lbIntervals("LEADERBOARD_DISCORD_INTERVALS", b.interval)
	for _, item := range asList(lbEnv("LEADERBOARD_POINTS", "10,6,4,2")) {
		if v, err := strconv.Atoi(item); err == nil {
			b.points = append(b.points, v)
		}
	}
	for _, k := range asList(strings.ToLower(lbEnv("LEADERBOARD_BOARDS", strings.Join(lbKinds, ",")))) {
		for _, known := range lbKinds {
			b.kinds[known] = b.kinds[known] || k == known
		}
	}
	if len(b.kinds) == 0 || !(b.kinds["general"] || b.kinds["year"] || b.kinds["month"] || b.kinds["week"] || b.kinds["event"]) {
		b.kinds["general"] = true
	}
	for _, k := range asList(strings.ToLower(lbEnv("LEADERBOARD_DISCORD", "general,week,event,jam,spot,skate"))) {
		if _, game := lbModeNames[k]; game {
			b.discordGames[k] = true
		}
		for _, known := range lbKinds {
			if k == known {
				b.discordScopes[k] = true
			}
		}
	}
	if len(b.discordScopes) == 0 {
		for _, known := range lbKinds {
			b.discordScopes[known] = true
		}
	}
	if !b.setup(legacy) {
		return nil
	}
	guarded(b.loop)
	return b
}

// ---- the database ------------------------------------------------------------------------

const lbSchema = `
CREATE TABLE IF NOT EXISTS boards (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
  created_at REAL NOT NULL, closed_at REAL,
  version INTEGER NOT NULL DEFAULT 0, posted_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS players (steam_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scores (
  board TEXT NOT NULL REFERENCES boards(id), steam_id TEXT NOT NULL,
  points INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0, played INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (board, steam_id));
CREATE INDEX IF NOT EXISTS scores_by_points ON scores (board, points DESC);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, board TEXT NOT NULL, steam_id TEXT NOT NULL,
  points INTEGER NOT NULL, reason TEXT NOT NULL, server TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS props_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, giver TEXT NOT NULL, receiver TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS props_by_giver ON props_log (giver, at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);`

func unixf(t time.Time) float64 { return float64(t.UnixNano()) / 1e9 }

// run does fn in one write transaction (several servers may share the file), logging a failure.
func (b *leaderboard) run(fn func(tx *sql.Tx) error) bool {
	tx, err := b.db.Begin()
	if err == nil {
		if err = fn(tx); err != nil {
			_ = tx.Rollback()
		} else {
			err = tx.Commit()
		}
	}
	if err != nil {
		fmt.Printf("[leaderboard] cannot use %s: %v\n", b.path, err)
		return false
	}
	return true
}

func (b *leaderboard) setup(legacy string) bool {
	dsn := "file:" + (&url.URL{Path: b.path}).EscapedPath() +
		"?_pragma=busy_timeout(15000)&_pragma=journal_mode(WAL)&_txlock=immediate"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		fmt.Printf("[leaderboard] cannot use %s: %v\n", b.path, err)
		return false
	}
	db.SetMaxOpenConns(1)
	b.db = db
	imported := false
	ok := b.run(func(tx *sql.Tx) error {
		for _, stmt := range strings.Split(lbSchema, ";") {
			if strings.TrimSpace(stmt) == "" {
				continue
			}
			if _, err := tx.Exec(stmt); err != nil {
				return err
			}
		}
		lbBoardInsert(tx, "general", "All Time", "general")
		_, _ = tx.Exec(`UPDATE boards SET name = 'All Time' WHERE id = 'general' AND name != 'All Time'`) // was "General"
		_, _ = tx.Exec(`UPDATE boards SET name = 'All Time' || substr(name, 8) WHERE id LIKE 'general:%' AND name LIKE 'General - %'`)
		var one int
		if tx.QueryRow(`SELECT 1 FROM meta WHERE key='legacy_imported'`).Scan(&one) == nil {
			return nil
		}
		if _, err := tx.Exec(`INSERT INTO meta VALUES ('legacy_imported', ?)`, strconv.FormatFloat(unixf(time.Now()), 'f', 0, 64)); err != nil {
			return err
		}
		if _, err := os.Stat(legacy); err != nil {
			return nil
		}
		var err error
		imported, err = b.importLegacy(tx, legacy)
		return err
	})
	if ok && imported {
		_ = os.Rename(legacy, legacy+".migrated")
	}
	return ok
}

// importLegacy: the leaderboard.json of the versions before the database becomes All Time.
func (b *leaderboard) importLegacy(tx *sql.Tx, legacy string) (bool, error) {
	raw, err := os.ReadFile(legacy)
	if err != nil {
		return false, nil
	}
	var old struct {
		Players map[string]struct {
			Name                 string `json:"name"`
			Points, Wins, Played int
		} `json:"players"`
	}
	if json.Unmarshal(raw, &old) != nil {
		return false, nil
	}
	now := unixf(time.Now())
	for key, p := range old.Players {
		name := p.Name
		if name == "" {
			name = key
		}
		if _, err := tx.Exec(`INSERT OR REPLACE INTO players VALUES (?, ?)`, key, name); err != nil {
			return false, err
		}
		if _, err := tx.Exec(`INSERT OR IGNORE INTO scores VALUES ('general', ?, ?, ?, ?)`, key, p.Points, p.Wins, p.Played); err != nil {
			return false, err
		}
		if _, err := tx.Exec(`INSERT INTO ledger (at, board, steam_id, points, reason, server) VALUES (?, 'general', ?, ?, 'imported', ?)`,
			now, key, p.Points, b.server); err != nil {
			return false, err
		}
	}
	_, err = tx.Exec(`UPDATE boards SET version = version + 1 WHERE id = 'general'`)
	fmt.Printf("[leaderboard] imported %d players from %s into All Time\n", len(old.Players), legacy)
	return err == nil, err
}

func lbBoardInsert(tx *sql.Tx, id, name, kind string) {
	_, _ = tx.Exec(`INSERT OR IGNORE INTO boards (id, name, kind, created_at) VALUES (?, ?, ?, ?)`, id, name, kind, unixf(time.Now()))
}

var lbMonths = []string{"January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"}

// lbPeriod is the id and name of the current year / month / week board (UTC).
func lbPeriod(kind string, now time.Time) (id, name string) {
	t := now.UTC()
	switch kind {
	case "year":
		return fmt.Sprintf("year-%d", t.Year()), strconv.Itoa(t.Year())
	case "month":
		return fmt.Sprintf("month-%d-%02d", t.Year(), int(t.Month())), fmt.Sprintf("%s %d", lbMonths[t.Month()-1], t.Year())
	}
	id, name, _ = lbWeek(now)
	return
}

func lbWeek(now time.Time) (id, name string, year int) {
	year, week := now.UTC().ISOWeek()
	return fmt.Sprintf("week-%d-%02d", year, week), fmt.Sprintf("Week %d", week), year
}

// active: the boards that count new points: All Time, this week, the open events.
func (b *leaderboard) active(tx *sql.Tx, now time.Time) []string {
	var ids []string
	if b.kinds["general"] {
		lbBoardInsert(tx, "general", "All Time", "general") // (missing after the points were wiped)
		ids = append(ids, "general")
	}
	for _, kind := range []string{"year", "month", "week"} {
		if b.kinds[kind] {
			id, name := lbPeriod(kind, now)
			lbBoardInsert(tx, id, name, kind)
			ids = append(ids, id)
		}
	}
	if b.kinds["event"] {
		if rows, err := tx.Query(`SELECT id FROM boards WHERE kind='event' AND closed_at IS NULL ORDER BY created_at`); err == nil {
			for rows.Next() {
				var id string
				if rows.Scan(&id) == nil {
					ids = append(ids, id)
				}
			}
			rows.Close()
		}
	}
	return ids
}

type lbAward struct {
	steam, name string
	points      int
	won, played int
}

// add gives every award to every board in boards, with a ledger line each.
func (b *leaderboard) add(tx *sql.Tx, boards []string, awards []lbAward, reason string, now time.Time) error {
	for _, a := range awards {
		if _, err := tx.Exec(`INSERT INTO players VALUES (?, ?) ON CONFLICT(steam_id) DO UPDATE SET name=excluded.name`, a.steam, a.name); err != nil {
			return err
		}
		for _, board := range boards {
			if _, err := tx.Exec(`INSERT INTO scores VALUES (?, ?, ?, ?, ?) ON CONFLICT(board, steam_id) DO UPDATE SET `+
				`points = points + excluded.points, wins = wins + excluded.wins, played = played + excluded.played`,
				board, a.steam, a.points, a.won, a.played); err != nil {
				return err
			}
			if _, err := tx.Exec(`INSERT INTO ledger (at, board, steam_id, points, reason, server) VALUES (?, ?, ?, ?, ?, ?)`,
				unixf(now), board, a.steam, a.points, reason, b.server); err != nil {
				return err
			}
		}
	}
	for _, board := range boards {
		if _, err := tx.Exec(`UPDATE boards SET version = version + 1 WHERE id = ?`, board); err != nil {
			return err
		}
	}
	return nil
}

func lbTop(tx *sql.Tx, board string, n int) []lbRow {
	rows, err := tx.Query(`SELECT p.name, s.steam_id, s.points, s.wins, s.played FROM scores s JOIN players p USING (steam_id) `+
		`WHERE s.board = ? AND s.points > 0 ORDER BY s.points DESC, s.wins DESC, p.name COLLATE NOCASE LIMIT ?`, board, n)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []lbRow
	for rows.Next() {
		var r lbRow
		if rows.Scan(&r.name, &r.steam, &r.points, &r.wins, &r.played) == nil {
			out = append(out, r)
		}
	}
	return out
}

// lbRank is the player's place (ties share one), how many have points, and their row; ok is false without points.
func lbRank(tx *sql.Tx, board, steam string) (rank, total int, row lbRow, ok bool) {
	if tx.QueryRow(`SELECT points, wins, played FROM scores WHERE board=? AND steam_id=?`, board, steam).Scan(&row.points, &row.wins, &row.played) != nil || row.points <= 0 {
		return
	}
	var ahead int
	_ = tx.QueryRow(`SELECT COUNT(*) FROM scores WHERE board=? AND points > ?`, board, row.points).Scan(&ahead)
	_ = tx.QueryRow(`SELECT COUNT(*) FROM scores WHERE board=? AND points > 0`, board).Scan(&total)
	return ahead + 1, total, row, true
}

func lbScan(row *sql.Row) (lbBoard, bool) {
	var bd lbBoard
	var closed sql.NullFloat64
	if row.Scan(&bd.id, &bd.name, &bd.kind, &closed, &bd.version, &bd.postedVers) != nil {
		return bd, false
	}
	bd.closed = closed.Valid
	return bd, true
}

const lbBoardCols = `SELECT id, name, kind, closed_at, version, posted_version FROM boards `

// lbFind: a board by what a player types: general, week, event, a week number, an id or (the start of) a name,
// optionally followed by a game mode (jam, skate, spot): "week skate" is that mode's board of the week. A mode
// board nobody scored on yet comes back without a database row.
func lbFind(tx *sql.Tx, spec string, now time.Time) (lbBoard, bool) {
	spec = strings.ToLower(strings.TrimSpace(spec))
	words := make([]string, 0, len(lbModeWords))
	for w := range lbModeWords {
		words = append(words, w)
	}
	sort.Slice(words, func(i, j int) bool { return len(words[i]) > len(words[j]) })
	for _, w := range words {
		if spec == w || strings.HasSuffix(spec, " "+w) {
			base, ok := lbFindBase(tx, strings.TrimSuffix(spec, w), now)
			if !ok {
				return lbBoard{}, false
			}
			mode := lbModeWords[w]
			if bd, ok := lbScan(tx.QueryRow(lbBoardCols+`WHERE id=?`, base.id+":"+mode)); ok {
				return bd, true
			}
			return lbBoard{id: base.id + ":" + mode, name: base.name + " - " + lbModeNames[mode], kind: "mode", closed: base.closed}, true
		}
	}
	return lbFindBase(tx, spec, now)
}

func lbFindBase(tx *sql.Tx, spec string, now time.Time) (lbBoard, bool) {
	spec = strings.ToLower(strings.TrimSpace(spec))
	switch spec {
	case "", "general", "all", "all time", "alltime", "all-time", "g":
		return lbScan(tx.QueryRow(lbBoardCols + `WHERE id='general'`))
	case "year", "y", "yearly", "month", "m", "monthly", "week", "w", "weekly":
		kind := map[byte]string{'y': "year", 'm': "month", 'w': "week"}[spec[0]]
		id, _ := lbPeriod(kind, now)
		return lbScan(tx.QueryRow(lbBoardCols+`WHERE id=?`, id))
	case "event", "e":
		return lbScan(tx.QueryRow(lbBoardCols + `WHERE kind='event' ORDER BY closed_at IS NOT NULL, created_at DESC`))
	}
	if len(spec) == 4 && strings.Trim(spec, "0123456789") == "" {
		if bd, ok := lbScan(tx.QueryRow(lbBoardCols+`WHERE id=?`, "year-"+spec)); ok {
			return bd, true
		}
	}
	if m := lbWeekNumRe.FindStringSubmatch(spec); m != nil {
		n, _ := strconv.Atoi(m[1])
		_, _, year := lbWeek(now)
		if bd, ok := lbScan(tx.QueryRow(lbBoardCols+`WHERE id=?`, fmt.Sprintf("week-%d-%02d", year, n))); ok {
			return bd, true
		}
		if bd, ok := lbScan(tx.QueryRow(lbBoardCols+`WHERE kind='week' AND name=? ORDER BY created_at DESC`, fmt.Sprintf("Week %d", n))); ok {
			return bd, true
		}
	}
	for _, where := range []string{`lower(id) = ? OR lower(name) = ?`, `lower(id) LIKE ? || '%' OR lower(name) LIKE ? || '%'`} {
		if bd, ok := lbScan(tx.QueryRow(lbBoardCols+`WHERE kind != 'mode' AND (`+where+`) ORDER BY closed_at IS NOT NULL, created_at DESC`, spec, spec)); ok {
			return bd, true
		}
	}
	return lbBoard{}, false
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
		b.speeding, b.told, b.admins = map[string]bool{}, map[string]bool{}, map[string]bool{}
		b.free = freeTuning()
	} else if m := lbJoinedRe.FindStringSubmatch(text); m != nil {
		forget(m[1])
		b.online[m[1]] = m[2]
		b.admins[m[2]] = m[3] == "admin"
		b.waiting[m[1]] = time.Now().Add(rankCheckAfter)
	} else if m := leftRe.FindStringSubmatch(text); m != nil {
		forget(m[1])
		delete(b.admins, b.online[m[1]])
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
		b.score(m[1], lbResults(m[1], m[2]))
	} else if m := lbTuningRe.FindStringSubmatch(text); m != nil {
		b.free = m[1] == "their"
		for name := range b.online {
			b.tell(name)
		}
	} else if m := lbCommandRe.FindStringSubmatch(text); m != nil {
		b.command(m[1])
	}
}

func (b *leaderboard) isRanked(name string) bool {
	if b.league == "Unranked" { // nothing is required here, so everybody scores (on the Unranked boards)
		return true
	}
	_, modded := b.modded[name]
	return !modded && !b.speeding[name] && !b.free
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
	if b.league == "Unranked" {
		text = unrankedServer
	} else if !ranked {
		if mods, ok := b.modded[name]; ok {
			if mods == "" {
				mods = "your mods"
			}
			text = strings.ReplaceAll(b.unranked, "{mods}", mods)
		} else if b.free {
			text = b.tuning
		} else {
			text = b.speed
		}
	}
	b.send(fit("msg "+id+" "+text, chatMax+30))
}

func (b *leaderboard) score(mode string, results []lbResult) {
	var finished []lbResult
	for _, r := range results {
		if !r.quit {
			finished = append(finished, r)
		}
	}
	if len(finished) < 2 || len(b.points) == 0 {
		return
	}
	if mode == "S.K.A.T.E." { // no places in the log: fewest missed, then most landed; equal results share a place
		placed := make([]lbResult, len(finished))
		for i, r := range finished {
			r.place = 1
			for _, o := range finished {
				if o.missed < r.missed || o.missed == r.missed && o.landed > r.landed {
					r.place++
				}
			}
			placed[i] = r
		}
		finished = placed
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
		won := 0
		if r.place == 1 {
			won = 1
		}
		awards = append(awards, lbAward{key, r.name, b.points[i], won, 1})
	}
	if len(awards) == 0 {
		return
	}
	now := time.Now()
	var boards []string
	if !b.run(func(tx *sql.Tx) error {
		boards = b.active(tx, now)
		if game := lbModeOf[mode]; game != "" { // each board also has one per game mode
			for _, id := range append([]string{}, boards...) {
				var name string
				_ = tx.QueryRow(`SELECT name FROM boards WHERE id=?`, id).Scan(&name)
				lbBoardInsert(tx, id+":"+game, name+" - "+lbModeNames[game], "mode")
				boards = append(boards, id+":"+game)
			}
		}
		return b.add(tx, boards, awards, mode, now)
	}) {
		return
	}
	var parts []string
	for _, a := range awards {
		parts = append(parts, fmt.Sprintf("%s +%d", a.name, a.points))
	}
	fmt.Println("[leaderboard] " + strings.Join(parts, ", ") + " (" + strings.Join(boards, ", ") + ")")
}

// ---- chat commands -----------------------------------------------------------------------

// command answers "Name /top week" (the text after "[command] "). The name is whoever is online and
// fits. If the line could also be another player's typed text in the colon form ("Other: /lb ..."),
// someone named to look like it must not get a say, so the line is dropped.
func (b *leaderboard) command(rest string) {
	for other := range b.online {
		if strings.HasPrefix(rest, other+": /") {
			return
		}
	}
	name := ""
	for n := range b.online {
		if strings.HasPrefix(rest, n+" /") && len(n) > len(name) {
			name = n
		}
	}
	if name == "" {
		return
	}
	verb, arg, _ := strings.Cut(rest[len(name)+2:], " ")
	verb, arg = strings.ToLower(verb), strings.TrimSpace(arg)
	steam := b.online[name]
	now := time.Now()
	if verb != "top" && verb != "rank" && verb != "boards" && verb != "props" && verb != "lb" || now.Sub(b.asked[steam]) < lbAskEvery {
		return
	}
	b.asked[steam] = now
	title, lines := "Leaderboard", []string(nil)
	switch verb {
	case "top":
		title, lines = b.cmdTop(arg, now)
	case "rank":
		title, lines = b.cmdRank(steam, arg, now)
	case "boards":
		title, lines = b.cmdBoards(now)
	case "props":
		title, lines = b.cmdProps(name, steam, arg, now)
	case "lb":
		if !b.admins[steam] {
			return // the server logs only allowed commands; belt and braces
		}
		title, lines = "Leaderboard admin", b.admin(name, arg, now)
	}
	// after the server's own rule: the board, then one row per line
	title += " (" + b.league + ")"
	for _, l := range append([]string{title}, lines...) {
		b.send(fit("msg "+steam+" "+l, chatMax+30))
	}
}

// pack: chat lines of at most chatMax bytes, head first, then the entries side by side.
func lbPack(head string, entries []string) []string {
	var lines []string
	line := head
	for _, e := range entries {
		if line == "" {
			line = e
		} else if len(line)+2+len(e) > chatMax {
			lines = append(lines, line)
			line = e
		} else {
			line += "  " + e
		}
	}
	if line == "" {
		return lines
	}
	return append(lines, line)
}

// lbRule is the server's own reply to a chat command: the line that opens every answer.
const lbRule = "------------------------------"

const lbUnavailable = "The leaderboard is not available right now."

func (b *leaderboard) cmdTop(arg string, now time.Time) (string, []string) {
	title, lines := "Leaderboard", []string{lbUnavailable}
	b.run(func(tx *sql.Tx) error {
		bd, ok := lbFind(tx, arg, now)
		if !ok {
			lines = []string{"No board called '" + arg + "'. Try /boards."}
			return nil
		}
		title = "Top - " + bd.name
		rows := lbTop(tx, bd.id, b.size)
		if len(rows) == 0 {
			lines = []string{"Nobody has points yet."}
			return nil
		}
		title = fmt.Sprintf("Top %d - %s", len(rows), bd.name)
		lines = nil
		for i, r := range rows {
			lines = append(lines, fmt.Sprintf("%d. %s  %d", i+1, r.name, r.points))
		}
		return nil
	})
	return title, lines
}

func (b *leaderboard) cmdRank(steam, arg string, now time.Time) (string, []string) {
	title, lines := "Leaderboard", []string{lbUnavailable}
	b.run(func(tx *sql.Tx) error {
		bd, ok := lbFind(tx, arg, now)
		if !ok {
			lines = []string{"No board called '" + arg + "'. Try /boards."}
			return nil
		}
		title = "Your rank - " + bd.name
		rank, total, row, ok := lbRank(tx, bd.id, steam)
		if !ok {
			lines = []string{"You have no points yet. Finish a ranked throwdown with two or more players."}
			return nil
		}
		lines = []string{fmt.Sprintf("You are #%d of %d with %d points (%d won / %d played).", rank, total, row.points, row.wins, row.played)}
		return nil
	})
	return title, lines
}

func (b *leaderboard) cmdBoards(now time.Time) (string, []string) {
	lines := []string{lbUnavailable}
	title := "Leaderboard"
	b.run(func(tx *sql.Tx) error {
		b.active(tx, now)
		rows, err := tx.Query(`SELECT b.name, b.closed_at IS NOT NULL, (SELECT COUNT(*) FROM scores s WHERE s.board = b.id AND s.points > 0) ` +
			`FROM boards b WHERE b.kind != 'mode' ORDER BY b.kind='general' DESC, b.closed_at IS NOT NULL, b.created_at DESC LIMIT 12`)
		if err != nil {
			return err
		}
		defer rows.Close()
		var entries []string
		for rows.Next() {
			var name string
			var closed bool
			var n int
			if rows.Scan(&name, &closed, &n) == nil {
				end := ""
				if closed {
					end = " (ended)"
				}
				entries = append(entries, fmt.Sprintf("%s%s  -  %d players", name, end, n))
			}
		}
		title = "Boards"
		lines = entries
		return nil
	})
	return title, lines
}

// lbFindOnline: the online player called text: the exact name, else the only name starting with it.
func (b *leaderboard) findOnline(text string) (name, id string) {
	text = strings.ToLower(strings.TrimSpace(text))
	if text == "" {
		return "", ""
	}
	var exact, starts [][2]string
	for n, i := range b.online {
		l := strings.ToLower(n)
		if l == text {
			exact = append(exact, [2]string{n, i})
		}
		if strings.HasPrefix(l, text) {
			starts = append(starts, [2]string{n, i})
		}
	}
	found := exact
	if len(found) == 0 {
		found = starts
	}
	if len(found) != 1 {
		return "", ""
	}
	return found[0][0], found[0][1]
}

// cmdProps gives a prop to another player online: one per player per 24 hours, at most LEADERBOARD_PROPS_PER_DAY
// a day. Props count on the Props boards (/top props), not on the points boards.
func (b *leaderboard) cmdProps(giver, steam, arg string, now time.Time) (string, []string) {
	const title = "Props"
	if b.propsPerDay == 0 {
		return title, []string{"Props are off on this server."}
	}
	if strings.TrimSpace(arg) == "" {
		have := "You have no props yet."
		b.run(func(tx *sql.Tx) error {
			if bd, ok := lbFind(tx, "props", now); ok {
				if rank, total, row, ok := lbRank(tx, bd.id, steam); ok {
					have = fmt.Sprintf("You have %d props (#%d of %d).", row.points, rank, total)
				}
			}
			return nil
		})
		return title, []string{have, "Give them with /props <player>. See /top props."}
	}
	name, target := b.findOnline(arg)
	if target == "" {
		return title, []string{"Nobody online is called '" + arg + "' (or several names start like that)."}
	}
	if target == steam {
		return title, []string{"You can't give props to yourself."}
	}
	state, used, total := "", 0, 0
	if !b.run(func(tx *sql.Tx) error {
		since := unixf(now) - 86400
		_ = tx.QueryRow(`SELECT COUNT(*) FROM props_log WHERE giver=? AND at>?`, steam, since).Scan(&used)
		if used >= b.propsPerDay {
			state = "limit"
			return nil
		}
		var one int
		if tx.QueryRow(`SELECT 1 FROM props_log WHERE giver=? AND receiver=? AND at>?`, steam, target, since).Scan(&one) == nil {
			state = "again"
			return nil
		}
		var boards []string
		for _, base := range b.active(tx, now) {
			var baseName string
			_ = tx.QueryRow(`SELECT name FROM boards WHERE id=?`, base).Scan(&baseName)
			lbBoardInsert(tx, base+":props", baseName+" - Props", "mode")
			boards = append(boards, base+":props")
		}
		if err := b.add(tx, boards, []lbAward{{target, name, 1, 0, 0}}, "props:"+giver, now); err != nil {
			return err
		}
		if _, err := tx.Exec(`INSERT INTO props_log (at, giver, receiver) VALUES (?, ?, ?)`, unixf(now), steam, target); err != nil {
			return err
		}
		used++
		total = 1
		_ = tx.QueryRow(`SELECT points FROM scores WHERE board=? AND steam_id=?`, boards[0], target).Scan(&total)
		state = "ok"
		return nil
	}) {
		return title, []string{lbUnavailable}
	}
	switch state {
	case "limit":
		return title, []string{fmt.Sprintf("You have given %d props in the last 24 hours, the most for now.", used)}
	case "again":
		return title, []string{"You gave " + name + " props in the last 24 hours already."}
	}
	b.send(fit(fmt.Sprintf("msg %s %s gave you props! You have %d now (/top props).", target, giver, total), chatMax+30))
	if b.propsAnnounce {
		b.send("say " + fit(giver+" gave props to "+name+"!", chatMax))
	}
	return title, []string{fmt.Sprintf("You gave props to %s. %d left in the next 24 hours.", name, b.propsPerDay-used)}
}

func (b *leaderboard) admin(by, arg string, now time.Time) []string {
	word, rest, _ := strings.Cut(arg, " ")
	word, rest = strings.ToLower(word), strings.TrimSpace(rest)
	switch word {
	case "award":
		return b.adminAward(by, rest, now)
	case "event":
		return b.adminEvent(rest, now)
	}
	return []string{"/lb award <points> <player> [board:<board>] | /lb event start <name> | /lb event end [name]"}
}

func (b *leaderboard) adminAward(by, rest string, now time.Time) []string {
	m := lbAwardRe.FindStringSubmatch(rest)
	if m == nil {
		return []string{"Usage: /lb award <points> <player or SteamID64> [board:<board>]"}
	}
	points, _ := strconv.Atoi(m[1])
	who, spec := strings.TrimSpace(m[2]), m[3]
	steam := ""
	if lbSteamRe.MatchString(who) {
		steam = who
	} else {
		for n, id := range b.online {
			if strings.EqualFold(n, who) {
				steam = id
			}
		}
	}
	reply := "The leaderboard could not be changed."
	if !b.run(func(tx *sql.Tx) error {
		sid, name := steam, who
		if sid == "" {
			rows, err := tx.Query(`SELECT steam_id, name FROM players WHERE name = ? COLLATE NOCASE`, who)
			if err != nil {
				return err
			}
			var ids, names []string
			for rows.Next() {
				var i, n string
				if rows.Scan(&i, &n) == nil {
					ids, names = append(ids, i), append(names, n)
				}
			}
			rows.Close()
			if len(ids) != 1 {
				reply = "Player not found: use the name of someone online or a SteamID64."
				if len(ids) > 1 {
					reply = "Several players have that name: use the SteamID64."
				}
				return nil
			}
			sid, name = ids[0], names[0]
		} else {
			name = sid
			_ = tx.QueryRow(`SELECT name FROM players WHERE steam_id = ?`, sid).Scan(&name)
			for n, id := range b.online {
				if id == sid {
					name = n
				}
			}
		}
		boards := b.active(tx, now)
		if spec != "" {
			bd, ok := lbFind(tx, spec, now)
			if !ok || bd.closed || bd.kind == "mode" {
				reply = "No open board called '" + spec + "' (game mode boards only get throwdown points)."
				return nil
			}
			boards = []string{bd.id}
		}
		if err := b.add(tx, boards, []lbAward{{sid, name, points, 0, 0}}, "admin:"+by, now); err != nil {
			return err
		}
		reply = fmt.Sprintf("Gave %+d points to %s on %d board(s).", points, name, len(boards))
		return nil
	}) {
		return []string{reply}
	}
	fmt.Printf("[leaderboard] %s: award %+d to %s: %s\n", by, points, who, reply)
	return []string{reply}
}

func (b *leaderboard) adminEvent(rest string, now time.Time) []string {
	word, event, _ := strings.Cut(rest, " ")
	word, event = strings.ToLower(word), strings.TrimSpace(event)
	if !b.kinds["event"] {
		return []string{"Events are off (LEADERBOARD_BOARDS)."}
	}
	slug := func(s string) string {
		s = strings.Trim(lbSlugRe.ReplaceAllString(strings.ToLower(s), "-"), "-")
		return s[:min(len(s), 32)]
	}
	switch word {
	case "start":
		name := lbSpaceRe.ReplaceAllString(event, " ")
		if len(name) > 40 {
			name = name[:40]
		}
		if slug(name) == "" {
			return []string{"Usage: /lb event start <name>"}
		}
		reply, started := "The leaderboard could not be changed.", false
		b.run(func(tx *sql.Tx) error {
			id := "event-" + slug(name)
			var closed sql.NullFloat64
			if err := tx.QueryRow(`SELECT closed_at FROM boards WHERE id=?`, id).Scan(&closed); err == nil {
				if !closed.Valid {
					reply = "Event '" + name + "' is already running."
					return nil
				}
				id += "-" + now.UTC().Format("20060102") // same name again: a board of its own
				var one int
				if tx.QueryRow(`SELECT 1 FROM boards WHERE id=?`, id).Scan(&one) == nil {
					reply = "An event '" + name + "' already ran today; pick another name."
					return nil
				}
			}
			lbBoardInsert(tx, id, name, "event")
			reply, started = "Event '"+name+"' started: every ranked result now counts there as well. End it with /lb event end.", true
			return nil
		})
		if started {
			b.send("say " + fit("Leaderboard event '"+name+"' has started! Ranked throwdown results count for it too: /top event", chatMax))
		}
		return []string{reply}
	case "end":
		reply := "The leaderboard could not be changed."
		var ended *lbBoard
		b.run(func(tx *sql.Tx) error {
			q, args := lbBoardCols+`WHERE kind='event' AND closed_at IS NULL`, []any{}
			if event != "" {
				q, args = q+` AND (lower(name) = lower(?) OR id = ?)`, []any{event, "event-" + slug(event)}
			}
			rows, err := tx.Query(q, args...)
			if err != nil {
				return err
			}
			var found []lbBoard
			for rows.Next() {
				var bd lbBoard
				var closed sql.NullFloat64
				if rows.Scan(&bd.id, &bd.name, &bd.kind, &closed, &bd.version, &bd.postedVers) == nil {
					found = append(found, bd)
				}
			}
			rows.Close()
			switch {
			case len(found) == 0 && event != "":
				reply = "No such event is running."
			case len(found) == 0:
				reply = "No event is running."
			case len(found) > 1:
				reply = "Several events are running: /lb event end <name>"
			default:
				if _, err := tx.Exec(`UPDATE boards SET closed_at = ?, version = version + 1 WHERE id = ?`, unixf(now), found[0].id); err != nil {
					return err
				}
				ended, reply = &found[0], "Event '"+found[0].name+"' ended."
			}
			return nil
		})
		if ended != nil {
			b.final(*ended)
		}
		return []string{reply}
	}
	return []string{"Usage: /lb event start <name> | /lb event end [name]"}
}

// final: the result of a finished event goes to chat and Discord.
func (b *leaderboard) final(bd lbBoard) {
	var rows []lbRow
	b.run(func(tx *sql.Tx) error { rows = lbTop(tx, bd.id, 10); return nil })
	if len(rows) == 0 {
		return
	}
	var entries []string
	for i, r := range rows[:min(3, len(rows))] {
		entries = append(entries, fmt.Sprintf("%d. %s %d", i+1, r.name, r.points))
	}
	for _, l := range lbPack("Event '"+bd.name+"' is over:", entries) {
		b.send("say " + fit(l, chatMax))
	}
	if b.post != nil {
		b.post("**Event finished: " + bd.name + "**\n```\n" + lbTable(rows, false) + "\n```")
	}
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
	for kind := range b.kinds {
		every := b.chatEvery[kind]
		last, ok := b.chatAt[kind]
		if !ok {
			last = b.started
		}
		if every > 0 && now.Sub(last) >= every {
			b.chatAt[kind] = now
			if playersOn {
				b.postChat(now, kind)
			}
		}
	}
	if b.post != nil && now.Sub(b.postAt) >= time.Minute {
		b.postAt = now
		b.postDiscord(now)
	}
}

type lbShown struct {
	board lbBoard
	rows  []lbRow
}

// current: the boards shown now (All Time, this week, the open events) with their top 10. For Discord
// (LEADERBOARD_DISCORD) only the chosen ones, each followed by its chosen game boards.
func (b *leaderboard) current(tx *sql.Tx, now time.Time, discord bool) []lbShown {
	var out []lbShown
	for _, id := range b.active(tx, now) {
		kind, _, _ := strings.Cut(id, "-")
		if discord && !b.discordScopes[kind] {
			continue
		}
		ids := []string{id}
		if discord && len(b.discordGames) > 0 {
			if rows, err := tx.Query(`SELECT id FROM boards WHERE kind='mode' AND id LIKE ? ORDER BY id`, id+":%"); err == nil {
				for rows.Next() {
					var m string
					if rows.Scan(&m) == nil && b.discordGames[m[strings.LastIndex(m, ":")+1:]] {
						ids = append(ids, m)
					}
				}
				rows.Close()
			}
		}
		for _, shown := range ids {
			if bd, ok := lbScan(tx.QueryRow(lbBoardCols+`WHERE id=?`, shown)); ok {
				out = append(out, lbShown{bd, lbTop(tx, shown, 10)})
			}
		}
	}
	return out
}

func (b *leaderboard) postChat(now time.Time, kind string) {
	var shown []lbShown
	b.run(func(tx *sql.Tx) error { shown = b.current(tx, now, false); return nil })
	first := true
	for _, s := range shown {
		rows := s.rows[:min(len(s.rows), b.size)]
		if len(rows) == 0 || s.board.kind != kind {
			continue
		}
		var entries []string
		for i, p := range rows {
			entries = append(entries, fmt.Sprintf("%d. %s %d", i+1, p.name, p.points))
		}
		if first {
			b.send("announce " + fit(s.board.name+": "+strings.Join(entries[:min(3, len(entries))], " | "), chatMax))
			first = false
		}
		head := s.board.name + ":"
		if s.board.kind == "general" {
			head = "Leaderboard (" + b.label + ", " + strings.ToLower(b.league) + " throwdowns):"
		}
		for _, l := range lbPack(head, entries) {
			b.send("say " + fit(l, chatMax))
		}
	}
}

func lbTable(rows []lbRow, props bool) string {
	width := 0
	for _, p := range rows {
		width = max(width, len(p.name))
	}
	var lines []string
	for i, p := range rows {
		if props {
			lines = append(lines, fmt.Sprintf("%2d. %-*s  %5d props", i+1, width, p.name, p.points))
			continue
		}
		lines = append(lines, fmt.Sprintf("%2d. %-*s  %5d pts  %d won / %d played", i+1, width, p.name, p.points, p.wins, p.played))
	}
	return strings.ReplaceAll(strings.Join(lines, "\n"), "```", "'''")
}

func (b *leaderboard) postDiscord(now time.Time) {
	var shown []lbShown
	b.run(func(tx *sql.Tx) error {
		all := b.current(tx, now, true)
		late := map[string]bool{} // the kinds whose interval is over and that have news
		kinds := map[string]bool{}
		for _, s := range all {
			kinds[lbKindOf(s.board.id)] = true
		}
		for kind := range kinds {
			every := b.discordEvery[kind]
			news := false
			for _, s := range all {
				news = news || lbKindOf(s.board.id) == kind && s.board.version != s.board.postedVers
			}
			var at string
			due := true // another server's post counts: the file is shared
			if tx.QueryRow(`SELECT value FROM meta WHERE key=?`, "posted_at:"+kind).Scan(&at) == nil {
				if v, err := strconv.ParseFloat(at, 64); err == nil && unixf(now)-v < every.Seconds() {
					due = false
				}
			}
			if every > 0 && news && due {
				late[kind] = true
			}
		}
		for kind := range late {
			if _, err := tx.Exec(`INSERT OR REPLACE INTO meta VALUES (?, ?)`, "posted_at:"+kind, strconv.FormatFloat(unixf(now), 'f', 3, 64)); err != nil {
				return err
			}
		}
		for _, s := range all {
			if late[lbKindOf(s.board.id)] {
				if _, err := tx.Exec(`UPDATE boards SET posted_version = version WHERE id = ?`, s.board.id); err != nil {
					return err
				}
				shown = append(shown, s)
			}
		}
		return nil
	})
	var parts []string
	for _, s := range shown {
		if len(s.rows) == 0 {
			continue
		}
		n := 5
		if s.board.kind == "general" {
			n = 10
		}
		isProps := strings.HasSuffix(s.board.id, ":props")
		what := strings.ToLower(b.league) + " throwdowns"
		if isProps {
			what = "props from players"
		}
		parts = append(parts, "**"+s.board.name+"** ("+b.label+", "+what+")\n```\n"+lbTable(s.rows[:min(len(s.rows), n)], isProps)+"\n```")
	}
	message := ""
	for _, part := range parts { // Discord takes 2000 characters per message
		if message != "" && len(message)+len(part)+1 > 1900 {
			b.post(message)
			message = ""
		}
		if message != "" {
			message += "\n"
		}
		message += part
	}
	if message != "" {
		b.post(message)
	}
}

func (b *leaderboard) loop() {
	for {
		func() {
			defer func() {
				if r := recover(); r != nil {
					fmt.Printf("[leaderboard] %v\n", r)
				}
			}()
			if !external("leaderboard", "") { // off while the leaderboard module outside runs it
				b.tick()
			}
		}()
		time.Sleep(time.Second)
	}
}

// lbResults reads the results of a finished line: "1. Anna 12,345, 2. Ben 9,876 (quit)" or,
// for S.K.A.T.E., "Anna 7 landed / 3 missed, Ben 2 landed / 5 missed (quit)".
func lbResults(mode, results string) []lbResult {
	var out []lbResult
	if mode == "S.K.A.T.E." {
		start := 0
		for _, m := range lbTriedRe.FindAllStringSubmatchIndex(results, -1) {
			landed, _ := strconv.Atoi(results[m[2]:m[3]])
			missed, _ := strconv.Atoi(results[m[4]:m[5]])
			out = append(out, lbResult{name: results[start:m[0]], quit: m[6] >= 0, missed: missed, landed: landed})
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
