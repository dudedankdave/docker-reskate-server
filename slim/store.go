package main

// Shared map/mod store, same as mods.py in the default image. MODS_STORE is `shared` (default: the
// volume mounted at /mods) or a path for a store of this server's own. Every package version is
// downloaded once into <store>/packages/<Owner>-<Name>-<version>
// and each server only gets a symlink /data/Mods/<Owner>-<Name> to the version it wants. A version is
// never changed after it is written, so a server still running (or draining players) on an older
// map keeps its files while another one already moved on, and a lock per version makes servers
// starting together wait for one download instead of repeating it.
// `reskate mods gc [days]` removes versions no server has started with for that long (never the
// newest of a package).

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

const lockWait = 4000 * time.Second // how long to wait for another server's download of the same version

const sharedStore = "/mods" // where the shared volume is mounted

var warnStore sync.Once

// storeSetting reads MODS_STORE: `shared` (default) = the volume at /mods, used only while one is
// mounted there (needsMount); an absolute path = a store at that path for this server, used as it is.
func storeSetting() (dir string, needsMount bool) {
	raw := strings.TrimSpace(os.Getenv("MODS_STORE"))
	if raw == "" || strings.EqualFold(raw, "shared") {
		return sharedStore, true
	}
	path := filepath.Clean(raw)
	if filepath.IsAbs(path) && path != "/" && !strings.HasPrefix(path+"/", modsDir+"/") {
		return path, false
	}
	warnStore.Do(func() {
		mlog("MODS_STORE=%q is neither 'shared' nor an absolute path outside %s, using shared", raw, modsDir)
	})
	return sharedStore, true
}

func storeDir() string {
	dir, _ := storeSetting()
	return dir
}

func storePackages() string { return storeDir() + "/packages" }

// storeShared is true when packages go into the store: always for a path, for `shared` only while
// a volume is mounted at /mods (like Python's os.path.ismount), else each server keeps its own copies.
func storeShared() bool {
	dir, needsMount := storeSetting()
	if !needsMount {
		return true
	}
	var st, parent syscall.Stat_t
	if syscall.Lstat(dir, &st) != nil || st.Mode&syscall.S_IFMT != syscall.S_IFDIR {
		return false
	}
	if syscall.Lstat(dir+"/..", &parent) != nil {
		return false
	}
	return st.Dev != parent.Dev || st.Ino == parent.Ino
}

// storeLock takes the exclusive lock of one package version; call the returned func to release it.
func storeLock(name string) (func(), error) {
	dir := storeDir() + "/.locks"
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, name+".lock"), os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(lockWait)
	for syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		if time.Now().After(deadline) {
			f.Close()
			return nil, fmt.Errorf("another server has been installing this for too long")
		}
		time.Sleep(2 * time.Second)
	}
	return func() { f.Close() }, nil // closing releases the lock
}

func storedPath(owner, name, version string) string {
	return fmt.Sprintf("%s/%s-%s-%s", storePackages(), owner, name, version)
}

// cleanStoreTmp removes leftovers of installs that died half way.
func cleanStoreTmp(tmp string) {
	entries, _ := os.ReadDir(tmp)
	for _, e := range entries {
		if info, err := e.Info(); err == nil && time.Since(info.ModTime()) > 2*time.Hour {
			_ = os.RemoveAll(filepath.Join(tmp, e.Name()))
		}
	}
}

// fillStore makes sure the store holds this version, downloading it once however many servers
// ask. adopt is a folder this installer wrote earlier with exactly this version: copied, not
// downloaded.
func fillStore(owner, name, version, url, adopt string) (string, error) {
	final := storedPath(owner, name, version)
	unlock, err := storeLock(fmt.Sprintf("%s-%s-%s", owner, name, version))
	if err != nil {
		return "", err
	}
	defer unlock()
	if _, err := os.Stat(filepath.Join(final, modsMarker)); err == nil { // another server finished while we waited
		return final, nil
	}
	tmp := storeDir() + "/.tmp"
	if err := os.MkdirAll(storePackages(), 0o755); err != nil {
		return "", err
	}
	if err := os.MkdirAll(tmp, 0o755); err != nil {
		return "", err
	}
	cleanStoreTmp(tmp)
	work := fmt.Sprintf("%s/%s-%s-%s.%d", tmp, owner, name, version, os.Getpid())
	_ = os.RemoveAll(work)
	if adopt != "" {
		err = copyModTree(adopt, work)
		if err != nil {
			err = fmt.Errorf("copying into the shared store failed: %v", err)
		}
	} else if err = os.MkdirAll(work, 0o755); err == nil {
		err = unpackMod(owner, name, version, url, work)
	}
	if err != nil {
		_ = os.RemoveAll(work)
		return "", err
	}
	_ = os.RemoveAll(final)                        // a folder without marker is a dead leftover
	if err := os.Rename(work, final); err != nil { // complete or absent, never half
		_ = os.RemoveAll(work)
		return "", err
	}
	return final, nil
}

