package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
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
		{"SERVER_NAME", "name", text},
		{"MAP", "map", text},
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

	// Discord sidecar (console forwarding + update announcements): a child process that
	// outlives the exec below, so the server keeps the console for `docker attach`.
	if hook, ok := env("DISCORD_WEBHOOK"); ok {
		if !strings.HasPrefix(hook, "https://") && !strings.HasPrefix(hook, "http://") {
			die("DISCORD_WEBHOOK: expected a webhook URL starting with https://")
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
