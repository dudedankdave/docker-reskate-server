package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestHandover(t *testing.T) {
	file := filepath.Join(t.TempDir(), "modules.json")
	t.Setenv("MODULES_FILE", file)
	reset := func(server string) {
		t.Setenv("RESKATE_SERVER", server)
		handover.at, handover.found, handover.mtime, handover.parts = time.Time{}, false, time.Time{}, map[string]any{}
	}
	reset("1")
	if external("leaderboard", "") || external("webhooks", "public") {
		t.Fatal("without the file everything is internal")
	}
	data := `{"leaderboard":"external","webhooks":{"public":"external"},"servers":{"6":{"leaderboard":"internal","webhooks":"external"}}}`
	if err := os.WriteFile(file, []byte(data), 0o644); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		server, part, scope string
		want                bool
	}{
		{"1", "leaderboard", "", true}, {"1", "webhooks", "public", true}, {"1", "webhooks", "admin", false},
		{"1", "announcer", "", false}, {"6", "leaderboard", "", false}, {"6", "webhooks", "admin", true},
	}
	for _, c := range cases {
		reset(c.server)
		if got := external(c.part, c.scope); got != c.want {
			t.Errorf("server %s %s/%s: got %v, want %v", c.server, c.part, c.scope, got, c.want)
		}
	}
}
