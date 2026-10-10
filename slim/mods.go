package main

// Installs the Thunderstore mods/maps listed in MODS into /data/Mods before the server starts.
// Same behaviour as mods.py in the default image, see there for the rules:
//
//	MODS = Owner-Name | Owner-Name-1.2.3 | https://thunderstore.io/c/reskate/p/Owner/Name/ (comma/space separated)
//
// Dependencies from manifest.json are installed too. Only folders this installer created
// (marker .thunderstore.json) are ever replaced. Failures are logged and never block the server.

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const (
	modsDir     = "/data/Mods"
	modsTmp     = "/data/.mods-tmp" // same volume as Mods, so renames are atomic
	modsMarker  = ".thunderstore.json"
	modsEvents  = "/data/.mods-events" // install/update events, posted to Discord by the notifier
	maxUnpacked = 8 << 30              // refuse zips that unpack to more than 8 GiB
)

var (
	idRe     = regexp.MustCompile(`^([A-Za-z0-9_]+)-([A-Za-z0-9_]+)(?:-(\d+\.\d+\.\d+))?$`)
	urlRe    = regexp.MustCompile(`/(?:p|package)/(?:download/)?([A-Za-z0-9_]+)/([A-Za-z0-9_]+)(?:/(\d+\.\d+\.\d+))?`)
	splitRe  = regexp.MustCompile(`[\s,]+`)
	modsHTTP = &http.Client{}
)

func modsBase() string {
	if b := strings.TrimRight(os.Getenv("THUNDERSTORE_URL"), "/"); b != "" {
		return b
	}
	return "https://thunderstore.io"
}

func mlog(format string, a ...any) { fmt.Printf("[mods] "+format+"\n", a...) }

type pkgInfo struct {
	Version      string   `json:"version_number"`
	DownloadURL  string   `json:"download_url"`
	Dependencies []string `json:"dependencies"`
}

// httpGet retries transient failures (network errors, 5xx, 408, 429) like curl --retry 3.
func httpGet(url string, timeout time.Duration) (*http.Response, context.CancelFunc, error) {
	var lastErr error
	for attempt := 0; attempt < 4; attempt++ {
		if attempt > 0 {
			time.Sleep(2 * time.Second)
		}
		ctx, cancel := context.WithTimeout(context.Background(), timeout)
		req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
		req.Header.Set("User-Agent", "reskate-server-image")
		resp, err := modsHTTP.Do(req)
		if err != nil {
			cancel()
			lastErr = err
			continue
		}
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return resp, cancel, nil
		}
		resp.Body.Close()
		cancel()
		lastErr = fmt.Errorf("HTTP %d from %s", resp.StatusCode, url)
		if resp.StatusCode < 500 && resp.StatusCode != 408 && resp.StatusCode != 429 {
			break
		}
	}
	return nil, nil, lastErr
}

func modMeta(owner, name, version string) (*pkgInfo, error) {
	url := fmt.Sprintf("%s/api/experimental/package/%s/%s/", modsBase(), owner, name)
	if version != "" {
		url += version + "/"
	}
	resp, cancel, err := httpGet(url, 30*time.Second)
	if err != nil {
		return nil, err
	}
	defer cancel()
	defer resp.Body.Close()
	var wrap struct {
		Latest *pkgInfo `json:"latest"`
		pkgInfo
	}
	if err := json.NewDecoder(resp.Body).Decode(&wrap); err != nil {
		return nil, err
	}
	if version == "" && wrap.Latest != nil {
		return wrap.Latest, nil
	}
	return &wrap.pkgInfo, nil
}

func readJSON(path string, v any) bool {
	raw, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	raw = bytes.TrimPrefix(raw, []byte{0xEF, 0xBB, 0xBF})
	return json.Unmarshal(raw, v) == nil
}

func manifestVersion(folder string) string {
	var m struct {
		Version string `json:"version_number"`
	}
	readJSON(filepath.Join(folder, "manifest.json"), &m)
	return m.Version
}

func writeMarker(folder, owner, name, version string) {
	raw, _ := json.Marshal(map[string]string{"owner": owner, "name": name, "version": version})
	_ = os.WriteFile(filepath.Join(folder, modsMarker), raw, 0o644)
}

