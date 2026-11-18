// wk-dbdiff runs small concurrent SQL programs on a real PostgreSQL server and
// records each execution as an NDJSON trace, for differential validation of
// the WebDB.tla model (webkit/tla/difftest).
//
// Interleavings are deterministic: after sending one operation, the harness
// waits until every in-flight statement has either completed or is confirmed
// (via pg_stat_activity) to be waiting on a lock, and holds that state long
// enough for deadlock detection to fire. Only then is the next operation sent.
//
// Programs are random (-cases random) or the classic isolation-anomaly
// schedules after Hermitage (-cases hermitage), at a chosen isolation level.
//
//	wk-dbdiff -db postgres://wk:wk@localhost:55432/wk -cases random -iso RC -n 500 -seed 1 -out traces/
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"math/rand"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ---- program model -------------------------------------------------------

type where struct {
	Op  string `json:"op"` // all | k_eq | v_ge | v_eq
	Val int    `json:"val"`
}

type setOp struct {
	Op  string `json:"op"` // none | v_add | v_set
	Val int    `json:"val"`
}

type rowVals struct {
	V int `json:"v"`
	U int `json:"u"`
}

type row struct {
	K int `json:"k"`
	V int `json:"v"`
	U int `json:"u"`
}

type op struct {
	Kind  string // begin commit rollback select lock update delete insert
	Iso   string
	Where where
	Set   setOp
	Key   int
	Row   rowVals
}

func (o op) terminal() bool { return o.Kind == "commit" || o.Kind == "rollback" }

type program struct {
	name     string
	init     []row
	txns     [][]op
	schedule []int // fixed order of session picks (hermitage); nil = random
}

// ---- trace events --------------------------------------------------------

type startEv struct {
	Ev    string  `json:"ev"`
	Txn   string  `json:"txn"`
	Kind  string  `json:"kind"`
	Iso   string  `json:"iso"`
	Where where   `json:"where"`
	Set   setOp   `json:"set"`
	Key   int     `json:"key"`
	Row   rowVals `json:"row"`
}

type doneEv struct {
	Ev   string `json:"ev"`
	Txn  string `json:"txn"`
	Kind string `json:"kind"`
	Ok   bool   `json:"ok"`
	Code string `json:"code"`
	Tag  string `json:"tag"`
	Rows []row  `json:"rows"`
}

type initEv struct {
	Ev      string `json:"ev"`
	Rows    []row  `json:"rows"`
	Program string `json:"program"`
}

// ---- execution -----------------------------------------------------------

var isoSQL = map[string]string{"RC": "READ COMMITTED", "RR": "REPEATABLE READ", "SER": "SERIALIZABLE"}

func whereSQL(w where) (string, []any) {
	switch w.Op {
	case "k_eq":
		return "k = $1", []any{w.Val}
	case "v_ge":
		return "v >= $1", []any{w.Val}
	case "v_eq":
		return "v = $1", []any{w.Val}
	default:
		return "TRUE", nil
	}
}

func stmtSQL(o op) (string, []any) {
	cond, args := whereSQL(o.Where)
	n := len(args)
	switch o.Kind {
	case "select":
		return "SELECT k, v, u FROM t WHERE " + cond + " ORDER BY k", args
	case "lock":
		return "SELECT k, v, u FROM t WHERE " + cond + " ORDER BY k FOR UPDATE", args
	case "update":
		set := fmt.Sprintf("v = v + $%d", n+1)
		if o.Set.Op == "v_set" {
			set = fmt.Sprintf("v = $%d", n+1)
		}
		return "UPDATE t SET " + set + " WHERE " + cond + " RETURNING k, v, u", append(args, o.Set.Val)
	case "delete":
		return "DELETE FROM t WHERE " + cond + " RETURNING k, v, u", args
	case "insert":
		return "INSERT INTO t (k, v, u) VALUES ($1, $2, $3) RETURNING k, v, u", []any{o.Key, o.Row.V, o.Row.U}
	}
	panic("unknown kind " + o.Kind)
}

type result struct {
	sess int
	ev   doneEv
}

type session struct {
	name     string
	conn     *pgx.Conn
	pid      uint32
	ops      []op
	next     int
	inflight bool
	failed   bool // explicit transaction aborted by an error
}

func (s *session) remaining() bool { return s.next < len(s.ops) }

