package main

import (
	"os"
	"path/filepath"
	"testing"
)

// A server that went back from a pulled release keeps it over an image that is not newer.
func TestPrepareServerAfterPulledRelease(t *testing.T) {
	base := t.TempDir()
	imageDir, serverDir = filepath.Join(base, "app"), filepath.Join(base, "server")
	stagingDir, oldDir = filepath.Join(base, "server.new"), filepath.Join(base, "server.old")
	t.Cleanup(func() {
		imageDir, serverDir, stagingDir, oldDir = "/app", "/data/server", "/data/server.new", "/data/server.old"
	})
	setup := func(pulled string) {
		for _, d := range []string{imageDir, serverDir} {
			_ = os.RemoveAll(d)
			_ = os.MkdirAll(d, 0o755)
			_ = os.WriteFile(filepath.Join(d, "ReSkateServer"), []byte(filepath.Base(d)), 0o755)
		}
		_ = writeVersion(serverDir, "2.0.7")
		if pulled != "" {
			_ = os.WriteFile(filepath.Join(serverDir, pulledFile), []byte(pulled+"\n"), 0o644)
		}
	}
	for _, c := range []struct{ pulled, image, want string }{
		{"2.0.8", "2.0.8", "2.0.7"}, // went back from 2.0.8: the 2.0.8 image does not undo it
		{"2.0.8", "2.0.9", "2.0.9"}, // a newer image does
		{"", "2.0.8", "2.0.8"},      // no mark: as before
	} {
		setup(c.pulled)
		if _, err := prepareServer(c.image); err != nil {
			t.Fatal(err)
		}
		if got := versionOf(serverDir); got != c.want {
			t.Errorf("pulled %q, image %s: runs %s, want %s", c.pulled, c.image, got, c.want)
		}
		if c.want != "2.0.7" && pulledFrom(serverDir) != "" {
			t.Errorf("pulled %q, image %s: the mark stayed", c.pulled, c.image)
		}
	}
}
