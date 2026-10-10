package main

// ReSkate server files in /data/server, and installing new ReSkate releases into it (as updater.py).
// UPDATE_MODE=pinned runs the server in the image (/app); auto runs it from /data/server, filled
// from the image on first start and whenever the image holds a newer version, so an installed
// release survives the container being recreated. Releases come from the release's launcher.json:
// the Linux tarball is checked against its SHA-256, unpacked next to /data/server and its
// ReSkateServer checked against exe_sha256 before anything is swapped.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	versionFile = ".version"
	launcherURL = "https://github.com/" + repo + "/releases/latest/download/launcher.json"
)

var (
	imageDir   = "/app"
	serverDir  = "/data/server"
	stagingDir = "/data/server.new"
	oldDir     = "/data/server.old"
)

var (
	ours = map[string]bool{"entrypoint.py": true, "healthcheck.py": true, "notifier.py": true, "mods.py": true,
		"updater.py": true, "supervisor.py": true, "reskate": true, "__pycache__": true, versionFile: true}
	keep = map[string]bool{"ReSkateServer.json": true, "ReSkateServer.log": true, "Mods": true, "world-layers.json": true}
)

type release struct {
	Version   string `json:"version"`
	URL       string `json:"url"`
	SHA256    string `json:"sha256"`
	Size      int64  `json:"size"`
	ExeSHA256 string `json:"exe_sha256"`
}

func versionOf(dir string) string {
	raw, _ := os.ReadFile(filepath.Join(dir, versionFile))
	return strings.TrimSpace(string(raw))
}

func writeVersion(dir, v string) error {
	return os.WriteFile(filepath.Join(dir, versionFile), []byte(v+"\n"), 0o644)
}

// cmpVersion compares like Python tuples of the first four numbers: -1, 0 or 1.
func cmpVersion(a, b string) int {
	x, y := numbers(a), numbers(b)
	for i := 0; i < len(x) && i < len(y); i++ {
		if x[i] != y[i] {
			if x[i] > y[i] {
				return 1
			}
			return -1
		}
	}
	switch {
	case len(x) > len(y):
		return 1
	case len(x) < len(y):
		return -1
	}
	return 0
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func copyTree(src, dst string) error {
	if err := os.MkdirAll(dst, 0o755); err != nil {
		return err
	}
	entries, err := os.ReadDir(src)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if ours[e.Name()] {
			continue
		}
		s, d := filepath.Join(src, e.Name()), filepath.Join(dst, e.Name())
		info, err := os.Lstat(s)
		if err != nil {
			return err
		}
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			target, err := os.Readlink(s)
			if err != nil {
				return err
			}
			_ = os.RemoveAll(d)
			if err := os.Symlink(target, d); err != nil {
				return err
			}
		case info.IsDir():
			if err := copyTree(s, d); err != nil {
				return err
			}
		default:
			if err := copyFile(s, d, info.Mode().Perm()); err != nil {
				return err
			}
		}
	}
	return nil
}

