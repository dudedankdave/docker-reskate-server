package main

import "testing"

func TestAnnouncement(t *testing.T) {
	for line, want := range map[string]string{
		"[20:00:00] Bob was kicked until the server restarts.": "Bob was kicked.",
		"[20:00:00] Bob was kicked for 30 minutes.":            "Bob was kicked for 30 minutes.",
		"[20:00:00] Bob was kicked for 1 minute.":              "Bob was kicked for 1 minute.",
		"[20:00:00] Bob was banned.":                           "Bob was banned.",
		"[20:00:00] [chat] Server: Bob was kicked.":            "",
	} {
		if got := announcement(line); got != want {
			t.Errorf("announcement(%q) = %q, want %q", line, got, want)
		}
	}
}
