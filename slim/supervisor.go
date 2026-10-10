package main

// Runs the ReSkate server as a child process instead of exec'ing it (as supervisor.py), so an
// update can restart it without the container restarting. Console input is passed through,
// except a bare `update` (install a waiting release now, UPDATE_MODE=auto). A server that dies
// within rollbackWindow of an update is rolled back. SIGTERM/SIGINT go to the server; when it
// exits on its own, so does the container.

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

const rollbackWindow = 3 * time.Minute

type pendingInstall struct{ dir, version string }

type supervisor struct {
	folder    string
	args      []string
	mu        sync.Mutex
	cmd       *exec.Cmd
	stdin     io.WriteCloser
	done      chan struct{} // closed when the current server process has exited
	pending   *pendingInstall
	stopping  bool
	updatedAt time.Time
	onUpdate  func()
	lastCmd   string // what was typed last and when, for the log feed's formatting
	lastAt    time.Time
	notify    func(kind string, d map[string]string)
}

func newSupervisor(folder string, args []string) *supervisor {
	var a []string
	for _, x := range args {
		if x != "--no-update" {
			a = append(a, x)
		}
	}
	return &supervisor{folder: folder, args: a, notify: func(string, map[string]string) {}}
}

func (s *supervisor) start() error {
	// steamclient.so is looked up in ~/.steam/sdk64: follow the folder the server runs from
	link := filepath.Join(os.Getenv("HOME"), ".steam/sdk64/steamclient.so")
	_ = os.Remove(link + ".tmp")
	if err := os.Symlink(filepath.Join(s.folder, "steamclient.so"), link+".tmp"); err == nil {
		_ = os.Rename(link+".tmp", link)
	}
	cmd := exec.Command(filepath.Join(s.folder, "ReSkateServer"), append([]string{"--config", configPath, "--no-update"}, s.args...)...)
	cmd.Dir = s.folder
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	env := []string{"LD_LIBRARY_PATH=" + s.folder}
	for _, kv := range os.Environ() {
		if !bytes.HasPrefix([]byte(kv), []byte("LD_LIBRARY_PATH=")) {
			env = append(env, kv)
		}
	}
	cmd.Env = env
	in, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	s.mu.Lock()
	s.cmd, s.stdin, s.done = cmd, in, make(chan struct{})
	s.mu.Unlock()
	return nil
}

// send types a console command into the server; false if it is not running.
func (s *supervisor) send(line string) bool {
	return s.write([]byte(line + "\n"))
}

func (s *supervisor) write(b []byte) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stdin == nil {
		return false
	}
	_, err := s.stdin.Write(b)
	return err == nil
}

// install stops the server; the main loop swaps the staged release in and starts it again.
func (s *supervisor) install(dir, version string) {
	s.mu.Lock()
	s.pending = &pendingInstall{dir, version}
	cmd, done := s.cmd, s.done
	s.mu.Unlock()
	if !s.send("quit") {
		_ = cmd.Process.Signal(syscall.SIGTERM)
	}
	go func() {
		for _, step := range []struct {
			wait time.Duration
			sig  os.Signal
		}{{30 * time.Second, syscall.SIGTERM}, {15 * time.Second, syscall.SIGKILL}} {
			select {
			case <-done:
				return
			case <-time.After(step.wait):
				_ = cmd.Process.Signal(step.sig)
			}
		}
	}()
}

func (s *supervisor) relay() {
	r := bufio.NewReader(os.Stdin)
	for {
		line, err := r.ReadBytes('\n')
		if len(line) > 0 {
			if string(bytes.ToLower(bytes.TrimSpace(line))) == "update" && s.onUpdate != nil {
				fmt.Println("[update] checking for a new ReSkate release now")
				s.onUpdate()
			} else {
				s.mu.Lock()
				s.lastCmd, s.lastAt = string(bytes.TrimSpace(line)), time.Now()
				s.mu.Unlock()
				s.write(line)
			}
		}
		if err != nil {
			return // keep the server's stdin open after our own closes
		}
	}
}

func (s *supervisor) run() {
	sigs := make(chan os.Signal, 4)
	signal.Notify(sigs, syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP)
	go func() {
		for sig := range sigs {
			s.mu.Lock()
			s.stopping = true
			cmd := s.cmd
			s.mu.Unlock()
			if cmd != nil && cmd.Process != nil {
				_ = cmd.Process.Signal(sig)
			}
		}
	}()
	if err := s.start(); err != nil {
		die("cannot start server: %v", err)
	}
	go s.relay()
	for {
		_ = s.cmd.Wait()
		close(s.done)
		s.mu.Lock()
		stopping, pending := s.stopping, s.pending
		s.pending = nil
		s.mu.Unlock()
		if stopping {
			break
		}
		code := fmt.Sprint(s.cmd.ProcessState.ExitCode())
		if pending != nil {
			if err := swapIn(pending.dir); err != nil {
				s.notify("failed", map[string]string{"version": pending.version, "error": "could not swap in the new files: " + err.Error()})
			} else {
				s.updatedAt = time.Now()
				s.notify("installed", map[string]string{"version": pending.version})
			}
		} else if !s.updatedAt.IsZero() && time.Since(s.updatedAt) < rollbackWindow {
			s.updatedAt = time.Time{}
			failed := versionOf(serverDir)
			if !rollback() {
				s.notify("exited", map[string]string{"code": code})
				time.Sleep(2 * time.Second)
				break
			}
			s.notify("rolled_back", map[string]string{"version": failed, "code": code, "now": versionOf(serverDir)})
		} else {
			s.notify("exited", map[string]string{"code": code})
			time.Sleep(2 * time.Second) // let the notifier post it
			break
		}
		if err := s.start(); err != nil {
			die("cannot start server: %v", err)
		}
	}
	st := s.cmd.ProcessState
	if ws, ok := st.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		os.Exit(128 + int(ws.Signal()))
	}
	os.Exit(st.ExitCode())
}
