// reskate is the single static binary of the distroless image. It replaces the Python
// entrypoint/notifier/healthcheck and the curl the server shells out to:
//
//	reskate              env vars -> /data/ReSkateServer.json, Discord sidecar, exec server
//	reskate notifier     Discord sidecar (started by the entrypoint)
//	reskate announcer    kick/ban announcements in chat (started by the entrypoint)
//	reskate mods         install the MODS from Thunderstore now (docker exec ... /app/reskate mods)
//	reskate healthcheck  container health from /data/ReSkateServer.log
//	curl ...             (symlink) the small subset of curl the server uses
package main

import (
	"fmt"
	"os"
	"path/filepath"
)

func die(format string, a ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", a...)
	os.Exit(1)
}

func main() {
	if filepath.Base(os.Args[0]) == "curl" {
		os.Exit(curlMain(os.Args[1:]))
	}
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "notifier":
			notifierMain()
			return
		case "announcer":
			announcerMain()
			return
		case "mods":
			update := true
			if u, ok := env("MODS_UPDATE"); ok {
				update = asBool("MODS_UPDATE", u)
			}
			if v, ok := env("MODS"); ok {
				installMods(v, update)
			}
			return
		case "healthcheck":
			os.Exit(healthMain())
		case "curl":
			os.Exit(curlMain(os.Args[2:]))
		}
	}
	entrypointMain(os.Args[1:])
}
