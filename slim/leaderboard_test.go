package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func newTestBoard(t *testing.T) (*leaderboard, *[]string, *[]string) {
	dir := t.TempDir()
	t.Setenv("LEADERBOARD", "true")
	t.Setenv("LEADERBOARD_SCOPE", "server")
	t.Setenv("LEADERBOARD_FILE", filepath.Join(dir, "leaderboard.db"))
	if err := os.WriteFile(filepath.Join(dir, "leaderboard.json"),
		[]byte(`{"players":{"7656":{"name":"Old","points":50,"wins":2,"played":5}}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	var sent, posted []string
	b := newLeaderboard(func(s string) bool { sent = append(sent, s); return true }, func(s string) { posted = append(posted, s) })
	if b == nil {
		t.Fatal("leaderboard did not start")
	}
	return b, &sent, &posted
}

func TestBoardsAndCommands(t *testing.T) {
	b, sent, posted := newTestBoard(t)
	feed := func(l string) { b.feed("[12:00:00] " + l); b.asked = map[string]time.Time{} }
	feed("Anna joined (76561198000000001, admin), 1/10 players, x")
	feed("Ben joined (76561198000000002), 2/10 players, x")
	feed("[command] Ben: /top") // the colon form is ignored
	if len(*sent) != 0 {
		t.Fatalf("colon form answered: %v", *sent)
	}
	feed("[command] Ben /top")
	if got := strings.Join(*sent, "|"); !strings.Contains(got, "Top 1 - All Time") && strings.Contains(got, "1. Old 50") {
		t.Fatalf("legacy import missing: %v", *sent)
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(b.path), "leaderboard.json.migrated")); err != nil {
		t.Fatal("legacy file not renamed")
	}
	b.waiting = map[string]time.Time{}
	feed("[throwdown] Anna's Jam has finished: 1. Anna 1,234, 2. Ben 999")
	feed("[command] Ben /lb award 99 Ben") // not an admin
	feed("[command] Anna /lb event start Spring Cup")
	feed("[throwdown] Anna's Jam has finished: 1. Ben 1,234, 2. Anna 999")
	*sent = nil
	feed("[command] Ben /top event")
	feed("[command] Anna /lb award 7 ben board:event")
	feed("[command] Ben /rank spring")
	feed("[command] Ben /top nope")
	feed("[throwdown] Anna's S.K.A.T.E. has finished: Anna 7 landed / 3 missed, Ben 2 landed / 8 missed")
	feed("[command] Ben /top week skate")
	feed("[command] Ben /top jam")
	feed("[command] Anna /props Ben")
	feed("[command] Anna /props Ben")  // again within 24 hours
	feed("[command] Anna /props Anna") // not yourself
	feed("[command] Ben /top props")
	feed("[command] Anna /lb event end")
	out := strings.Join(*sent, "\n")
	for _, want := range []string{"Top 2 - Spring Cup", "1. Ben  10", "2. Anna  6", "Gave +7 points to Ben on 1 board(s).",
		"Your rank - Spring Cup", "You are #1 of 2 with 17 points", "No board called 'nope'", "Week", "Top 2 - All Time - Jam", "Top 2 - Week 41 - S.K.A.T.E.", "1. Anna  10", "2. Ben  6", "Event 'Spring Cup' ended.", "Event 'Spring Cup' is over:"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
	for _, want := range []string{"Anna gave you props!", "You gave props to Ben.", "in the last 24 hours already", "can't give props to yourself", "Top 1 - All Time - Props"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
	if strings.Contains(out, "Gave +99") {
		t.Error("non-admin /lb was executed")
	}
	*sent = nil
	b.postAt, b.interval = time.Time{}, time.Second
	b.postDiscord(time.Now().Add(time.Hour))
	if len(*posted) < 2 {
		t.Errorf("expected the event result and the boards post, got %v", *posted)
	}
}

func TestForgedNameIsIgnored(t *testing.T) {
	b, sent, _ := newTestBoard(t)
	b.feed("[12:00:00] Anna joined (76561198000000001, admin), 1/10 players, x")
	b.feed("[12:00:00] Eve: joined (76561198000000003), 2/10 players, x")
	b.feed("[12:00:00] Eve joined (76561198000000004), 3/10 players, x")
	// Eve typed "/lb award ..." (refused by the server): the line reads like "Eve:" running /lb.
	b.feed("[12:00:00] [command] Eve: /lb award 500 Eve")
	if len(*sent) != 0 {
		t.Fatalf("forged command was answered: %v", *sent)
	}
}