func execute(ctx context.Context, s *session, idx int, o op, out chan<- result) {
	ev := doneEv{Ev: "done", Txn: s.name, Kind: o.Kind, Ok: true, Rows: []row{}}
	var err error
	switch o.Kind {
	case "begin":
		_, err = s.conn.Exec(ctx, "BEGIN ISOLATION LEVEL "+isoSQL[o.Iso])
	case "commit", "rollback":
		var tag pgconn.CommandTag
		tag, err = s.conn.Exec(ctx, strings.ToUpper(o.Kind))
		ev.Tag = tag.String()
	default:
		sql, args := stmtSQL(o)
		var rows pgx.Rows
		rows, err = s.conn.Query(ctx, sql, args...)
		if err == nil {
			for rows.Next() {
				var r row
				if err = rows.Scan(&r.K, &r.V, &r.U); err != nil {
					break
				}
				ev.Rows = append(ev.Rows, r)
			}
			rows.Close()
			if err == nil {
				err = rows.Err()
			}
		}
	}
	if err != nil {
		var pgErr *pgconn.PgError
		ev.Ok, ev.Rows = false, []row{}
		if errors.As(err, &pgErr) {
			ev.Code = pgErr.Code
		} else {
			ev.Code = "client:" + err.Error()
		}
	}
	out <- result{sess: idx, ev: ev}
}

type runner struct {
	dsn     string
	monitor *pgx.Conn
	rng     *rand.Rand
}

var errStuck = errors.New("program did not settle")

// run executes one program and returns its trace lines.
func (r *runner) run(ctx context.Context, p program) ([]any, error) {
	if err := r.reset(ctx, p.init); err != nil {
		return nil, err
	}
	sessions := make([]*session, len(p.txns))
	for i, ops := range p.txns {
		c, err := pgx.Connect(ctx, r.dsn)
		if err != nil {
			return nil, err
		}
		sessions[i] = &session{name: fmt.Sprintf("T%d", i+1), conn: c, pid: c.PgConn().PID(), ops: ops}
	}
	defer func() {
		for _, s := range sessions {
			cctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			_ = s.conn.Close(cctx)
			cancel()
		}
	}()

	trace := []any{initEv{Ev: "init", Rows: p.init, Program: p.name}}
	results := make(chan result, 16)
	schedPos := 0

	for {
		var ready []int
		for i, s := range sessions {
			if s.remaining() && !s.inflight {
				ready = append(ready, i)
			}
		}
		if len(ready) == 0 {
			busy := false
			for _, s := range sessions {
				busy = busy || s.inflight
			}
			if !busy {
				return trace, nil
			}
			return nil, errStuck // everything left is waiting on something that never ends
		}

		var pick int
		if p.schedule != nil {
			if schedPos >= len(p.schedule) {
				return nil, fmt.Errorf("schedule exhausted")
			}
			pick = p.schedule[schedPos]
			schedPos++
			if !contains(ready, pick) {
				return nil, fmt.Errorf("schedule picks T%d, which is not ready", pick+1)
			}
		} else {
			pick = ready[r.rng.Intn(len(ready))]
		}

		s := sessions[pick]
		o := s.ops[s.next]
		s.next++
		if s.failed && !o.terminal() {
			continue // the harness skips statements after an error; COMMIT/ROLLBACK still run
		}
		trace = append(trace, startEv{Ev: "start", Txn: s.name, Kind: o.Kind, Iso: o.Iso,
			Where: o.Where, Set: o.Set, Key: o.Key, Row: o.Row})
		s.inflight = true
		go execute(ctx, s, pick, o, results)

		evs, err := r.settle(ctx, sessions, results)
		trace = append(trace, evs...)
		if err != nil {
			return nil, err
		}
	}
}

func contains(xs []int, x int) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// settle waits until every in-flight statement has completed or is waiting on a
// lock, stable across a window longer than deadlock_timeout.
func (r *runner) settle(ctx context.Context, sessions []*session, results chan result) ([]any, error) {
	var evs []any
	deadline := time.Now().Add(10 * time.Second)
	stableSince := time.Time{}
	for time.Now().Before(deadline) {
		progressed := false
	drain:
		for {
			select {
			case res := <-results:
				s := sessions[res.sess]
				s.inflight = false
				o := s.ops[s.next-1]
				if !res.ev.Ok && !o.terminal() && s.ops[0].Kind == "begin" {
					s.failed = true
				}
				if o.terminal() {
					s.failed = false
				}
				evs = append(evs, res.ev)
				progressed = true
			default:
				break drain
			}
		}
		var pids []uint32
		for _, s := range sessions {
			if s.inflight {
				pids = append(pids, s.pid)
			}
		}
		if len(pids) == 0 {
			return evs, nil
		}
		waiting, err := r.lockWaiters(ctx, pids)
		if err != nil {
			return evs, err
		}
		if len(waiting) == len(pids) && !progressed {
			if stableSince.IsZero() {
				stableSince = time.Now()
			} else if time.Since(stableSince) > 150*time.Millisecond {
				return evs, nil // all in-flight statements are waiting; deadlock check had time to fire
			}
		} else {
			stableSince = time.Time{}
		}
		time.Sleep(2 * time.Millisecond)
	}
	return evs, errStuck
}

