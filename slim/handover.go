package main

// Which parts this server leaves to the modules outside it (bridge, leaderboard, webhooks ...),
// read from /shared/modules.json while the server runs, so a part can be handed over and back
// without a restart. Without the file everything runs inside the server, as before. Same file
// and rules as app/handover.py:
//
//	{"leaderboard": "external", "webhooks": {"public": "external"}, "servers": {"6": {"leaderboard": "internal"}}}
//
// Parts: leaderboard, announcer, webhooks (one value, or one per scope). "servers" overrides per
// server, by RESKATE_SERVER. The [event] lines are printed either way.

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

var handoverParts = []string{"leaderboard", "announcer", "webhooks"}

var handover = struct {
	sync.Mutex
	at    time.Time
	mtime time.Time
	found bool
	parts map[string]any
}{parts: map[string]any{}}

func handoverFile() string {
	if f := strings.TrimSpace(os.Getenv("MODULES_FILE")); f != "" {
		return f
	}
	return "/shared/modules.json"
}

// parseHandover picks the parts (and this server's overrides) from the file's JSON.
func parseHandover(data map[string]any, server string) map[string]any {
	parts := map[string]any{}
	for _, k := range handoverParts {
		if v, ok := data[k]; ok {
			parts[k] = v
		}
	}
	if servers, ok := data["servers"].(map[string]any); ok && server != "" {
		if own, ok := servers[server].(map[string]any); ok {
			for _, k := range handoverParts {
				if v, ok := own[k]; ok {
					parts[k] = v
				}
			}
		}
	}
	return parts
}

func loadHandover() {
	file := handoverFile()
	st, err := os.Stat(file)
	found := err == nil
	if found == handover.found && (!found || st.ModTime().Equal(handover.mtime)) {
		return
	}
	parts := map[string]any{}
	if found {
		raw, err := os.ReadFile(file)
		var data map[string]any
		if err == nil {
			err = json.Unmarshal(raw, &data)
		}
		if err != nil {
			fmt.Printf("[modules] cannot read %s, keeping what was set: %v\n", file, err)
			return
		}
		parts = parseHandover(data, strings.TrimSpace(os.Getenv("RESKATE_SERVER")))
	}
	old, _ := json.Marshal(handover.parts)
	now, _ := json.Marshal(parts)
	handover.found, handover.parts = found, parts
	if found {
		handover.mtime = st.ModTime()
	}
	if string(old) != string(now) {
		var shown []string
		for k, v := range parts {
			b, _ := json.Marshal(v)
			shown = append(shown, k+"="+string(b))
		}
		sort.Strings(shown)
		if len(shown) == 0 {
			shown = []string{"everything inside the server"}
		}
		fmt.Printf("[modules] %s\n", strings.Join(shown, ", "))
	}
}

// external: true when part (for webhooks: of scope) is run outside the server now.
func external(part, scope string) bool {
	handover.Lock()
	if handover.at.IsZero() || time.Since(handover.at) >= time.Minute {
		handover.at = time.Now()
		loadHandover()
	}
	v := handover.parts[part]
	handover.Unlock()
	if m, ok := v.(map[string]any); ok {
		if scope == "" {
			return false
		}
		v = m[scope]
	}
	s, _ := v.(string)
	return strings.EqualFold(strings.TrimSpace(s), "external")
}