func copyFile(src, dst string, mode os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	_ = os.Remove(dst)
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// prepareServer makes sure /data/server holds a runnable server and returns it.
func prepareServer(imageVersion string) (string, error) {
	have := ""
	if _, err := os.Stat(filepath.Join(serverDir, "ReSkateServer")); err == nil {
		have = versionOf(serverDir)
	}
	if have != "" && cmpVersion(have, imageVersion) >= 0 {
		if cmpVersion(have, imageVersion) > 0 {
			fmt.Printf("[update] running ReSkate %s from %s (installed by the updater; the image has %s)\n", have, serverDir, imageVersion)
		}
		return serverDir, nil
	}
	_ = os.RemoveAll(stagingDir)
	if err := copyTree(imageDir, stagingDir); err != nil {
		return "", err
	}
	if err := writeVersion(stagingDir, imageVersion); err != nil {
		return "", err
	}
	if err := swapIn(stagingDir); err != nil {
		return "", err
	}
	_ = os.RemoveAll(oldDir)
	was := ""
	if have != "" {
		was = " (was " + have + ")"
	}
	fmt.Printf("[update] %s set up from the image, ReSkate %s%s\n", serverDir, imageVersion, was)
	return serverDir, nil
}

// swapIn moves staged to /data/server, keeping the previous one as /data/server.old for a rollback.
func swapIn(staged string) error {
	_ = os.RemoveAll(oldDir)
	if _, err := os.Stat(serverDir); err == nil {
		if err := os.Rename(serverDir, oldDir); err != nil {
			return err
		}
	}
	return os.Rename(staged, serverDir)
}

func rollback() bool {
	if _, err := os.Stat(oldDir); err != nil {
		return false
	}
	_ = os.RemoveAll(stagingDir)
	if os.Rename(serverDir, stagingDir) != nil || os.Rename(oldDir, serverDir) != nil {
		return false
	}
	_ = os.RemoveAll(stagingDir)
	return true
}

// latestRelease is the newest ReSkate release's Linux server.
func latestRelease() (release, error) {
	var r release
	resp, cancel, err := httpGet(launcherURL, 30*time.Second)
	if err != nil {
		return r, err
	}
	defer cancel()
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return r, err
	}
	var l struct {
		ServerLinux release `json:"server_linux"`
	}
	if err := json.Unmarshal(bytes.TrimPrefix(raw, []byte("\xef\xbb\xbf")), &l); err != nil {
		return r, fmt.Errorf("launcher.json: %v", err)
	}
	if !versionRe.MatchString(l.ServerLinux.Version) {
		return r, fmt.Errorf("launcher.json: unexpected server_linux version %q", l.ServerLinux.Version)
	}
	return l.ServerLinux, nil
}

// stageRelease downloads, verifies and unpacks a release into /data/server.new (a copy of
// /data/server with the release unpacked over it).
func stageRelease(r release) (dir string, err error) {
	url := r.URL
	if name, ok := strings.CutPrefix(url, "asset:"); ok {
		url = fmt.Sprintf("https://github.com/%s/releases/download/v%s/%s", repo, r.Version, name)
	}
	if !strings.HasPrefix(url, "https://") {
		return "", fmt.Errorf("launcher.json: refusing download URL %q", url)
	}
	_ = os.RemoveAll(stagingDir)
	defer func() {
		if err != nil {
			_ = os.RemoveAll(stagingDir)
		}
	}()
	resp, cancel, err := httpGet(url, 10*time.Minute)
	if err != nil {
		return "", err
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 512<<20))
	resp.Body.Close()
	cancel()
	if err != nil {
		return "", err
	}
	if r.Size > 0 && int64(len(data)) != r.Size {
		return "", fmt.Errorf("download is %d bytes, launcher.json says %d", len(data), r.Size)
	}
	if sum := sha256.Sum256(data); hex.EncodeToString(sum[:]) != strings.ToLower(r.SHA256) {
		return "", fmt.Errorf("download does not match its SHA-256 in launcher.json")
	}
	if err := copyTree(serverDir, stagingDir); err != nil {
		return "", err
	}
	gz, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return "", err
	}
	tr := tar.NewReader(gz)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return "", err
		}
		_, rel, _ := strings.Cut(h.Name, "/") // ReSkateServer-Linux-x.y.z/<path>
		rel = strings.TrimSuffix(rel, "/")
		if rel == "" || keep[strings.Split(rel, "/")[0]] || strings.HasPrefix(rel, "/") ||
			strings.Contains("/"+rel+"/", "/../") {
			continue
		}
		dst := filepath.Join(stagingDir, rel)
		switch h.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(dst, 0o755); err != nil {
				return "", err
			}
		case tar.TypeReg:
			mode := os.FileMode(0o644)
			if h.Mode&0o111 != 0 {
				mode = 0o755
			}
			_ = os.MkdirAll(filepath.Dir(dst), 0o755)
			_ = os.Remove(dst)
			f, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
			if err != nil {
				return "", err
			}
			_, err = io.Copy(f, tr)
			if cerr := f.Close(); err == nil {
				err = cerr
			}
			if err != nil {
				return "", err
			}
		}
	}
	if r.ExeSHA256 != "" {
		if sum, err := fileSHA256(filepath.Join(stagingDir, "ReSkateServer")); err != nil || sum != strings.ToLower(r.ExeSHA256) {
			return "", fmt.Errorf("unpacked ReSkateServer does not match exe_sha256 in launcher.json")
		}
	}
	if err := writeVersion(stagingDir, r.Version); err != nil {
		return "", err
	}
	return stagingDir, nil
}
