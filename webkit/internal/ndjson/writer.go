// Package ndjson writes one JSON event per line, shared by every webkit recorder.
//
// Every event carries "src" (which recorder wrote it), "ev" (event kind) and
// "ts_ns" (wall-clock nanoseconds since the Unix epoch, taken when the event is
// written). All recorders run on the same host clock, so timestamps from
// different sources are comparable.
package ndjson

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Writer appends events to a file. It is safe for concurrent use.
type Writer struct {
	mu  sync.Mutex
	f   *os.File
	src string
}

// Open creates (or appends to) the file at path.
func Open(path, src string) (*Writer, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("create output dir: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	return &Writer{f: f, src: src}, nil
}

// Now returns the current wall-clock time in nanoseconds.
func Now() int64 { return time.Now().UnixNano() }

// Emit writes one event. Fields may not use the keys "src", "ev" or "ts_ns".
func (w *Writer) Emit(ev string, fields map[string]any) {
	line := make(map[string]any, len(fields)+3)
	for k, v := range fields {
		line[k] = v
	}
	line["src"] = w.src
	line["ev"] = ev
	line["ts_ns"] = Now()
	data, err := json.Marshal(line)
	if err != nil {
		data, _ = json.Marshal(map[string]any{
			"src": w.src, "ev": "encode_error", "ts_ns": Now(), "error": err.Error(), "for_ev": ev,
		})
	}
	data = append(data, '\n')
	w.mu.Lock()
	defer w.mu.Unlock()
	// Each event is a single write, so a crash leaves at most one partial line.
	_, _ = w.f.Write(data)
}

// Close flushes and closes the file.
func (w *Writer) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.f.Close()
}
