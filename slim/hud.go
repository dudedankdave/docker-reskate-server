package main

// The leaderboard's feed for the COJA tools HUD (the COCOJAMBO-TOOLS plugin, plugins/tools), as in leaderboard.py:
// with TOOLS_HUD on (by default whenever PLUGINS_REQUIRED lists COCOJAMBO-TOOLS) every online player's HUD gets
// their points and rank on the board TOOLS_HUD_BOARD (default general: All Time) and their week, and the panel
// gets that board's top 10 (`coco player` / `coco top` on the console, as base64 JSON): shortly after joining,
// after points are awarded and every 5 minutes. Once the server is up the panel also gets the server's name
// (SERVER_NAME), links (TOOLS_LINKS, "Discord=https://...|Website=https://...") and rules (TOOLS_RULES, "Be nice|No
// hacks") with `coco info`.

import (
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strings"
	"time"
)

var hudPackageRe = regexp.MustCompile(`(?i)^cocojambo-tools(-\d+\.\d+\.\d+)?$`)

const (
	hudEvery     = 5 * time.Minute // between two refreshes of every online player's HUD and the board
	hudAfterJoin = 3 * time.Second // after the join line, before a player's stats go out
	hudAfterUp   = 5 * time.Second // after the server is up, before the server's info goes out
)

// hudOn: TOOLS_HUD, else on when PLUGINS_REQUIRED lists COCOJAMBO-TOOLS.
func hudOn() bool {
	if v := strings.TrimSpace(os.Getenv("TOOLS_HUD")); v != "" {
		return truthy(v, false)
	}
	for _, p := range regexp.MustCompile(`[,\s]+`).Split(os.Getenv("PLUGINS_REQUIRED"), -1) {
		if p != "" && hudPackageRe.MatchString(p) {
			return true
		}
	}
	return false
}

type hudLink struct {
	Label string `json:"label"`
	URL   string `json:"url"`
}

type hudServer struct {
	Name  string    `json:"name"`
	Links []hudLink `json:"links"`
	Rules []string  `json:"rules"`
}

// hudInfo is the panel's server part, from SERVER_NAME, TOOLS_LINKS and TOOLS_RULES.
func hudInfo() hudServer {
	info := hudServer{Name: strings.TrimSpace(os.Getenv("SERVER_NAME")), Links: []hudLink{}, Rules: []string{}}
	for _, part := range strings.Split(os.Getenv("TOOLS_LINKS"), "|") {
		label, url, _ := strings.Cut(part, "=")
		if strings.TrimSpace(label) != "" && strings.TrimSpace(url) != "" && len(info.Links) < 6 {
			info.Links = append(info.Links, hudLink{strings.TrimSpace(label), strings.TrimSpace(url)})
		}
	}
	for _, rule := range strings.Split(os.Getenv("TOOLS_RULES"), "|") {
		if rule = strings.TrimSpace(rule); rule != "" && len(info.Rules) < 6 {
			info.Rules = append(info.Rules, rule)
		}
	}
	return info
}

// hudLine is a `coco` console line for the HUD's server copy; value goes as base64 JSON.
func hudLine(verb string, head []string, value any) string {
	data, _ := json.Marshal(value)
	return strings.Join(append(append([]string{"coco", verb}, head...), base64.StdEncoding.EncodeToString(data)), " ")
}

// hudStats is what a player's HUD shows: their points and rank on the HUD's board, and their week.
func (b *leaderboard) hudStats(tx *sql.Tx, board lbBoard, steam string, now time.Time) map[string]any {
	stats := map[string]any{"points": 0, "board": board.name + " (" + b.league + ")"}
	if rank, total, row, ok := lbRank(tx, board.id, steam); ok {
		stats["points"], stats["rank"], stats["of"] = row.points, rank, total
	}
	if board.kind != "week" && b.kinds["week"] {
		label, value := "This week", "no points yet"
		if week, ok := lbFind(tx, "week", now); ok {
			label = week.name
			if rank, _, row, ok := lbRank(tx, week.id, steam); ok {
				value = fmt.Sprintf("#%d, %d pts", rank, row.points)
			}
		}
		stats["lines"] = []map[string]string{{"l": label, "v": value}}
	}
	return stats
}

// hudPush sends `coco player` for each of steams, and the board's `coco top` when top.
func (b *leaderboard) hudPush(steams []string, top bool) {
	now := time.Now()
	var board lbBoard
	stats := map[string]map[string]any{}
	var rows []lbRow
	found := false
	b.run(func(tx *sql.Tx) error {
		b.active(tx, now) // this week's board exists before anyone scores on it
		if board, found = lbFind(tx, b.hudBoard, now); !found {
			board, found = lbFind(tx, "general", now)
		}
		if !found {
			return nil
		}
		for _, s := range steams {
			stats[s] = b.hudStats(tx, board, s, now)
		}
		if top {
			rows = lbTop(tx, board.id, 10)
		}
		return nil
	})
	if !found {
		return
	}
	for _, s := range steams {
		if v, ok := stats[s]; ok {
			b.send(hudLine("player", []string{s}, v))
		}
	}
	if top {
		list := make([]map[string]any, 0, len(rows))
		for _, r := range rows {
			list = append(list, map[string]any{"n": r.name, "p": r.points})
		}
		b.send(hudLine("top", nil, map[string]any{"board": board.name + " (" + b.league + ")", "rows": list}))
	}
}

func (b *leaderboard) hudTick(now time.Time) {
	b.mu.Lock()
	var due []string
	for s, at := range b.hudDue {
		if !now.Before(at) {
			due = append(due, s)
			delete(b.hudDue, s)
		}
	}
	info := !b.hudInfoAt.IsZero() && !now.Before(b.hudInfoAt)
	if info {
		b.hudInfoAt = time.Time{}
	}
	var everyone []string
	seen := map[string]bool{}
	for _, s := range b.online {
		if !seen[s] {
			seen[s] = true
			everyone = append(everyone, s)
		}
	}
	b.mu.Unlock()
	if info {
		b.send(hudLine("info", nil, b.hudServer))
	}
	top := now.Sub(b.hudAt) >= hudEvery || info
	if top {
		b.hudAt = now
		due = everyone
	}
	sort.Strings(due)
	if len(due) > 0 || top {
		b.hudPush(due, top)
	}
}