// copyModTree copies a folder with its files, sub folders and symlinks.
func copyModTree(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		out := filepath.Join(dst, rel)
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(link, out)
		case info.IsDir():
			return os.MkdirAll(out, info.Mode().Perm()|0o700)
		case !info.Mode().IsRegular():
			return nil
		}
		in, err := os.Open(path)
		if err != nil {
			return err
		}
		defer in.Close()
		f, err := os.OpenFile(out, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, info.Mode().Perm())
		if err != nil {
			return err
		}
		if _, err := io.Copy(f, in); err != nil {
			f.Close()
			return err
		}
		if err := f.Close(); err != nil {
			return err
		}
		return os.Chtimes(out, info.ModTime(), info.ModTime())
	})
}

func isLink(path string) bool {
	st, err := os.Lstat(path)
	return err == nil && st.Mode()&os.ModeSymlink != 0
}

// touchStore marks the store folder a /data/Mods entry points at as in use right now (what gc goes by).
func touchStore(path string) {
	if real, err := filepath.EvalSymlinks(path); err == nil {
		now := time.Now()
		_ = os.Chtimes(real, now, now)
	}
}

// linkMod points /data/Mods/<name> at a store folder; an earlier symlink is swapped atomically, a
// folder this installer made earlier is moved aside and removed.
func linkMod(target, final string) error {
	if err := os.MkdirAll(modsDir, 0o755); err != nil {
		return err
	}
	tmp := target + ".link"
	_ = os.Remove(tmp)
	if err := os.Symlink(final, tmp); err != nil {
		return err
	}
	old := ""
	if st, err := os.Lstat(target); err == nil && st.IsDir() {
		old = target + ".old"
		_ = os.RemoveAll(old)
		if err := os.Rename(target, old); err != nil {
			_ = os.Remove(tmp)
			return err
		}
	}
	if err := os.Rename(tmp, target); err != nil {
		return err
	}
	if old != "" {
		_ = os.RemoveAll(old)
	}
	touchStore(target)
	return nil
}

// storeGC removes store folders nobody started with for days days, except the newest of each
// package. A running server keeps using the version it started with, so keep days above how
// long a server may run without a restart.
func storeGC(days float64) {
	type found struct{ owner, name, version, path string }
	var all []found
	newest := map[string]found{}
	entries, _ := os.ReadDir(storePackages())
	for _, e := range entries {
		path := filepath.Join(storePackages(), e.Name())
		var mk struct{ Owner, Name, Version string }
		if !e.IsDir() || !readJSON(filepath.Join(path, modsMarker), &mk) {
			continue
		}
		f := found{mk.Owner, mk.Name, mk.Version, path}
		all = append(all, f)
		key := f.owner + "/" + f.name
		if n, ok := newest[key]; !ok || cmpVersion(f.version, n.version) > 0 {
			newest[key] = f
		}
	}
	for _, f := range all {
		if newest[f.owner+"/"+f.name].path == f.path {
			continue
		}
		info, err := os.Stat(f.path)
		if err != nil {
			continue
		}
		age := time.Since(info.ModTime()).Hours() / 24
		if age < days {
			continue
		}
		unlock, err := storeLock(fmt.Sprintf("%s-%s-%s", f.owner, f.name, f.version))
		if err != nil {
			mlog("%s: %v", filepath.Base(f.path), err)
			continue
		}
		_ = os.RemoveAll(f.path)
		unlock()
		mlog("removed %s (unused for %.0f days)", filepath.Base(f.path), age)
	}
}