// recordModEvent hands an install/update over to the Discord sidecar (only when a webhook is set).
func recordModEvent(owner, name, old, version string, maps []string) {
	if os.Getenv("DISCORD_WEBHOOK") == "" && os.Getenv("DISCORD_WEBHOOK_ADMIN") == "" {
		return
	}
	raw, _ := json.Marshal(map[string]any{"owner": owner, "name": name, "from": old, "to": version, "maps": maps})
	f, err := os.OpenFile(modsEvents, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	f.Write(append(raw, '\n'))
}

type modUpdate struct{ owner, name, latest, have string }

// pendingModUpdates lists unpinned MODS entries whose installed version is older than the latest.
func pendingModUpdates(spec string) []modUpdate {
	var out []modUpdate
	for _, token := range splitRe.Split(strings.TrimSpace(spec), -1) {
		p := parseMod(token)
		if token == "" || p == nil || p[3] != "" { // invalid or pinned: nothing to announce
			continue
		}
		m, err := modMeta(p[1], p[2], "")
		if err != nil {
			mlog("%s: update check failed: %v", token, err)
			continue
		}
		have := manifestVersion(filepath.Join(modsDir, p[2]))
		if have != "" && cmpVersion(m.Version, have) > 0 {
			out = append(out, modUpdate{p[1], p[2], m.Version, have})
		}
	}
	return out
}

func unsafeName(n string) bool {
	n = strings.ReplaceAll(n, "\\", "/")
	if strings.HasPrefix(n, "/") {
		return true
	}
	for _, part := range strings.Split(n, "/") {
		if part == ".." {
			return true
		}
	}
	return false
}

// extract refuses path traversal, links/special files and zip bombs.
func extract(archive, dest string) error {
	zr, err := zip.OpenReader(archive)
	if err != nil {
		return fmt.Errorf("not a valid zip file")
	}
	defer zr.Close()
	var total uint64
	for _, f := range zr.File {
		if unsafeName(f.Name) {
			return fmt.Errorf("unsafe path in zip: %q", f.Name)
		}
		if m := f.Mode(); m&os.ModeSymlink != 0 || (!m.IsRegular() && !m.IsDir()) {
			return fmt.Errorf("zip contains a link or special file")
		}
		total += f.UncompressedSize64
	}
	if total > maxUnpacked {
		return fmt.Errorf("zip unpacks to more than 8 GiB")
	}
	var written int64
	for _, f := range zr.File {
		path := filepath.Join(dest, filepath.FromSlash(strings.ReplaceAll(f.Name, "\\", "/")))
		if path != dest && !strings.HasPrefix(path, dest+string(os.PathSeparator)) {
			return fmt.Errorf("unsafe path in zip: %q", f.Name)
		}
		if f.FileInfo().IsDir() || strings.HasSuffix(f.Name, "/") {
			if err := os.MkdirAll(path, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return err
		}
		perm := os.FileMode(0o644)
		if f.Mode()&0o111 != 0 {
			perm = 0o755
		}
		rc, err := f.Open()
		if err != nil {
			return err
		}
		out, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, perm)
		if err != nil {
			rc.Close()
			return err
		}
		n, err := io.Copy(out, io.LimitReader(rc, maxUnpacked-written+1))
		rc.Close()
		out.Close()
		written += n
		if err != nil {
			return err
		}
		if written > maxUnpacked {
			return fmt.Errorf("zip unpacks to more than 8 GiB")
		}
	}
	return nil
}

func installMod(owner, name, version, url, old string) error {
	target := filepath.Join(modsDir, name)
	work := filepath.Join(modsTmp, name)
	_ = os.RemoveAll(modsTmp)
	if err := os.MkdirAll(work, 0o755); err != nil {
		return err
	}
	defer os.RemoveAll(modsTmp)
	archive := filepath.Join(modsTmp, fmt.Sprintf("%s-%s.zip", name, version))
	mlog("%s-%s %s: downloading", owner, name, version)
	resp, cancel, err := httpGet(url, time.Hour)
	if err != nil {
		return fmt.Errorf("download failed: %v", err)
	}
	defer cancel()
	f, err := os.Create(archive)
	if err != nil {
		resp.Body.Close()
		return err
	}
	_, err = io.Copy(f, resp.Body)
	resp.Body.Close()
	f.Close()
	if err != nil {
		return fmt.Errorf("download failed: %v", err)
	}
	if err := extract(archive, work); err != nil {
		return err
	}
	_ = os.Remove(archive)
	if _, err := os.Stat(filepath.Join(work, "manifest.json")); err != nil {
		return fmt.Errorf("package has no manifest.json")
	}
	writeMarker(work, owner, name, version)
	backup := target + ".old"
	if st, err := os.Stat(target); err == nil && st.IsDir() {
		_ = os.RemoveAll(backup)
		if err := os.Rename(target, backup); err != nil {
			return err
		}
	}
	if err := os.MkdirAll(modsDir, 0o755); err != nil {
		return err
	}
	if err := os.Rename(work, target); err != nil {
		return err
	}
	_ = os.RemoveAll(backup)
	var lv struct {
		Levels []struct {
			DisplayName string `json:"displayName"`
		} `json:"levels"`
	}
	var maps []string
	if readJSON(filepath.Join(target, "reskate-levels.json"), &lv) {
		for _, l := range lv.Levels {
			if l.DisplayName != "" {
				maps = append(maps, l.DisplayName)
			}
		}
	}
	msg := fmt.Sprintf("%s-%s %s: installed", owner, name, version)
	if len(maps) > 0 {
		msg += ", maps: " + strings.Join(maps, ", ") + " (use as MAP)"
	}
	mlog("%s", msg)
	recordModEvent(owner, name, old, version, maps)
	return nil
}

func ensureMod(owner, name, version string, update, dependency bool, seen map[string]bool) error {
	key := owner + "/" + name
	if seen[key] {
		return nil
	}
	seen[key] = true
	label := owner + "-" + name
	m, err := modMeta(owner, name, version)
	if err != nil {
		return err
	}
	want := m.Version
	target := filepath.Join(modsDir, name)
	st, statErr := os.Stat(target)
	exists := statErr == nil && st.IsDir()
	have := ""
	managed := false
	if exists {
		have = manifestVersion(target)
		var mk map[string]string
		managed = readJSON(filepath.Join(target, modsMarker), &mk)
	}
	switch {
	case exists && have == want:
		if !managed {
			writeMarker(target, owner, name, want)
		}
		mlog("%s %s: up to date", label, want)
	case exists && have != "" && !managed:
		mlog("%s: %s/%s (%s) was not installed by MODS, leaving it alone", label, modsDir, name, have)
	case exists && have != "" && dependency:
		mlog("%s: needs %s, %s is installed, leaving it", label, want, have)
	case exists && have != "" && version == "" && cmpVersion(have, want) > 0:
		mlog("%s: %s installed, Thunderstore reports older %s (stale?), leaving it", label, have, want)
	case exists && have != "" && version == "" && !update:
		mlog("%s: %s installed, newer %s available (MODS_UPDATE=false)", label, have, want)
	default:
		if err := installMod(owner, name, want, m.DownloadURL, have); err != nil {
			return err
		}
	}
	for _, dep := range m.Dependencies {
		if d := idRe.FindStringSubmatch(dep); d != nil {
			if err := ensureMod(d[1], d[2], d[3], update, true, seen); err != nil {
				mlog("%s: dependency %s: %v", label, dep, err)
			}
		}
	}
	return nil
}

func parseMod(token string) []string {
	if strings.HasPrefix(token, "http://") || strings.HasPrefix(token, "https://") {
		return urlRe.FindStringSubmatch(token)
	}
	return idRe.FindStringSubmatch(token)
}

func installMods(spec string, update bool) {
	defer func() {
		if r := recover(); r != nil {
			mlog("disabled: %v", r) // the server must start even if this breaks
		}
	}()
	seen := map[string]bool{}
	for _, token := range splitRe.Split(strings.TrimSpace(spec), -1) {
		if token == "" {
			continue
		}
		p := parseMod(token)
		if p == nil {
			mlog("%s: not a valid package (use Owner-Name, Owner-Name-1.2.3 or a Thunderstore URL)", token)
			continue
		}
		if err := ensureMod(p[1], p[2], p[3], update, false, seen); err != nil {
			_ = os.RemoveAll(modsTmp)
			mlog("%s: %v", token, err)
		}
	}
}
