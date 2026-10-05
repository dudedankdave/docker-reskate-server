package main

// The server reads the ReSkate global ban list by running, through sh:
//
//	curl --silent --show-error --fail --max-time 15 --max-filesize N --proto =https
//	     --user-agent ReSkateServer/1 <url>
//
// This is a minimal stand-in for exactly that, so the image needs no curl. Anything it does
// not know is refused loudly (exit 2) instead of being guessed at.

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

func curlMain(args []string) int {
	fail, maxTime, maxSize, ua, proto := false, 0, int64(0), "curl/8", ""
	var url string
	needVal := map[string]bool{"--max-time": true, "-m": true, "--max-filesize": true, "--proto": true, "--user-agent": true, "-A": true}
	for i := 0; i < len(args); i++ {
		a, val := args[i], ""
		hasVal := false
		if strings.HasPrefix(a, "--") {
			if k, v, ok := strings.Cut(a, "="); ok {
				a, val, hasVal = k, v, true
			}
		} else if len(a) > 2 && a[0] == '-' && !needVal[a[:2]] { // -fsS
			var ex []string
			for _, c := range a[1:] {
				ex = append(ex, "-"+string(c))
			}
			args = append(args[:i+1], append(ex, args[i+1:]...)...)
			continue
		}
		next := func() string {
			if hasVal {
				return val
			}
			if i+1 >= len(args) {
				fmt.Fprintf(os.Stderr, "curl: option %s: requires parameter\n", a)
				os.Exit(2)
			}
			i++
			return args[i]
		}
		switch a {
		case "--silent", "-s", "--show-error", "-S":
		case "--fail", "-f":
			fail = true
		case "--max-time", "-m":
			maxTime, _ = strconv.Atoi(next())
		case "--max-filesize":
			maxSize, _ = strconv.ParseInt(next(), 10, 64)
		case "--proto":
			proto = next()
		case "--user-agent", "-A":
			ua = next()
		default:
			if strings.HasPrefix(a, "-") {
				fmt.Fprintf(os.Stderr, "curl: option %s: is unknown\n", a)
				return 2
			}
			url = a
		}
	}
	if url == "" {
		fmt.Fprintln(os.Stderr, "curl: no URL specified")
		return 2
	}
	if !strings.HasPrefix(url, "https://") && (proto == "=https" || !strings.HasPrefix(url, "http://")) {
		fmt.Fprintln(os.Stderr, "curl: (1) Protocol not supported or disabled in libcurl")
		return 1
	}
	c := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	if maxTime > 0 {
		c.Timeout = time.Duration(maxTime) * time.Second
	}
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", ua)
	resp, err := c.Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "curl: (7) %v\n", err)
		return 7
	}
	defer resp.Body.Close()
	if fail && resp.StatusCode >= 400 {
		fmt.Fprintf(os.Stderr, "curl: (22) The requested URL returned error: %d\n", resp.StatusCode)
		return 22
	}
	var r io.Reader = resp.Body
	if maxSize > 0 {
		if resp.ContentLength > maxSize {
			fmt.Fprintln(os.Stderr, "curl: (63) Maximum file size exceeded")
			return 63
		}
		r = io.LimitReader(resp.Body, maxSize+1)
	}
	body, err := io.ReadAll(r)
	if err != nil {
		fmt.Fprintf(os.Stderr, "curl: (56) %v\n", err)
		return 56
	}
	if maxSize > 0 && int64(len(body)) > maxSize {
		fmt.Fprintln(os.Stderr, "curl: (63) Maximum file size exceeded")
		return 63
	}
	os.Stdout.Write(body)
	return 0
}
