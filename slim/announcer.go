package main

// Kick and ban announcements in chat, started by the entrypoint unless ANNOUNCE_KICKS=false.
// ReSkate tells only the admin who kicks or bans someone; this reads the server log and, for
// each admin kick or ban, types `say <name> was kicked.` / `say <name> was banned.` into the
// server console through the supervisor, like the notifier's countdowns. Failures are swallowed.

import (
	"os"
	"regexp"
	"strings"
	"syscall"
	"time"
)

var (
	// The server's replies to `kick` / `ban` (Server/server_commands.cpp). Vote kicks are not
	// matched: the server already says those in chat. A tagged line ("[chat] Server: ...", our
	// own `say`) never matches, so nothing is announced twice.
	kickedRe = regexp.MustCompile(`^\[\d\d:\d\d:\d\d\] ([^\[].*) was kicked until the server restarts\.$`)
	bannedRe = regexp.MustCompile(`^\[\d\d:\d\d:\d\d\] ([^\[].*) was banned\.$`)
)

// announcement is the chat line for one cleaned log line ("[hh:mm:ss] text"), or "".
func announcement(line string) string {
	line = strings.TrimRight(line, "\r")
	if m := kickedRe.FindStringSubmatch(line); m != nil {
		return strings.TrimSpace(m[1]) + " was kicked."
	}
	if m := bannedRe.FindStringSubmatch(line); m != nil {
		return strings.TrimSpace(m[1]) + " was banned."
	}
	return ""
}

func announcer(sup *supervisor) {
	s := &tailState{}
	if st, err := os.Stat(tailPath); err == nil { // only what the server writes from now on
		s.ino, s.pos = st.Sys().(*syscall.Stat_t).Ino, st.Size()
	}
	for {
		time.Sleep(time.Second)
		for _, line := range readNew(s) {
			if text := announcement(line); text != "" {
				sup.send("say " + text) // false while the server restarts: skip
			}
		}
	}
}
