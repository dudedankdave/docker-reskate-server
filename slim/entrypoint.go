package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

const configPath = "/data/ReSkateServer.json"

func env(name string) (string, bool) {
	v := strings.TrimSpace(os.Getenv(name))
	return v, v != ""
}

type conv func(name, v string) any

func asBool(name, v string) bool {
	switch strings.ToLower(v) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	}
	die("%s: expected true/false, got %q", name, v)
	return false
}

func asInt(name, v string) int {
	n, err := strconv.Atoi(v)
	if err != nil {
		die("%s: expected a number, got %q", name, v)
	}
	return n
}

func asList(v string) []string {
	var out []string
	for _, item := range strings.Split(v, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

func choice(allowed ...string) conv {
	return func(name, v string) any {
		l := strings.ToLower(v)
		for _, a := range allowed {
			if l == a {
				return l
			}
		}
		die("%s: expected one of %s, got %q", name, strings.Join(allowed, ", "), v)
		return nil
	}
}

func text(_, v string) any { return v }

// "off" (or "none") clears a value, since empty means "leave alone".
var colorRe = regexp.MustCompile(`^#[0-9A-Fa-f]{6}$`)

func color(name, v string) any {
	if !colorRe.MatchString(v) {
		die("%s: expected a colour like #8E5CFF, got %q", name, v)
	}
	return strings.ToUpper(v)
}

func clearable(_, v string) any {
	if l := strings.ToLower(v); l == "off" || l == "none" {
		return ""
	}
	return v
}
func nonNegative(name, v string) any {
	n := asInt(name, v)
	if n < 0 {
		die("%s: expected 0 (off) or a positive number, got %q", name, v)
	}
	return n
}

// ReSkate only accepts 1-64 ASCII letters, digits, spaces and - _ / [ ] ( ) as a name and
// refuses to start otherwise. Clean a name it would reject (and say so) instead of crash-looping.
var (
	nameOK    = regexp.MustCompile(`^[A-Za-z0-9 _/\[\]()-]{1,64}$`)
	nameDrop  = regexp.MustCompile(`['\x{2019}.#%]`)
	nameBad   = regexp.MustCompile(`[^A-Za-z0-9 _/\[\]()-]`)
	nameDash  = regexp.MustCompile(`\s*-(?:\s*-)+\s*`)
	nameSpace = regexp.MustCompile(`\s+`)
	accents   = strings.NewReplacer(
		"ä", "a", "ö", "o", "ü", "u", "Ä", "A", "Ö", "O", "Ü", "U", "ß", "ss",
		"é", "e", "è", "e", "ê", "e", "ë", "e", "É", "E", "È", "E", "Ê", "E", "Ë", "E",
		"á", "a", "à", "a", "â", "a", "ã", "a", "å", "a", "Á", "A", "À", "A", "Â", "A", "Ã", "A", "Å", "A",
		"í", "i", "ì", "i", "î", "i", "ï", "i", "Í", "I", "Ì", "I", "Î", "I", "Ï", "I",
		"ó", "o", "ò", "o", "ô", "o", "õ", "o", "ø", "o", "Ó", "O", "Ò", "O", "Ô", "O", "Õ", "O", "Ø", "O",
		"ú", "u", "ù", "u", "û", "u", "Ú", "U", "Ù", "U", "Û", "U", "ý", "y", "ÿ", "y", "Ý", "Y",
		"ç", "c", "Ç", "C", "ñ", "n", "Ñ", "N", "æ", "ae", "Æ", "AE", "œ", "oe", "Œ", "OE")
)

func cleanName(v string) string {
	s := accents.Replace(v)
	s = nameDrop.ReplaceAllString(s, "")
	s = nameBad.ReplaceAllString(s, "-")
	s = nameDash.ReplaceAllString(s, " - ")
	s = strings.Trim(nameSpace.ReplaceAllString(s, " "), " -")
	if len(s) > 64 {
		s = strings.TrimRight(s[:64], " -")
	}
	if s == "" {
		s = "ReSkate server"
	}
	return s
}

func serverName(name, v string) any {
	if nameOK.MatchString(v) {
		return v
	}
	clean := cleanName(v)
	fmt.Printf("[config] %s %q is not accepted by ReSkate (1-64 letters, numbers, spaces and - _ / [ ] ( ) only), using %q instead\n", name, v, clean)
	return clean
}

func intRange(lo, hi int) conv {
	return func(name, v string) any {
		n := asInt(name, v)
		if n < lo || n > hi {
			die("%s: expected a number from %d to %d, got %q", name, lo, hi, v)
		}
		return n
	}
}

func boolConv(name, v string) any { return asBool(name, v) }
func intConv(name, v string) any  { return asInt(name, v) }

// sub returns cfg[key] as an object, creating it when missing.
func sub(cfg map[string]any, key string) map[string]any {
	if m, ok := cfg[key].(map[string]any); ok {
		return m
	}
	m := map[string]any{}
	cfg[key] = m
	return m
}

func asString(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case json.Number:
		return t.String()
	}
	return fmt.Sprint(v)
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// Maps the server knows without a mod (ReSkate's own list); anything else must come from an installed mod.
var builtinMaps = []string{"San Vansterdam", "Isle of Grom", "Super Ultra Mega Resort", "Stadium 1"}

// warnUnknownMaps: the server refuses to start on an unknown map. Say which names are available before it does.
func warnUnknownMaps(cfg map[string]any) {
	provided := map[string]string{}
	entries, _ := os.ReadDir("/data/Mods")
	for _, e := range entries {
		var lv struct {
			Levels []struct {
				DisplayName string `json:"displayName"`
			} `json:"levels"`
		}
		if readJSON("/data/Mods/"+e.Name()+"/reskate-levels.json", &lv) {
			for _, l := range lv.Levels {
				if l.DisplayName != "" {
					provided[l.DisplayName] = e.Name()
				}
			}
		}
	}
	known := map[string]bool{}
	var names []string
	for _, m := range builtinMaps {
		known[strings.ToLower(m)] = true
	}
	for m := range provided {
		known[strings.ToLower(m)] = true
		names = append(names, m)
	}
	sort.Strings(names)
	type want struct{ v, name string }
	var wanted []want
	maps, _ := cfg["maps"].(map[string]any)
	if m, ok := maps["map"].(string); ok && m != "" {
		wanted = append(wanted, want{"MAP", m})
	}
	if pool, ok := maps["pool"].([]any); ok {
		for _, m := range pool {
			wanted = append(wanted, want{"MAP_POOL", fmt.Sprint(m)})
		}
	}
	installed := "none"
	if len(names) > 0 {
		installed = strings.Join(names, ", ")
	}
	for _, w := range wanted {
		if !known[strings.ToLower(w.name)] {
			fmt.Printf("[maps] WARNING: %s %q is not a built-in map and no installed mod provides it, the server will refuse to start (Config problem). Installed mod maps: %s. Built-in maps: %s.\n",
				w.v, w.name, installed, strings.Join(builtinMaps, ", "))
		}
	}
}

var bansPath = filepath.Join(filepath.Dir(configPath), "data", "bans.json")

// layout: section -> {key, key before 1.1.7}. ReSkate 1.1.7 put the settings into sections and renamed several.
var layout = []struct {
	section string
	keys    [][2]string
}{
	{"server", [][2]string{{"name", "name"}, {"password", "password"}, {"welcome_message", "welcome"},
		{"listed", "listed"}, {"max_players", "max_players"}, {"port", "port"},
		{"query_port", "query_port"}, {"steam_token", "steam_token"},
		{"auto_update", "auto_update"}, {"activity_log", "activity_log"}}},
	{"access", [][2]string{{"admins", "admins"}, {"reserved_players_slots", "reserved"},
		{"use_global_bans", "global_bans"}}},
	{"maps", [][2]string{{"map", "map"}, {"pool", "map_pool"}, {"rotation_minutes", "map_rotation_minutes"},
		{"parks", "parks"}, {"world_layer_sync", "world_layer_sync"}, {"layers", "layers"}}},
	{"players", [][2]string{{"allow_boosts", "boosts"}, {"allow_no_bail", "no_bail"}, {"allow_noclip", "noclip"},
		{"allow_parties", "parties"}, {"party_size", "party_size"},
		{"allow_voice_chat", "voice_chat"}, {"voice_range", "voice_range"},
		{"object_placement", "object_placement"}, {"object_limit", "object_limit"},
		{"announce_throwdowns", "announce_throwdowns"}}},
	{"anti_cheat", [][2]string{{"speed_hack", "speed_check"}, {"modified_scoring", "score_check"},
		{"allowed_scoring_mods", "score_allow"}, {"enforce_tuning", "enforce_tuning"},
		{"bone_scale_limit", "bone_scale_limit"}}},
	{"network", [][2]string{{"send_rate", "send_rate"}, {"crowd_budget", "crowd_budget"}, {"distances", "distances"}}},
}

// removed: settings gone in 1.1.7 -> their env var.
var removed = [][2]string{{"tps", "TPS"}, {"reserved_slots", "RESERVED_SLOTS"}}

func loadBans() []any {
	raw, err := os.ReadFile(bansPath)
	if err != nil {
		return []any{}
	}
	var bans []any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if dec.Decode(&bans) != nil || bans == nil {
		return []any{}
	}
	return bans
}

func mergeBans(bans, add []any) []any {
	var known []string
	for _, b := range bans {
		if m, ok := b.(map[string]any); ok {
			known = append(known, asString(m["id"]))
		}
	}
	for _, b := range add {
		if m, ok := b.(map[string]any); ok && !contains(known, asString(m["id"])) {
			known = append(known, asString(m["id"]))
			bans = append(bans, b)
		}
	}
	return bans
}

func writeJSON(path string, v any) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		die("cannot encode %s: %v", path, err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		die("cannot create %s: %v", filepath.Dir(path), err)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, buf.Bytes(), 0o644); err != nil {
		die("cannot write %s: %v", tmp, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		die("cannot replace %s: %v", path, err)
	}
}

// migrate moves pre-1.1.7 top-level settings into their sections (a value already in a section wins).
func migrate(cfg map[string]any) {
	moved := false
	for _, l := range layout {
		for _, k := range l.keys {
			if v, ok := cfg[k[1]]; ok { // no old name is also a section name
				sec := sub(cfg, l.section)
				if _, ok := sec[k[0]]; !ok {
					sec[k[0]] = v
				}
				delete(cfg, k[1])
				moved = true
			}
		}
	}
	for _, r := range removed {
		if _, ok := cfg[r[0]]; ok {
			delete(cfg, r[0])
			moved = true
		}
	}
	if b, ok := cfg["bans"]; ok {
		old, _ := b.([]any)
		writeJSON(bansPath, mergeBans(loadBans(), old))
		delete(cfg, "bans")
		moved = true
	}
	if moved {
		fmt.Println("[config] moved the settings to the sectioned layout of ReSkate 1.1.7+")
	}
}

// mergeList appends the env list to cfg[section][key], keeping what is there (in-game additions).
func mergeList(cfg map[string]any, section, key, v string, lower bool) {
	norm := func(s string) string {
		if lower {
			return strings.ToLower(s)
		}
		return s
	}
	sec := sub(cfg, section)
	var have []string
	old, _ := sec[key].([]any)
	for _, a := range old {
		have = append(have, norm(asString(a)))
	}
	for _, a := range asList(v) {
		if a = norm(a); !contains(have, a) {
			have = append(have, a)
		}
	}
	out := make([]any, len(have))
	for i, a := range have {
		out[i] = a
	}
	sec[key] = out
}

func entrypointMain(serverArgs []string) {
	if err := os.MkdirAll("/data/Mods", 0o755); err != nil {
		die("cannot create /data/Mods: %v", err)
	}
	cfg := map[string]any{}
	if raw, err := os.ReadFile(configPath); err == nil {
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.UseNumber()
		if err := dec.Decode(&cfg); err != nil {
			die("%s: %v", configPath, err)
		}
	}
	migrate(cfg)

	simple := []struct {
		env, section, key string
		c                 conv
	}{
		{"SERVER_NAME", "server", "name", serverName},
		{"SERVER_PASSWORD", "server", "password", clearable},
		{"WELCOME_MESSAGE", "server", "welcome_message", clearable},
		{"LISTED", "server", "listed", boolConv},
		{"MAX_PLAYERS", "server", "max_players", intRange(1, 249)},
		{"PORT", "server", "port", intConv},
		{"QUERY_PORT", "server", "query_port", intConv},
		{"STEAM_TOKEN", "server", "steam_token", clearable},
		{"AUTO_UPDATE", "server", "auto_update", boolConv},
		{"ACTIVITY_LOG", "server", "activity_log", boolConv},
		{"CHAT_COLOR", "server", "chat_color", color},
		{"CHAT_TEXT_COLOR", "server", "chat_text_color", color},
		{"GLOBAL_BANS", "access", "use_global_bans", boolConv},
		{"MAP", "maps", "map", text},
		{"MAP_ROTATION_MINUTES", "maps", "rotation_minutes", nonNegative},
		{"WORLD_LAYER_SYNC", "maps", "world_layer_sync", boolConv},
		{"BOOSTS", "players", "allow_boosts", boolConv},
		{"NO_BAIL", "players", "allow_no_bail", boolConv},
		{"NOCLIP", "players", "allow_noclip", boolConv},
		{"PARTIES", "players", "allow_parties", boolConv},
		{"PARTY_SIZE", "players", "party_size", intConv},
		{"VOICE_CHAT", "players", "allow_voice_chat", boolConv},
		{"VOICE_RANGE", "players", "voice_range", intRange(50, 1000)},
		{"OBJECT_PLACEMENT", "players", "object_placement", choice("everyone", "admins", "nobody")},
		{"OBJECT_LIMIT", "players", "object_limit", intRange(0, 1024)},
		{"ANNOUNCE_THROWDOWNS", "players", "announce_throwdowns", boolConv},
		{"AFK_KICK_MINUTES", "players", "afk_kick_minutes", intRange(0, 1440)},
		{"OBJECT_SCALING", "players", "allow_object_scaling", boolConv},
		{"SYNC_EFFECTS", "players", "sync_effects", boolConv},
		{"SPEED_CHECK", "anti_cheat", "speed_hack", choice("off", "warn", "kick")},
		{"SCORE_CHECK", "anti_cheat", "modified_scoring", choice("off", "warn", "kick")},
		{"ENFORCE_TUNING", "anti_cheat", "enforce_tuning", boolConv},
		{"BONE_SCALE_LIMIT", "anti_cheat", "bone_scale_limit", intRange(0, 8)},
		{"USE_STEAM_RELAY", "network", "use_steam_relay", boolConv},
		{"SEND_RATE", "network", "send_rate", intRange(128, 16384)},
		{"CROWD_BUDGET", "network", "crowd_budget", intRange(0, 1000000)},
		{"PACK_MS", "network", "pack_ms", intRange(0, 50)},
		{"THREADS", "network", "threads", intRange(0, 64)},
		{"FINGER_DISTANCE", "network", "finger_distance", intRange(0, 100000)},
		{"STEAM_DEBUG", "network", "steam_debug", boolConv},
	}
	for _, s := range simple {
		if v, ok := env(s.env); ok {
			sub(cfg, s.section)[s.key] = s.c(s.env, v)
		}
	}
	for _, r := range removed {
		if _, ok := env(r[1]); ok {
			fmt.Printf("[config] %s is ignored: ReSkate 1.1.7 removed the '%s' setting\n", r[1], r[0])
		}
	}

	for _, d := range []struct{ env, key string }{
		{"DISTANCE_FULL_RATE_RETURN", "full_rate_return"},
		{"DISTANCE_HALF_RATE_START", "half_rate_start"},
		{"DISTANCE_HALF_RATE_RETURN", "half_rate_return"},
		{"DISTANCE_LOW_RATE_START", "low_rate_start"},
	} {
		if v, ok := env(d.env); ok {
			sub(sub(cfg, "network"), "distances")[d.key] = asInt(d.env, v)
		}
	}
	for _, p := range []struct{ env, key string }{
		{"PARK_CONSTRUCTION", "construction"}, {"PARK_HISTORIC", "historic"}, {"PARK_FINANCIAL", "financial"},
	} {
		if v, ok := env(p.env); ok {
			sub(sub(cfg, "maps"), "parks")[p.key] = v
		}
	}

	votes := sub(cfg, "votes")
	for _, p := range []struct{ prefix, key string }{
		{"VOTE_MAP", "map"}, {"VOTE_KICK", "kick"}, {"VOTE_TIME_OF_DAY", "time_of_day"},
	} {
		for _, f := range []struct {
			suffix, field string
			c             conv
		}{
			{"_ENABLED", "enabled", boolConv},
			{"_PERCENT", "percent", intConv},
			{"_SECONDS", "seconds", nonNegative},
			{"_COOLDOWN_SECONDS", "cooldown_seconds", nonNegative},
			{"_MIN_PLAYERS", "min_players", intRange(1, 249)},
		} {
			if v, ok := env(p.prefix + f.suffix); ok {
				sub(votes, p.key)[f.field] = f.c(p.prefix+f.suffix, v)
			}
		}
	}
	if v, ok := env("VOTE_STARTER_YES"); ok {
		votes["starter_votes_yes"] = asBool("VOTE_STARTER_YES", v)
	}
	if v, ok := env("POLLS"); ok {
		votes["polls"] = choice("off", "admins", "everyone")("POLLS", v)
	}
	if v, ok := env("POLL_SECONDS"); ok {
		votes["poll_seconds"] = intRange(1, 86400)("POLL_SECONDS", v)
	}
	// VOTES_CUSTOM=[{"name": "restart", "command": "map {map}", "percent": 60}]  (JSON, pins the list; off = none)
	if v, ok := env("VOTES_CUSTOM"); ok {
		custom := []any{}
		if l := strings.ToLower(v); l != "off" && l != "none" {
			dec := json.NewDecoder(strings.NewReader(v))
			dec.UseNumber()
			if err := dec.Decode(&custom); err != nil {
				die("VOTES_CUSTOM: expected a JSON list of votes, %v", err)
			}
			for _, c := range custom {
				if _, ok := c.(map[string]any); !ok {
					die(`VOTES_CUSTOM: expected a JSON list of votes like [{"name": ..., "command": ...}]`)
				}
			}
		}
		votes["custom"] = custom
	}
	if v, ok := env("VOTE_SECONDS"); ok {
		votes["seconds"] = asInt("VOTE_SECONDS", v)
	}
	if v, ok := env("VOTE_COOLDOWN_SECONDS"); ok {
		votes["cooldown_seconds"] = asInt("VOTE_COOLDOWN_SECONDS", v)
	}
	if len(votes) == 0 {
		delete(cfg, "votes")
	}

	// ANNOUNCEMENTS=First line|Second line  (| separated, chat lines contain commas; pins the list; off = none)
	if v, ok := env("ANNOUNCEMENTS"); ok {
		messages := []any{}
		if l := strings.ToLower(v); l != "off" && l != "none" {
			for _, m := range strings.Split(v, "|") {
				if m = strings.TrimSpace(m); m != "" {
					messages = append(messages, m)
				}
			}
		}
		sub(cfg, "announcements")["messages"] = messages
	}
	if v, ok := env("ANNOUNCEMENT_INTERVAL_MINUTES"); ok {
		sub(cfg, "announcements")["interval_minutes"] = nonNegative("ANNOUNCEMENT_INTERVAL_MINUTES", v)
	}
	if v, ok := env("ANNOUNCEMENT_CARD"); ok {
		sub(cfg, "announcements")["card"] = asBool("ANNOUNCEMENT_CARD", v)
	}

	// LAYERS=key=on,other_key=off
	if v, ok := env("LAYERS"); ok {
		layers := sub(sub(cfg, "maps"), "layers")
		for _, item := range asList(v) {
			key, mode, _ := strings.Cut(item, "=")
			mode = strings.ToLower(strings.TrimSpace(mode))
			if mode != "on" && mode != "off" && mode != "default" {
				die("LAYERS: %q must be key=on|off|default", item)
			}
			if mode == "default" {
				delete(layers, strings.TrimSpace(key))
			} else {
				layers[strings.TrimSpace(key)] = mode
			}
		}
	}

	// MAP_POOL=Map A,Map B: the maps players vote between and the rotation goes through.
	// Pins the list (in-game map-pool changes are replaced on restart); "off" empties it = all maps.
	if v, ok := env("MAP_POOL"); ok {
		pool := []any{}
		if l := strings.ToLower(v); l != "off" && l != "none" {
			for _, m := range asList(v) {
				pool = append(pool, m)
			}
		}
		sub(cfg, "maps")["pool"] = pool
	}

	// Lists are merged so in-game additions (admin add, ban) are kept.
	if v, ok := env("ADMINS"); ok {
		mergeList(cfg, "access", "admins", v, false)
	}
	if v, ok := env("RESERVED"); ok {
		mergeList(cfg, "access", "reserved_players_slots", v, false)
	}
	if v, ok := env("SCORE_ALLOW"); ok {
		mergeList(cfg, "anti_cheat", "allowed_scoring_mods", v, true)
	}
	// BANS=76561198000000000,76561198000000001:Some Name  (written to data/bans.json)
	if v, ok := env("BANS"); ok {
		var add []any
		for _, item := range asList(v) {
			sid, name, _ := strings.Cut(item, ":")
			add = append(add, map[string]any{"id": strings.TrimSpace(sid), "name": strings.TrimSpace(name), "added": 0})
		}
		bans := loadBans()
		n := len(bans)
		bans = mergeBans(bans, add)
		if _, err := os.Stat(bansPath); err != nil || len(bans) != n {
			writeJSON(bansPath, bans)
		}
	}

	writeJSON(configPath, cfg)

	// Thunderstore mods/maps (MODS): installed before the server starts, failures never block it.
	if v, ok := env("MODS"); ok {
		update := true
		if u, ok := env("MODS_UPDATE"); ok {
			update = asBool("MODS_UPDATE", u)
		}
		installMods(v, update)
	}

	warnUnknownMaps(cfg)

	// Discord sidecar (console forwarding + update announcements): a child process that
	// outlives the exec below, so the server keeps the console for `docker attach`.
	var hooks [][2]string
	for _, v := range []string{"DISCORD_WEBHOOK", "DISCORD_WEBHOOK_ADMIN", "DISCORD_WEBHOOK_USER"} {
		if h, ok := env(v); ok {
			hooks = append(hooks, [2]string{v, h})
		}
	}
	if len(hooks) > 0 {
		for _, h := range hooks {
			if !strings.HasPrefix(h[1], "https://") && !strings.HasPrefix(h[1], "http://") {
				die("%s: expected a webhook URL starting with https://", h[0])
			}
		}
		if v, ok := env("DISCORD_MENTION_IDS"); ok {
			for _, item := range asList(v) {
				if _, err := strconv.ParseUint(item, 10, 64); err != nil {
					die("DISCORD_MENTION_IDS: expected Discord user ids (digits), got %q", item)
				}
			}
		}
		if v, ok := env("DISCORD_CONSOLE"); ok {
			asBool("DISCORD_CONSOLE", v)
		}
		if self, err := os.Executable(); err == nil {
			cmd := exec.Command(self, "notifier") // stdin/stdout/stderr = /dev/null
			cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
			_ = cmd.Start()
		}
	}

	// Kick and ban announcements (announcer.go): the server's console becomes a pipe that both the
	// container's stdin and the announcer write into.
	announce := true
	if v, ok := env("ANNOUNCE_KICKS"); ok {
		announce = asBool("ANNOUNCE_KICKS", v)
	}
	if announce {
		if self, err := os.Executable(); err == nil {
			if r, w, err := os.Pipe(); err == nil {
				cmd := exec.Command(self, "announcer") // stdout/stderr = /dev/null
				cmd.Stdin = os.Stdin
				cmd.ExtraFiles = []*os.File{w}
				cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
				if cmd.Start() == nil {
					_ = syscall.Dup3(int(r.Fd()), 0, 0)
				}
				r.Close()
				w.Close()
			}
		}
	}

	// ReSkate 1.1.4+ can replace its own binary. In a container that drifts from the image tag and is
	// lost when the container is recreated, so it stays off unless AUTO_UPDATE=true is set explicitly.
	args := []string{"/app/ReSkateServer", "--config", configPath}
	au, _ := env("AUTO_UPDATE")
	if l := strings.ToLower(au); l != "1" && l != "true" && l != "yes" && l != "on" {
		skip := false
		for _, a := range serverArgs {
			skip = skip || a == "--no-update"
		}
		if !skip {
			args = append(args, "--no-update")
		}
	}
	args = append(args, serverArgs...)
	if err := os.Chdir("/app"); err != nil {
		die("chdir /app: %v", err)
	}
	die("cannot start server: %v", syscall.Exec(args[0], args, os.Environ()))
}
