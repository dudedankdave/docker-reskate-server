package main

// Settings shared by the servers of a stack, or their own, from files in /shared/settings
// (SETTINGS_DIR): common.env, secrets/common.env, server-<n>.env, secrets/server-<n>.env, each
// overriding the one before and the container env. KEY+=value appends to a list. The values go
// into the process environment before anything reads it. Same files and rules as app/settings.py;
// tests/test_config_parity.py checks both write the same.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// settingsIdentity: per container (a server's A and B slot run at the same time with their own
// ports and token), never from the files.
var settingsIdentity = map[string]bool{"PORT": true, "QUERY_PORT": true, "STEAM_TOKEN": true, "RESKATE_SERVER": true}

type setting struct {
	key, value string
	append     bool
}

func settingsDir() string {
	if d := strings.TrimSpace(os.Getenv("SETTINGS_DIR")); d != "" {
		return d
	}
	return "/shared/settings"
}

// parseSettings reads env-file text: KEY=value, # comments, "double" or 'single' quotes, KEY+=value.
func parseSettings(text string) []setting {
	var out []setting
	for _, raw := range strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.HasPrefix(line, "export ") {
			line = strings.TrimSpace(line[7:])
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		appendTo := strings.HasSuffix(key, "+")
		key = strings.TrimSpace(strings.TrimRight(key, "+"))
		if key == "" || strings.Trim(key, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_") != "" {
			continue
		}
		value = strings.TrimSpace(value)
		if value != "" && (value[0] == '"' || value[0] == '\'') {
			q := value[0]
			var b strings.Builder
			for i := 1; i < len(value) && value[i] != q; i++ {
				if q == '"' && value[i] == '\\' && i+1 < len(value) {
					i++
					switch value[i] {
					case 'n':
						b.WriteByte('\n')
					case 't':
						b.WriteByte('\t')
					default:
						b.WriteByte(value[i])
					}
					continue
				}
				b.WriteByte(value[i])
			}
			value = b.String()
		} else {
			cut := -1
			for _, sep := range []string{" #", "\t#"} {
				if i := strings.Index(value, sep); i >= 0 && (cut < 0 || i < cut) {
					cut = i
				}
			}
			if cut >= 0 {
				value = strings.TrimRight(value[:cut], " \t")
			}
		}
		out = append(out, setting{key, value, appendTo})
	}
	return out
}

// joinedSetting: base with extra appended, as the list the key is (JSON, | or comma).
func joinedSetting(key, base, extra string) string {
	if base == "" {
		return extra
	}
	if extra == "" {
		return base
	}
	if strings.HasPrefix(strings.TrimLeft(base, " \t"), "[") {
		var a, b []any
		if json.Unmarshal([]byte(base), &a) == nil && json.Unmarshal([]byte(extra), &b) == nil {
			if out, err := pyJSON(append(a, b...)); err == nil {
				return out
			}
		}
	}
	if key == "ANNOUNCEMENTS" {
		return base + "|" + extra
	}
	return base + "," + extra
}

// pyJSON writes JSON as app/settings.py's json.dumps(sort_keys=True, ensure_ascii=False) does
// (", " and ": ", sorted keys, no HTML escapes), so both images get the same text and digest.
func pyJSON(v any) (string, error) {
	var b strings.Builder
	var write func(v any) error
	write = func(v any) error {
		switch t := v.(type) {
		case []any:
			b.WriteString("[")
			for i, x := range t {
				if i > 0 {
					b.WriteString(", ")
				}
				if err := write(x); err != nil {
					return err
				}
			}
			b.WriteString("]")
		case map[string]any:
			b.WriteString("{")
			keys := make([]string, 0, len(t))
			for k := range t {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for i, k := range keys {
				if i > 0 {
					b.WriteString(", ")
				}
				if err := write(k); err != nil {
					return err
				}
				b.WriteString(": ")
				if err := write(t[k]); err != nil {
					return err
				}
			}
			b.WriteString("}")
		default:
			var buf strings.Builder
			enc := json.NewEncoder(&buf)
			enc.SetEscapeHTML(false)
			if err := enc.Encode(t); err != nil {
				return err
			}
			b.WriteString(strings.TrimSuffix(buf.String(), "\n"))
		}
		return nil
	}
	err := write(v)
	return b.String(), err
}

// loadSettings: the files' values for the server in the environment, and which keys came from which file.
func loadSettings(dir string) (map[string]string, [][2]string) {
	server := strings.TrimSpace(os.Getenv("RESKATE_SERVER"))
	names := []string{"common.env", filepath.Join("secrets", "common.env")}
	if server != "" {
		names = append(names, "server-"+server+".env", filepath.Join("secrets", "server-"+server+".env"))
	}
	values := map[string]string{}
	var sources [][2]string
	for _, name := range names {
		raw, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			continue
		}
		raw = []byte(strings.TrimPrefix(string(raw), "\ufeff"))
		var keys []string
		seen := map[string]bool{}
		for _, s := range parseSettings(string(raw)) {
			if settingsIdentity[s.key] {
				fmt.Printf("[settings] %s in %s is ignored: it belongs to the container (per slot)\n", s.key, name)
				continue
			}
			value := s.value
			if s.append {
				base, ok := values[s.key]
				if !ok {
					base = os.Getenv(s.key)
				}
				value = joinedSetting(s.key, base, value)
			}
			values[s.key] = value
			if !seen[s.key] {
				seen[s.key] = true
				keys = append(keys, s.key)
			}
		}
		if len(keys) > 0 {
			sources = append(sources, [2]string{name, strings.Join(keys, ", ")})
		}
	}
	return values, sources
}

