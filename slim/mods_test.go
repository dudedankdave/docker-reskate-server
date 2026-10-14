package main

import (
	"io"
	"os"
	"path/filepath"
	"testing"
)

// logMapsIn prints one line per map of an installed mod, as mods.py does, for the status cards.
func TestLogMaps(t *testing.T) {
	dir := t.TempDir()
	mod := filepath.Join(dir, "Skate_3_Improved")
	_ = os.MkdirAll(mod, 0o755)
	_ = os.WriteFile(filepath.Join(mod, "manifest.json"), []byte(`{"version_number":"1.0.8"}`), 0o644)
	_ = os.WriteFile(filepath.Join(mod, "reskate-levels.json"),
		[]byte(`{"levels":[{"displayName":"Skate3Improved"},{"name":"no display name"}]}`), 0o644)

	r, w, _ := os.Pipe()
	stdout := os.Stdout
	os.Stdout = w
	logMapsIn(mod, "333", "Skate_3_Improved")
	logMapsIn(filepath.Join(dir, "Not_Installed"), "333", "Not_Installed")
	os.Stdout = stdout
	w.Close()
	out, _ := io.ReadAll(r)
	if want := "[mods] map Skate3Improved: 333-Skate_3_Improved 1.0.8\n"; string(out) != want {
		t.Errorf("got %q, want %q", out, want)
	}
}
