// reskate is the single static binary of the distroless image. It replaces the Python
// entrypoint/notifier/healthcheck and the curl the server shells out to:
//
//	reskate              env vars -> /data/ReSkateServer.json, then runs the server under the
//	                     supervisor with the Discord feeds and updater beside it
//	reskate mods         install the MODS from Thunderstore now (docker exec ... /app/reskate mods)
//	reskate mods gc [d]  remove shared store versions no server started with for d days (default 30)
//	reskate healthcheck  container health from /data/ReSkateServer.log
//	curl ...             (symlink) the small subset of curl the server uses
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
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
		case "mods":
			if len(os.Args) > 2 && os.Args[2] == "gc" {
				days := 30.0
				if len(os.Args) > 3 {
					d, err := strconv.ParseFloat(os.Args[3], 64)
					if err != nil {
						die("days must be a number: %s", os.Args[3])
					}
					days = d
				}
				storeGC(days)
				return
			}
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