// settingsDigest: a short hash of what the files set, to tell when they changed.
func settingsDigest(values map[string]string) string {
	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha256.New()
	for _, k := range keys {
		fmt.Fprintf(h, "%s=%s\n", k, values[k])
	}
	return hex.EncodeToString(h.Sum(nil))[:16]
}

// applySettings puts the files' values into the environment and says where they came from.
func applySettings() {
	dir := settingsDir()
	if st, err := os.Stat(dir); err != nil || !st.IsDir() {
		return
	}
	values, sources := loadSettings(dir)
	for _, s := range sources {
		fmt.Printf("[settings] %s: %s\n", s[0], s[1])
	}
	for k, v := range values {
		os.Setenv(k, v)
	}
	fmt.Printf("[settings] digest %s\n", settingsDigest(values))
	fmt.Printf("[settings] restart-digest %s\n", restartDigest(values))
}

// liveKeys: the settings a running server takes from its console (app/settings.py LIVE has the
// commands); the restart digest leaves them out. tests/test_settings_live.py checks both lists match.
var liveKeys = map[string]bool{
	"ACTIVITY_LOG": true, "ADMINS": true, "AFK_KICK_MINUTES": true, "ANNOUNCEMENTS": true,
	"ANNOUNCEMENT_INTERVAL_MINUTES": true, "ANNOUNCE_THROWDOWNS": true, "BANS": true, "BOARD_CHECK": true,
	"BONE_REACH_LIMIT": true, "BONE_SCALE_LIMIT": true, "BOOSTS": true, "CHAT_COLOR": true,
	"CHAT_TEXT_COLOR": true, "CROWD_BUDGET": true, "DISTANCE_FULL_RATE_RETURN": true,
	"DISTANCE_HALF_RATE_RETURN": true, "DISTANCE_HALF_RATE_START": true, "DISTANCE_LOW_RATE_START": true,
	"ENFORCE_TUNING": true, "GLOBAL_WORD_FILTER": true, "KICK_MINUTES": true, "LAYERS": true, "LISTED": true,
	"MAP_LATEST_CHECK_MINUTES": true, "MAP_LATEST_VERSIONS": true, "MAP_POOL": true,
	"MAP_ROTATION_ANNOUNCE": true, "MAP_ROTATION_EXTEND_MINUTES": true, "MAP_ROTATION_MINUTES": true,
	"MAP_ROTATION_VOTE": true, "MODS": true, "NOCLIP": true, "NO_BAIL": true, "OBJECT_LIMIT": true, "OBJECT_PLACEMENT": true,
	"OBJECT_SCALING": true, "PARK_CONSTRUCTION": true, "PARK_FINANCIAL": true, "PARK_HISTORIC": true,
	"PARTIES": true, "PARTY_SIZE": true, "POLLS": true, "POLL_SECONDS": true, "RESERVED": true,
	"SCORE_ALLOW": true, "SCORE_CHECK": true, "SEND_RATE": true, "SERVER_NAME": true, "SERVER_PASSWORD": true,
	"SPEED_CHECK": true, "SYNC_DRONES": true, "SYNC_EFFECTS": true, "THROWDOWN_LOCKS": true, "VOICE_CHAT": true, "VOICE_RANGE": true,
	"VOTE_COOLDOWN_SECONDS": true, "VOTE_KICK_COOLDOWN_SECONDS": true, "VOTE_KICK_ENABLED": true,
	"VOTE_KICK_MINUTES": true, "VOTE_KICK_MIN_PLAYERS": true, "VOTE_KICK_PERCENT": true, "VOTE_KICK_SECONDS": true,
	"VOTE_MAP_COOLDOWN_SECONDS": true, "VOTE_MAP_ENABLED": true, "VOTE_MAP_MIN_PLAYERS": true,
	"VOTE_MAP_PERCENT": true, "VOTE_MAP_SECONDS": true, "VOTE_SECONDS": true, "VOTE_STARTER_YES": true,
	"VOTE_TIME_OF_DAY_COOLDOWN_SECONDS": true, "VOTE_TIME_OF_DAY_ENABLED": true,
	"VOTE_TIME_OF_DAY_MIN_PLAYERS": true, "VOTE_TIME_OF_DAY_PERCENT": true, "VOTE_TIME_OF_DAY_SECONDS": true,
	"WELCOME_MESSAGE": true, "WORLD_LAYER_SYNC": true,
}

// restartDigest: settingsDigest of only the keys a running server cannot take.
func restartDigest(values map[string]string) string {
	kept := map[string]string{}
	for k, v := range values {
		if !liveKeys[k] {
			kept[k] = v
		}
	}
	return settingsDigest(kept)
}
