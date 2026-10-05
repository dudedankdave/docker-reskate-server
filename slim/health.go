package main

// Container health from the server log. The log is appended to across restarts, so a stale
// "is up on" line must not count: healthy = the most recent lifecycle marker is "is up on".

import (
	"bufio"
	"fmt"
	"os"
	"strings"
)

func healthMain() int {
	markers := []struct {
		text string
		ok   bool
	}{{"is up on", true}, {"Signing in to Steam", false}, {"Config problem", false}}
	f, err := os.Open(logPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "no log yet")
		return 1
	}
	defer f.Close()
	last, ok := "", false
	r := bufio.NewReaderSize(f, 1<<20)
	for {
		line, err := r.ReadString('\n')
		for _, m := range markers {
			if strings.Contains(line, m.text) {
				last, ok = m.text, m.ok
			}
		}
		if err != nil {
			break
		}
	}
	if last == "" {
		fmt.Fprintln(os.Stderr, "no startup markers in log")
		return 1
	}
	if !ok {
		fmt.Fprintf(os.Stderr, "last lifecycle event: %s\n", last)
		return 1
	}
	return 0
}
