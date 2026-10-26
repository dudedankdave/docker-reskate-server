package main

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

type hudSent struct {
	verb, who string
	value     map[string]any
}

func hudDecode(t *testing.T, lines []string) []hudSent {
	var out []hudSent
	for _, l := range lines {
		if !strings.HasPrefix(l, "coco ") {
			continue
		}
		w := strings.Split(l, " ")
		data, err := base64.StdEncoding.DecodeString(w[len(w)-1])
		if err != nil {
			t.Fatal(err)
		}
		s := hudSent{verb: w[1]}
		if len(w) == 4 {
			s.who = w[2]
		}
		if err := json.Unmarshal(data, &s.value); err != nil {
			t.Fatal(err)
		}
		out = append(out, s)
	}
	return out
}

func TestHudOn(t *testing.T) {
	for _, c := range []struct {
		hud, required string
		want          bool
	}{
		{"", "SmokeyDawg58-ReSkate_Throwdown, COCOJAMBO-TOOLS", true},
		{"", "cocojambo-tools-1.0.0", true},
		{"", "COCOJAMBO-TOOLSX", false},
		{"", "", false},
		{"true", "", true},
		{"false", "COCOJAMBO-TOOLS", false},
	} {
		t.Setenv("TOOLS_HUD", c.hud)
		t.Setenv("PLUGINS_REQUIRED", c.required)
		if got := hudOn(); got != c.want {
			t.Errorf("TOOLS_HUD=%q PLUGINS_REQUIRED=%q: %v", c.hud, c.required, got)
		}
	}
	t.Setenv("SERVER_NAME", "Yard")
	t.Setenv("TOOLS_LINKS", "Discord=https://discord.gg/x | broken | Web=https://cocojambo.net")
	t.Setenv("TOOLS_RULES", "Be nice| |No hacks")
	info := hudInfo()
	if info.Name != "Yard" || len(info.Links) != 2 || info.Links[1].URL != "https://cocojambo.net" || strings.Join(info.Rules, "|") != "Be nice|No hacks" {
		t.Fatalf("%+v", info)
	}
}

func TestHudFeed(t *testing.T) {
	t.Setenv("PLUGINS_REQUIRED", "COCOJAMBO-TOOLS")
	t.Setenv("SERVER_NAME", "Yard")
	b, sent, _ := newTestBoard(t)
	if !b.hud {
		t.Fatal("HUD feed is off")
	}
	b.feed("[12:00:00] Yard is up on Skate3Improved for 16 players.")
	b.hudTick(time.Now().Add(hudAfterUp + time.Second))
	got := hudDecode(t, *sent)
	if len(got) != 2 || got[0].verb != "info" || got[0].value["name"] != "Yard" || got[1].verb != "top" {
		t.Fatalf("after up: %+v", got)
	}
	*sent = nil
	b.feed("[12:00:01] Anna joined (76561198000000001, player), 1/16 players")
	b.feed("[12:00:02] Ben joined (76561198000000002, admin), 2/16 players")
	b.hudTick(time.Now().Add(time.Second))
	if len(hudDecode(t, *sent)) != 0 {
		t.Fatalf("too early: %v", *sent)
	}
	b.hudTick(time.Now().Add(hudAfterJoin + time.Second))
	players := map[string]map[string]any{}
	for _, s := range hudDecode(t, *sent) {
		if s.verb == "player" {
			players[s.who] = s.value
		}
	}
	if len(players) != 2 || players["76561198000000001"]["board"] != "All Time (Ranked)" {
		t.Fatalf("after joins: %+v", players)
	}
	*sent = nil
	b.waiting = map[string]time.Time{}
	b.feed("[12:05:00] [throwdown] Anna's Jam has finished: 1. Anna 5,000, 2. Ben 1,000")
	b.tick()
	players = map[string]map[string]any{}
	var top map[string]any
	for _, s := range hudDecode(t, *sent) {
		if s.verb == "player" {
			players[s.who] = s.value
		} else if s.verb == "top" {
			top = s.value
		}
	}
	a := players["76561198000000001"]
	// (newTestBoard's legacy file has Old with 50 points on All Time: Anna is second there, first on the week)
	if a["points"] != float64(10) || a["rank"] != float64(2) || a["of"] != float64(3) || !strings.HasPrefix(a["lines"].([]any)[0].(map[string]any)["v"].(string), "#1, 10 pts") {
		t.Fatalf("Anna after the jam: %+v", a)
	}
	rows := top["rows"].([]any)
	if len(rows) != 3 || rows[0].(map[string]any)["n"] != "Old" || rows[1].(map[string]any)["n"] != "Anna" {
		t.Fatalf("top: %+v", top)
	}
}

func TestHudOffWithoutThePlugin(t *testing.T) {
	t.Setenv("PLUGINS_REQUIRED", "")
	b, sent, _ := newTestBoard(t)
	b.feed("[12:00:01] Anna joined (76561198000000001, player), 1/16 players")
	b.tick()
	if b.hud || len(hudDecode(t, *sent)) != 0 {
		t.Fatalf("sent with the HUD off: %v", *sent)
	}
}