func (r *runner) lockWaiters(ctx context.Context, pids []uint32) (map[uint32]bool, error) {
	ints := make([]int32, len(pids))
	for i, p := range pids {
		ints[i] = int32(p)
	}
	rows, err := r.monitor.Query(ctx,
		"SELECT pid FROM pg_stat_activity WHERE pid = ANY($1) AND wait_event_type = 'Lock'", ints)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[uint32]bool{}
	for rows.Next() {
		var pid int32
		if err := rows.Scan(&pid); err != nil {
			return nil, err
		}
		out[uint32(pid)] = true
	}
	return out, rows.Err()
}

func (r *runner) reset(ctx context.Context, init []row) error {
	if _, err := r.monitor.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS t (k int PRIMARY KEY, v int NOT NULL, u int UNIQUE);
		TRUNCATE t`); err != nil {
		return err
	}
	for _, x := range init {
		if _, err := r.monitor.Exec(ctx, "INSERT INTO t (k, v, u) VALUES ($1, $2, $3)", x.K, x.V, x.U); err != nil {
			return err
		}
	}
	return nil
}

// ---- program generation --------------------------------------------------

func randomProgram(rng *rand.Rand, iso string, name string) program {
	keys := rng.Perm(3)[:2]
	us := rng.Perm(3)
	var init []row
	for i, k := range keys {
		init = append(init, row{K: k + 1, V: rng.Intn(3), U: us[i] + 1})
	}
	sort.Slice(init, func(a, b int) bool { return init[a].K < init[b].K })

	pickIso := func() string {
		if iso == "mixed" {
			return []string{"RC", "RR", "SER"}[rng.Intn(3)]
		}
		return iso
	}
	randWhere := func() where {
		switch x := rng.Intn(20); {
		case x < 4:
			return where{Op: "all"}
		case x < 14:
			return where{Op: "k_eq", Val: 1 + rng.Intn(3)}
		case x < 17:
			return where{Op: "v_ge", Val: rng.Intn(3)}
		default:
			return where{Op: "v_eq", Val: rng.Intn(3)}
		}
	}
	randStmt := func() op {
		switch x := rng.Intn(100); {
		case x < 20:
			return op{Kind: "select", Where: randWhere(), Set: setOp{Op: "none"}}
		case x < 35:
			return op{Kind: "lock", Where: randWhere(), Set: setOp{Op: "none"}}
		case x < 65:
			s := setOp{Op: "v_add", Val: 1}
			if rng.Intn(10) < 4 {
				s = setOp{Op: "v_set", Val: rng.Intn(4)}
			}
			return op{Kind: "update", Where: randWhere(), Set: s}
		case x < 75:
			return op{Kind: "delete", Where: randWhere(), Set: setOp{Op: "none"}}
		default:
			return op{Kind: "insert", Where: where{Op: "all"}, Set: setOp{Op: "none"},
				Key: 1 + rng.Intn(3), Row: rowVals{V: rng.Intn(3), U: 1 + rng.Intn(4)}}
		}
	}

	n := 2 + rng.Intn(2)
	txns := make([][]op, n)
	for i := range txns {
		if rng.Intn(10) < 2 {
			txns[i] = []op{randStmt()} // autocommit statement
			continue
		}
		ops := []op{{Kind: "begin", Iso: pickIso(), Where: where{Op: "all"}, Set: setOp{Op: "none"}}}
		for j := 0; j < 1+rng.Intn(3); j++ {
			ops = append(ops, randStmt())
		}
		end := "commit"
		if rng.Intn(10) < 2 {
			end = "rollback"
		}
		ops = append(ops, op{Kind: end, Where: where{Op: "all"}, Set: setOp{Op: "none"}})
		txns[i] = ops
	}
	return program{name: name, init: init, txns: txns}
}

// Hermitage-style anomaly schedules on rows (1,10) and (2,20).
func hermitage(iso string) []program {
	init := []row{{K: 1, V: 10, U: 1}, {K: 2, V: 20, U: 2}}
	none := setOp{Op: "none"}
	all := where{Op: "all"}
	begin := op{Kind: "begin", Iso: iso, Where: all, Set: none}
	commit := op{Kind: "commit", Where: all, Set: none}
	rollback := op{Kind: "rollback", Where: all, Set: none}
	sel := func(w where) op { return op{Kind: "select", Where: w, Set: none} }
	upd := func(k, v int) op { return op{Kind: "update", Where: where{Op: "k_eq", Val: k}, Set: setOp{Op: "v_set", Val: v}} }
	k := func(n int) where { return where{Op: "k_eq", Val: n} }

	p := func(name string, txns [][]op, sched ...int) program {
		return program{name: name + "-" + iso, init: init, txns: txns, schedule: sched}
	}
	return []program{
		p("G0", [][]op{
			{begin, upd(1, 11), upd(2, 21), commit},
			{begin, upd(1, 12), upd(2, 22), commit},
			{sel(all)},
		}, 0, 1, 0, 1, 0, 0, 1, 1, 2),
		p("G1a", [][]op{
			{begin, upd(1, 101), rollback},
			{begin, sel(all), sel(all), commit},
		}, 0, 1, 0, 1, 0, 1, 1),
		p("G1b", [][]op{
			{begin, upd(1, 101), upd(1, 11), commit},
			{begin, sel(all), sel(all), commit},
		}, 0, 1, 0, 1, 0, 0, 1, 1),
		p("G1c", [][]op{
			{begin, upd(1, 11), sel(k(2)), commit},
			{begin, upd(2, 22), sel(k(1)), commit},
		}, 0, 1, 0, 1, 0, 1, 0, 1),
		p("OTV", [][]op{
			{begin, upd(1, 11), upd(2, 19), commit},
			{begin, upd(1, 12), upd(2, 18), commit},
			{begin, sel(k(1)), sel(k(2)), sel(k(2)), sel(k(1)), commit},
		}, 0, 1, 2, 0, 0, 1, 0, 2, 1, 2, 1, 2, 2, 2),
		p("PMPread", [][]op{
			{begin, sel(where{Op: "v_eq", Val: 30}), sel(where{Op: "v_ge", Val: 30}), commit},
			{begin, {Kind: "insert", Where: all, Set: none, Key: 3, Row: rowVals{V: 30, U: 3}}, commit},
		}, 0, 1, 0, 1, 1, 0, 0),
		p("PMPwrite", [][]op{
			{begin, {Kind: "update", Where: all, Set: setOp{Op: "v_add", Val: 10}}, commit},
			{begin, {Kind: "delete", Where: where{Op: "v_eq", Val: 20}, Set: none}, sel(all), commit},
		}, 0, 1, 0, 1, 0, 1, 1),
		p("P4", [][]op{
			{begin, sel(k(1)), upd(1, 11), commit},
			{begin, sel(k(1)), upd(1, 11), commit},
		}, 0, 1, 0, 1, 0, 1, 0, 1),
		p("Gsingle", [][]op{
			{begin, sel(k(1)), sel(k(2)), commit},
			{begin, sel(k(1)), sel(k(2)), upd(1, 12), upd(2, 18), commit},
		}, 0, 1, 0, 1, 1, 1, 1, 1, 0, 0),
		p("G2item", [][]op{
			{begin, sel(all), upd(1, 11), commit},
			{begin, sel(all), upd(2, 21), commit},
		}, 0, 1, 0, 1, 0, 1, 0, 1),
	}
}

// ---- main ----------------------------------------------------------------

func main() {
	dsn := flag.String("db", "postgres://wk:wk@localhost:55432/wk", "PostgreSQL URL")
	cases := flag.String("cases", "random", "random | hermitage")
	iso := flag.String("iso", "RC", "RC | RR | SER | mixed")
	n := flag.Int("n", 100, "number of random programs")
	seed := flag.Int64("seed", 1, "random seed")
	outDir := flag.String("out", "traces", "output directory")
	flag.Parse()

	ctx := context.Background()
	monitor, err := pgx.Connect(ctx, *dsn)
	if err != nil {
		log.Fatal(err)
	}
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		log.Fatal(err)
	}
	r := &runner{dsn: *dsn, monitor: monitor, rng: rand.New(rand.NewSource(*seed))}

	var programs []program
	if *cases == "hermitage" {
		isos := []string{*iso}
		if *iso == "mixed" {
			isos = []string{"RC", "RR", "SER"}
		}
		for _, l := range isos {
			programs = append(programs, hermitage(l)...)
		}
	} else {
		for i := 0; i < *n; i++ {
			programs = append(programs, randomProgram(r.rng, *iso, fmt.Sprintf("rand-%s-s%d-%04d", *iso, *seed, i)))
		}
	}

	written, discarded := 0, 0
	for _, p := range programs {
		trace, err := r.run(ctx, p)
		if err != nil {
			if errors.Is(err, errStuck) {
				discarded++
				log.Printf("%s: discarded (%v)", p.name, err)
				continue
			}
			log.Fatalf("%s: %v", p.name, err)
		}
		f, err := os.Create(filepath.Join(*outDir, p.name+".ndjson"))
		if err != nil {
			log.Fatal(err)
		}
		enc := json.NewEncoder(f)
		for _, line := range trace {
			if err := enc.Encode(line); err != nil {
				log.Fatal(err)
			}
		}
		_ = f.Close()
		written++
	}
	fmt.Printf("wrote %d traces to %s (%d discarded)\n", written, *outDir, discarded)
}
