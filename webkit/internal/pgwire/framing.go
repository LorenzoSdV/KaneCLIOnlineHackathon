// Package pgwire reads raw PostgreSQL wire-protocol frames without interpreting
// them, so a proxy can forward bytes unchanged and decode only what it records.
package pgwire

import (
	"encoding/binary"
	"fmt"
	"io"
)

// Special startup-phase request codes (first int32 after the length).
const (
	SSLRequestCode    uint32 = 80877103
	GSSEncRequestCode uint32 = 80877104
	CancelRequestCode uint32 = 80877102
)

// maxFrame guards against reading garbage as a huge length.
const maxFrame = 1 << 30

// ReadStartup reads one untyped startup-phase frame: int32 length, then payload.
// It returns the full raw frame and the protocol/request code.
func ReadStartup(r io.Reader) (raw []byte, code uint32, err error) {
	var hdr [4]byte
	if _, err = io.ReadFull(r, hdr[:]); err != nil {
		return nil, 0, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n < 8 || n > maxFrame {
		return nil, 0, fmt.Errorf("invalid startup frame length %d", n)
	}
	raw = make([]byte, n)
	copy(raw, hdr[:])
	if _, err = io.ReadFull(r, raw[4:]); err != nil {
		return nil, 0, err
	}
	return raw, binary.BigEndian.Uint32(raw[4:8]), nil
}

// StartupParams decodes the key/value parameters of a StartupMessage frame.
func StartupParams(raw []byte) map[string]string {
	params := map[string]string{}
	if len(raw) < 8 {
		return params
	}
	b := raw[8:]
	for {
		k, rest, ok := cstring(b)
		if !ok || k == "" {
			return params
		}
		v, rest2, ok := cstring(rest)
		if !ok {
			return params
		}
		params[k] = v
		b = rest2
	}
}

func cstring(b []byte) (string, []byte, bool) {
	for i, c := range b {
		if c == 0 {
			return string(b[:i]), b[i+1:], true
		}
	}
	return "", nil, false
}

// ReadMessage reads one typed frame: type byte, int32 length, body.
// raw is the complete frame (for forwarding), body excludes type and length.
func ReadMessage(r io.Reader) (typ byte, body []byte, raw []byte, err error) {
	var hdr [5]byte
	if _, err = io.ReadFull(r, hdr[:]); err != nil {
		return 0, nil, nil, err
	}
	n := binary.BigEndian.Uint32(hdr[1:5])
	if n < 4 || n > maxFrame {
		return 0, nil, nil, fmt.Errorf("invalid frame length %d for type %q", n, hdr[0])
	}
	raw = make([]byte, 1+n)
	copy(raw, hdr[:])
	if _, err = io.ReadFull(r, raw[5:]); err != nil {
		return 0, nil, nil, err
	}
	return hdr[0], raw[5:], raw, nil
}

// QueryFrame builds a simple-protocol Query ('Q') frame for sql.
func QueryFrame(sql string) []byte {
	n := 4 + len(sql) + 1
	frame := make([]byte, 1+n)
	frame[0] = 'Q'
	binary.BigEndian.PutUint32(frame[1:5], uint32(n))
	copy(frame[5:], sql)
	return frame
}
