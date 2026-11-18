----------------------------- MODULE TraceWebDB -----------------------------
(***************************************************************************)
(* Replays one execution recorded on real PostgreSQL by cmd/dbdiff and     *)
(* checks that WebDB can produce it exactly: every statement's result      *)
(* (rows, error code, COMMIT/ROLLBACK tag) and every wait.                 *)
(*                                                                         *)
(* Table: t(k int PRIMARY KEY, v int NOT NULL, u int UNIQUE).              *)
(* Trace: line 1 is the initial rows; then "start" and "done" events per   *)
(* statement, in the order the harness observed them. Consecutive "done"   *)
(* events completed within the same settle window (e.g. an error that      *)
(* wakes a waiter), so their relative order is not significant: they may   *)
(* be matched in any order.                                                *)
(*                                                                         *)
(* Matched iff TLC reports a violation of the invariant TraceNotDone,      *)
(* which means some behavior consumed every event.                         *)
(***************************************************************************)
EXTENDS WebDB, Json, TLC

CONSTANTS TracePath, Cut   \* Cut: events to match (normally FullLen; smaller to locate a mismatch)

TraceLog == ndJsonDeserialize(TracePath)
Header == TraceLog[1]

\* Definitions substituted for WebDB constants in the .cfg file.
TraceInitRows ==
    [tb \in {"t"} |-> [k \in Keys |->
        IF \E i \in 1..Len(Header.rows) : Header.rows[i].k = k
        THEN LET r == Header.rows[CHOOSE i \in 1..Len(Header.rows) : Header.rows[i].k = k]
             IN [v |-> r.v, u |-> r.u]
        ELSE Absent]]
TraceUniqueCols == [tb \in {"t"} |-> << {"u"} >>]

\* Every row value in the trace (initial, inserted, or returned by a statement).
\* Every value a committed row can take is returned by the statement that wrote it.
TraceRowRecs ==
    (UNION {{TraceLog[i].rows[j] : j \in 1..Len(TraceLog[i].rows)} :
                i \in {i \in 1..Len(TraceLog) : TraceLog[i].ev \in {"init", "done"}}})
    \cup {TraceLog[i].row : i \in {i \in 1..Len(TraceLog) : TraceLog[i].ev = "start" /\ TraceLog[i].kind = "insert"}}
TraceRowDomain ==
    [tb \in {"t"} |-> {[v |-> a, u |-> b] : a \in {r.v : r \in TraceRowRecs}, b \in {r.u : r \in TraceRowRecs}}]

\* The statement DSL the harness generates.
WhereOK(w, k, row) ==
    CASE w.op = "all"  -> TRUE
      [] w.op = "k_eq" -> k = w.val
      [] w.op = "v_ge" -> row.v >= w.val
      [] w.op = "v_eq" -> row.v = w.val
SetRow(s, k, row) ==
    CASE s.op = "v_add" -> [row EXCEPT !.v = @ + s.val]
      [] s.op = "v_set" -> [row EXCEPT !.v = s.val]
      [] OTHER          -> row

VARIABLES l, pend, got  \* cursor; in-flight DSL statement per session; done events matched in the current run
vars == <<dbVars, l, pend, got>>

E == TraceLog[l]

RowsOut(S) == {[k |-> p[1], v |-> p[2].v, u |-> p[2].u] : p \in S}
AsSet(seq) == {seq[i] : i \in 1..Len(seq)}

\* Post-state check: the model's result equals the observed one.
MatchesEv(r, ev) ==
    /\ r.ok = ev.ok
    /\ r.code = ev.code
    /\ r.tag = ev.tag
    /\ RowsOut(r.rows) = AsSet(ev.rows)

\* The run of consecutive "done" events starting at line i.
RunEnd(i) == CHOOSE j \in i..Len(TraceLog) :
                 /\ \A m \in i..j : TraceLog[m].ev = "done"
                 /\ (j = Len(TraceLog) \/ TraceLog[j + 1].ev # "done")

Init ==
    /\ DBInit
    /\ l = 2
    /\ pend = [t \in Txns |-> Header]   \* placeholder record
    /\ got = {}

Advance == l' = l + 1

\* "start" events: issue the statement.
Start ==
    /\ E.ev = "start"
    /\ LET t == E.txn
           P(k, row) == WhereOK(E.where, k, row)
       IN IF E.kind = "begin" THEN Begin(t, E.iso) /\ UNCHANGED pend
          ELSE IF E.kind = "commit" THEN (Commit(t) \/ SerCommitFail(t)) /\ UNCHANGED pend
          ELSE IF E.kind = "rollback" THEN Rollback(t) /\ UNCHANGED pend
          ELSE IF tx[t].st = "failed" THEN FailedStmt(t) /\ UNCHANGED pend
          ELSE IF E.kind = "select" THEN (Select(t, "t", P) \/ SerSelectFail(t)) /\ UNCHANGED pend
          ELSE IF E.kind = "insert"
               THEN InsertStart(t, "t", E.key, [v |-> E.row.v, u |-> E.row.u]) /\ UNCHANGED pend
          ELSE /\ E.kind \in WriteKinds
               /\ WriteStart(t, E.kind, "t", P)
               /\ pend' = [pend EXCEPT ![t] = E]
    /\ Advance
    /\ UNCHANGED got

\* "done" events: the statement completed with the observed result. Any event
\* of the current run of consecutive "done" events may be matched next.
DoneEv(ev) ==
    LET t == ev.txn IN
    \/ \* completed earlier (immediate statements, or aborted while waiting)
       /\ stmt[t] = NoStmt
       /\ MatchesEv(res[t], ev)
       /\ UNCHANGED <<dbVars, pend>>
    \/ /\ stmt[t].kind \in WriteKinds
       /\ LET P(k, row) == WhereOK(pend[t].where, k, row)
              F(k, row) == SetRow(pend[t].set, k, row)
          IN WriteFinish(t, P, F)
       /\ MatchesEv(res'[t], ev)
       /\ UNCHANGED pend
    \/ /\ stmt[t].kind = "insert"
       /\ InsertFinish(t)
       /\ MatchesEv(res'[t], ev)
       /\ UNCHANGED pend

Done ==
    /\ E.ev = "done"
    /\ \E j \in (l..RunEnd(l)) \ got :
          /\ DoneEv(TraceLog[j])
          /\ IF got \cup {j} = l..RunEnd(l)
             THEN l' = RunEnd(l) + 1 /\ got' = {}
             ELSE l' = l /\ got' = got \cup {j}

Silent == DBSilent /\ UNCHANGED <<l, pend, got>>

Next == Start \/ Done \/ Silent

TraceSpec == Init /\ [][Next]_vars

FullLen == Len(TraceLog)

\* Violated exactly when trace events 2..Cut have all been reproduced.
TraceNotDone == l <= Cut

=============================================================================
