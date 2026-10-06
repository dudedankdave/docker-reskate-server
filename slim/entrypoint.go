package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
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
func clearable(_, v string) any {
	if l := strings.ToLower(v); l == "off" || l == "none" {
		return ""
	}
	return v
}
func nonNegative(name, v string) any {
	n := asInt(name, v)
	if n < 0 {
		die("%s: expected 0 (off) or more minutes, got %q", name, v)
	}
	return n
}

// ReSkate 1.1.3+ only accepts 1-64 ASCII letters, digits, spaces and - _ [ ] ( ) as a name and
// refuses to start otherwise. Clean a name it would reject (and say so) instead of crash-looping.
var (
	nameOK    = regexp.MustCompile(`^[A-Za-z0-9 _\[\]()-]{1,64}$`)
	nameDrop  = regexp.MustCompile(`['\x{2019}.#%]`)
	nameBad   = regexp.MustCompile(`[^A-Za-z0-9 _\[\]()-]`)
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
	fmt.Printf("[config] %s %q is not accepted by ReSkate 1.1.3+ (1-64 letters, numbers, spaces and - _ [ ] ( ) only), using %q instead\n", name, v, clean)
	return clean
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
	if m, ok := cfg["map"].(string); ok && m != "" {
		wanted = append(wanted, want{"MAP", m})
	}
	if pool, ok := cfg["map_pool"].([]any); ok {
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

	simple := []struct {
		env, key string
		c        conv
	}{
		{"SERVER_NAME", "name", serverName},
		{"MAP", "map", text},
		{"MAP_ROTATION_MINUTES", "map_rotation_minutes", nonNegative},
		{"MAX_PLAYERS", "max_players", intConv},
		{"SERVER_PASSWORD", "password", clearable},
		{"WELCOME_MESSAGE", "welcome", clearable},
		{"LISTED", "listed", boolConv},
		{"AUTO_UPDATE", "auto_update", boolConv},
		{"ANNOUNCE_THROWDOWNS", "announce_throwdowns", boolConv},
		{"PARTIES", "parties", boolConv},
		{"PARTY_SIZE", "party_size", intConv},
		{"SPEED_CHECK", "speed_check", choice("off", "warn", "kick")},
		{"SCORE_CHECK", "score_check", choice("off", "warn", "kick")},
		{"ACTIVITY_LOG", "activity_log", boolConv},
		{"PORT", "port", intConv},
		{"QUERY_PORT", "query_port", intConv},
		{"TPS", "tps", choice("20", "30", "60", "120")},
		{"VOICE_CHAT", "voice_chat", boolConv},
		{"VOICE_RANGE", "voice_range", intConv},
		{"OBJECT_PLACEMENT", "object_placement", choice("everyone", "admins", "nobody")},
		{"NOCLIP", "noclip", boolConv},
		{"NO_BAIL", "no_bail", boolConv},
		{"BOOSTS", "boosts", boolConv},
		{"ENFORCE_TUNING", "enforce_tuning", boolConv},
		{"WORLD_LAYER_SYNC", "world_layer_sync", boolConv},
	}
	for _, s := range simple {
		if v, ok := env(s.env); ok {
			cfg[s.key] = s.c(s.env, v)
		}
	}
	if t, ok := cfg["tps"].(string); ok {
		cfg["tps"], _ = strconv.Atoi(t)
	}

	for _, d := range []struct{ env, key string }{
		{"DISTANCE_FULL_RATE_RETURN", "full_rate_return"},
		{"DISTANCE_HALF_RATE_START", "half_rate_start"},
		{"DISTANCE_HALF_RATE_RETURN", "half_rate_return"},
		{"DISTANCE_LOW_RATE_START", "low_rate_start"},
	} {
		if v, ok := env(d.env); ok {
			sub(cfg, "distances")[d.key] = asInt(d.env, v)
		}
	}
	for _, p := range []struct{ env, key string }{
		{"PARK_CONSTRUCTION", "construction"}, {"PARK_HISTORIC", "historic"}, {"PARK_FINANCIAL", "financial"},
	} {
		if v, ok := env(p.env); ok {
			sub(cfg, "parks")[p.key] = v
		}
	}

	votes := sub(cfg, "votes")
	for _, p := range []struct{ prefix, key string }{
		{"VOTE_MAP", "map"}, {"VOTE_KICK", "kick"}, {"VOTE_TIME_OF_DAY", "time_of_day"},
	} {
		if v, ok := env(p.prefix + "_ENABLED"); ok {
			sub(votes, p.key)["enabled"] = asBool(p.prefix+"_ENABLED", v)
		}
		if v, ok := env(p.prefix + "_PERCENT"); ok {
			sub(votes, p.key)["percent"] = asInt(p.prefix+"_PERCENT", v)
		}
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

	// LAYERS=key=on,other_key=off
	if v, ok := env("LAYERS"); ok {
		layers := sub(cfg, "layers")
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
		cfg["map_pool"] = pool
	}

	// Lists are merged so in-game additions (admin add, ban) are kept.
	if v, ok := env("ADMINS"); ok {
		var admins []string
		old, _ := cfg["admins"].([]any)
		for _, a := range old {
			admins = append(admins, asString(a))
		}
		for _, a := range asList(v) {
			if !contains(admins, a) {
				admins = append(admins, a)
			}
		}
		out := make([]any, len(admins))
		for i, a := range admins {
			out[i] = a
		}
		cfg["admins"] = out
	}
	if v, ok := env("SCORE_ALLOW"); ok {
		var allowed []string
		old, _ := cfg["score_allow"].([]any)
		for _, a := range old {
			allowed = append(allowed, strings.ToLower(asString(a)))
		}
		for _, a := range asList(v) {
			if a = strings.ToLower(a); !contains(allowed, a) {
				allowed = append(allowed, a)
			}
		}
		out := make([]any, len(allowed))
		for i, a := range allowed {
			out[i] = a
		}
		cfg["score_allow"] = out
	}
	// BANS=76561198000000000,76561198000000001:Some Name
	if v, ok := env("BANS"); ok {
		bans, _ := cfg["bans"].([]any)
		if bans == nil {
			bans = []any{}
		}
		var known []string
		for _, b := range bans {
			if m, ok := b.(map[string]any); ok {
				known = append(known, asString(m["id"]))
			}
		}
		for _, item := range asList(v) {
			sid, name, _ := strings.Cut(item, ":")
			sid, name = strings.TrimSpace(sid), strings.TrimSpace(name)
			if !contains(known, sid) {
				bans = append(bans, map[string]any{"id": sid, "name": name, "added": 0})
			}
		}
		cfg["bans"] = bans
	}

	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(cfg); err != nil {
		die("cannot encode config: %v", err)
	}
	tmp := configPath + ".tmp"
	if err := os.WriteFile(tmp, buf.Bytes(), 0o644); err != nil {
		die("cannot write %s: %v", tmp, err)
	}
	if err := os.Rename(tmp, configPath); err != nil {
		die("cannot replace %s: %v", configPath, err)
	}

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

	args := append([]string{"/app/ReSkateServer", "--config", configPath}, serverArgs...)
	if err := os.Chdir("/app"); err != nil {
		die("chdir /app: %v", err)
	}
	die("cannot start server: %v", syscall.Exec(args[0], args, os.Environ()))
}
