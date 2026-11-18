------------------------------- MODULE WebDB -------------------------------
(***************************************************************************)
(* Reusable PostgreSQL transaction semantics for TLA+ models of web apps.  *)
(*                                                                         *)
(* An app model EXTENDS or INSTANCEs this module and calls one action per  *)
(* SQL statement its handler issues. Statements that can wait on locks     *)
(* are split in two (Start / Finish); between them other sessions run and  *)
(* the silent DBSilent steps take locks, detect deadlocks and raise        *)
(* serialization failures.                                                 *)
(*                                                                         *)
(* Modelled (PostgreSQL 17; checked against a real server by               *)
(* webkit/tla/difftest, see README.md):                                    *)
(*   - MVCC with tuple identity: an UPDATE continues a row's version       *)
(*     chain, a DELETE ends it, an INSERT starts a new tuple even if the   *)
(*     key was used before. A REPEATABLE READ snapshot can therefore show  *)
(*     a deleted tuple next to the transaction's own new one.              *)
(*   - Snapshots: READ COMMITTED per statement; REPEATABLE READ and        *)
(*     SERIALIZABLE at the transaction's first statement.                 *)
(*   - Row locks to transaction end; waiting; deadlock detection (40P01).  *)
(*   - READ COMMITTED: after waiting on a row, follow its update chain and *)
(*     re-check the WHERE clause on the newest version (EvalPlanQual); a   *)
(*     deleted tuple is skipped. Rows that only started matching after the *)
(*     statement's snapshot are never seen.                                *)
(*   - REPEATABLE READ: touching a tuple whose snapshot version was        *)
(*     changed by a transaction that committed after the snapshot fails    *)
(*     with 40001 at once, without waiting for current lock holders.       *)
(*   - Unique checks index by index (primary key first, then UniqueCols in *)
(*     order): a definite conflict fails with 23505; a conflict with a row *)
(*     another session is inserting, updating or deleting waits for it.    *)
(*     Rows an in-flight UPDATE/DELETE has locked count as being changed.  *)
(*   - Errors abort the transaction at once (locks released, writes        *)
(*     dropped); later statements fail with 25P02 until COMMIT/ROLLBACK.   *)
(*   - Autocommit statements (outside BEGIN) commit on completion.         *)
(*                                                                         *)
(* SERIALIZABLE is REPEATABLE READ plus PostgreSQL's guarantee that the    *)
(* committed SERIALIZABLE transactions are serializable: each one's         *)
(* predicate reads and writes are recorded, and a COMMIT that would close a *)
(* dependency cycle (ww, wr or rw edges) among committed SERIALIZABLE       *)
(* transactions cannot succeed. Beyond that, a serialization failure        *)
(* (40001) is allowed at any statement or COMMIT, because PostgreSQL's SSI  *)
(* also aborts transactions conservatively (depending on lock granularity   *)
(* and memory limits that are not modelled). Transactions at other levels   *)
(* are invisible to SSI, as in PostgreSQL. Predicates of SERIALIZABLE reads *)
(* are evaluated over RowDomain, which must contain every row value that    *)
(* can occur.                                                               *)
(*                                                                         *)
(* Not modelled: FOR SHARE / KEY SHARE locks (so foreign-key checks),      *)
(* CHECK constraints, ON CONFLICT, savepoints, advisory locks, sequences,  *)
(* UPDATEs that change a UNIQUE column.                                    *)
(*                                                                         *)
(* Predicates and update functions are TLA+ operators over (key, row);     *)
(* rows are records over the table's columns; Absent means "no row".      *)
(***************************************************************************)
EXTENDS Naturals, Integers, Sequences, FiniteSets

CONSTANTS
    Txns,        \* database sessions: one per request, job or webhook actor
    Tables,
    Keys,        \* primary-key domain (shared by all tables)
    UniqueCols,  \* [Tables -> Seq(SUBSET column)]: UNIQUE constraints, checked after the primary key
    InitRows,    \* [Tables -> [Keys -> row or Absent]]: committed state at start
    AutoIso,     \* isolation level of autocommit statements (PostgreSQL default: "RC")
    RowDomain,   \* [Tables -> SUBSET row]: every row value that can occur (for SERIALIZABLE reads)
    Absent, NoTxn, NoWrite   \* model values

VARIABLES
    ver,    \* committed versions: [Tables -> [Keys -> Seq(<<commitNo, row or Absent, startsTuple>>)]]
    clock,  \* number of committed writing transactions; snapshots are commit numbers
    tx,     \* per session: [st, iso, snap, auto]
    wr,     \* per session: [Tables -> [Keys -> NoWrite or [row, fresh, delc]]]
            \*   row:   the session's current row at this key (or Absent)
            \*   fresh: row is a tuple this session inserted
            \*   delc:  this session deleted or updated the committed tuple at this key
    lk,     \* row locks: [Tables -> [Keys -> session or NoTxn]]
    stmt,   \* per session: the in-flight statement, or NoStmt
    res,    \* per session: result of the last completed statement
    sread,  \* per session: SERIALIZABLE predicate reads so far: set of [tb, s, ext]
    serlog  \* committed SERIALIZABLE transactions: Seq([cno, reads, writes])

dbVars == <<ver, clock, tx, wr, lk, stmt, res, sread, serlog>>

IsoLevels == {"RC", "RR", "SER"}
WriteKinds == {"update", "delete", "lock"}

NoTx == [st |-> "none", iso |-> AutoIso, snap |-> -1, auto |-> FALSE]
NoStmt == [kind |-> "none", tb |-> "", snap |-> -1, todo |-> {}, locked |-> {}, own |-> {},
           key |-> 0, row |-> Absent]
Res(ok, code, tag, rows) == [ok |-> ok, code |-> code, tag |-> tag, rows |-> rows]
OkRes(rows) == Res(TRUE, "", "", rows)
ErrRes(code) == Res(FALSE, code, "", {})
TagRes(tag) == Res(TRUE, "", tag, {})
EmptyWr == [tb \in Tables |-> [k \in Keys |-> NoWrite]]

DBInit ==
    /\ ver = [tb \in Tables |-> [k \in Keys |-> << <<0, InitRows[tb][k], TRUE>> >>]]
    /\ clock = 0
    /\ tx = [t \in Txns |-> NoTx]
    /\ wr = [t \in Txns |-> EmptyWr]
    /\ lk = [tb \in Tables |-> [k \in Keys |-> NoTxn]]
    /\ stmt = [t \in Txns |-> NoStmt]
    /\ res = [t \in Txns |-> OkRes({})]
    /\ sread = [t \in Txns |-> {}]
    /\ serlog = <<>>

-----------------------------------------------------------------------------
(* Versions and visibility *)

Latest(tb, k) == ver[tb][k][Len(ver[tb][k])]          \* <<commitNo, row, startsTuple>>
LatestRow(tb, k) == Latest(tb, k)[2]

\* Index of the version visible at snapshot s.
VisIdx(tb, k, s) ==
    LET vs == ver[tb][k]
    IN CHOOSE i \in 1..Len(vs) : vs[i][1] <= s /\ (i = Len(vs) \/ vs[i + 1][1] > s)
VisibleAt(tb, k, s) == ver[tb][k][VisIdx(tb, k, s)][2]

\* The tuple visible at s was neither deleted nor replaced by a new tuple since.
ChainAlive(tb, k, s) ==
    \A j \in (VisIdx(tb, k, s) + 1)..Len(ver[tb][k]) :
        ver[tb][k][j][2] # Absent /\ ~ver[tb][k][j][3]

Own(t, tb, k) == wr[t][tb][k]
OwnLive(t, tb, k) == Own(t, tb, k) # NoWrite /\ Own(t, tb, k).row # Absent
\* The committed tuple at k still belongs to t's view (t did not delete/update it).
SnapAllowed(t, tb, k) == IF Own(t, tb, k) = NoWrite THEN TRUE ELSE ~Own(t, tb, k).delc

\* Tuples t sees at k under snapshot s: <<"own", row>> and/or <<"snap", row>>.
ViewTuples(t, tb, k, s) ==
    (IF OwnLive(t, tb, k) THEN {<<"own", Own(t, tb, k).row>>} ELSE {})
    \cup
    (IF VisibleAt(tb, k, s) # Absent /\ SnapAllowed(t, tb, k)
     THEN {<<"snap", VisibleAt(tb, k, s)>>} ELSE {})

\* The transaction a statement runs in (an implicit one for autocommit).
Eff(t) == IF tx[t].st = "none"
          THEN [st |-> "active", iso |-> AutoIso, snap |-> -1, auto |-> TRUE]
          ELSE tx[t]
WithSnap(r) == IF r.iso # "RC" /\ r.snap = -1 THEN [r EXCEPT !.snap = clock] ELSE r
SnapOf(r) == IF r.iso = "RC" THEN clock ELSE r.snap

CanIssue(t) == stmt[t] = NoStmt /\ Eff(t).st = "active"

-----------------------------------------------------------------------------
(* Transaction-ending helpers (each primes the variables it names) *)

ReleaseLocks(t) == [tb \in Tables |-> [k \in Keys |-> IF lk[tb][k] = t THEN NoTxn ELSE lk[tb][k]]]

\* The committed version a write entry produces, if any.
Produces(e) == e # NoWrite /\ (e.row # Absent \/ e.delc)
NewVersion(e, n) == IF e.row # Absent THEN <<n, e.row, e.fresh>> ELSE <<n, Absent, FALSE>>

ApplyCommit(w) ==
    IF \A tb \in Tables, k \in Keys : ~Produces(w[tb][k])
    THEN UNCHANGED <<ver, clock>>
    ELSE /\ clock' = clock + 1
         /\ ver' = [tb \in Tables |-> [k \in Keys |->
                      IF Produces(w[tb][k]) THEN Append(ver[tb][k], NewVersion(w[tb][k], clock + 1))
                      ELSE ver[tb][k]]]

EndTxn(t) ==
    /\ tx' = [tx EXCEPT ![t] = NoTx]
    /\ wr' = [wr EXCEPT ![t] = EmptyWr]
    /\ lk' = ReleaseLocks(t)
    /\ stmt' = [stmt EXCEPT ![t] = NoStmt]
    /\ sread' = [sread EXCEPT ![t] = {}]

\* An error aborts the transaction at once: writes dropped, locks released.
\* An explicit transaction then stays "failed" until COMMIT or ROLLBACK.
Fail(t, code) ==
    /\ tx' = [tx EXCEPT ![t] = IF Eff(t).auto THEN NoTx ELSE [Eff(t) EXCEPT !.st = "failed"]]
    /\ wr' = [wr EXCEPT ![t] = EmptyWr]
    /\ lk' = ReleaseLocks(t)
    /\ stmt' = [stmt EXCEPT ![t] = NoStmt]
    /\ res' = [res EXCEPT ![t] = ErrRes(code)]
    /\ sread' = [sread EXCEPT ![t] = {}]
    /\ UNCHANGED <<ver, clock, serlog>>

\* Finish a statement with write map w, row locks l and result r.
\* Autocommit statements commit at once (which releases their locks).
Complete(t, w, l, r) ==
    /\ res' = [res EXCEPT ![t] = r]
    /\ IF tx[t].auto
       THEN /\ ApplyCommit(w)
            /\ EndTxn(t)
            /\ UNCHANGED serlog
       ELSE /\ wr' = [wr EXCEPT ![t] = w]
            /\ lk' = l
            /\ stmt' = [stmt EXCEPT ![t] = NoStmt]
            /\ UNCHANGED <<ver, clock, tx, sread, serlog>>

-----------------------------------------------------------------------------
(* SERIALIZABLE dependency tracking *)

\* Record a predicate read by a SERIALIZABLE transaction (r is its transaction record).
AddRead(t, r, tb, s, P(_, _)) ==
    IF r.iso = "SER" /\ ~r.auto
    THEN sread' = [sread EXCEPT ![t] = @ \cup
             {[tb |-> tb, s |-> s, ext |-> {p \in Keys \X RowDomain[tb] : P(p[1], p[2])}]}]
    ELSE UNCHANGED sread

\* The writes t would commit: the committed row it replaces and the new one.
CommitWrites(t) ==
    {[tb |-> p[1], k |-> p[2], old |-> LatestRow(p[1], p[2]), new |-> wr[t][p[1]][p[2]].row] :
        p \in {q \in Tables \X Keys : Produces(wr[t][q[1]][q[2]])}}

CommitRec(t) == [cno |-> clock + 1, reads |-> sread[t], writes |-> CommitWrites(t)]

\* Some read r observes or misses write w on a row that the read's predicate covers.
Covers(r, w) == r.tb = w.tb /\ (<<w.k, w.old>> \in r.ext \/ <<w.k, w.new>> \in r.ext)

\* Dependency a -> b between committed transactions (a's effects precede b's).
Dep(a, b) ==
    \/ \E wa \in a.writes, wb \in b.writes :                    \* ww
           wa.tb = wb.tb /\ wa.k = wb.k /\ a.cno < b.cno
    \/ \E wa \in a.writes, rb \in b.reads :                     \* wr: b saw a's version
           /\ Covers(rb, wa) /\ a.cno <= rb.s
           /\ ver[wa.tb][wa.k][VisIdx(wa.tb, wa.k, rb.s)][1] = a.cno
    \/ \E ra \in a.reads, wb \in b.writes :                     \* rw: a missed b's version
           Covers(ra, wb) /\ b.cno > ra.s

\* Committing t would close a cycle among committed SERIALIZABLE transactions.
SerCycle(t) ==
    LET n       == Len(serlog)
        node(i) == IF i = 0 THEN CommitRec(t) ELSE serlog[i]
        E(i, j) == i # j /\ Dep(node(i), node(j))
        R[m \in 0..(n + 1)] ==
            IF m = 0 THEN {j \in 0..n : E(0, j)}
            ELSE R[m - 1] \cup {j \in 0..n : \E i \in R[m - 1] : E(i, j)}
    IN 0 \in R[n + 1]

-----------------------------------------------------------------------------
(* Transaction control *)

Begin(t, iso) ==
    /\ tx[t].st = "none" /\ stmt[t] = NoStmt /\ iso \in IsoLevels
    /\ tx' = [tx EXCEPT ![t] = [st |-> "active", iso |-> iso, snap |-> -1, auto |-> FALSE]]
    /\ res' = [res EXCEPT ![t] = OkRes({})]
    /\ UNCHANGED <<ver, clock, wr, lk, stmt, sread, serlog>>

\* COMMIT of a failed transaction rolls back (PostgreSQL reports "ROLLBACK").
\* A SERIALIZABLE COMMIT cannot succeed if it would close a dependency cycle.
Commit(t) ==
    /\ stmt[t] = NoStmt /\ tx[t].st \in {"active", "failed"}
    /\ IF tx[t].st = "failed"
       THEN /\ UNCHANGED <<ver, clock, serlog>>
            /\ res' = [res EXCEPT ![t] = TagRes("ROLLBACK")]
       ELSE /\ tx[t].iso = "SER" => ~SerCycle(t)
            /\ ApplyCommit(wr[t])
            /\ serlog' = IF tx[t].iso = "SER" THEN Append(serlog, CommitRec(t)) ELSE serlog
            /\ res' = [res EXCEPT ![t] = TagRes("COMMIT")]
    /\ EndTxn(t)

Rollback(t) ==
    /\ stmt[t] = NoStmt /\ tx[t].st \in {"active", "failed"}
    /\ res' = [res EXCEPT ![t] = TagRes("ROLLBACK")]
    /\ UNCHANGED <<ver, clock, serlog>>
    /\ EndTxn(t)

\* Any statement issued in a failed transaction.
FailedStmt(t) ==
    /\ stmt[t] = NoStmt /\ tx[t].st = "failed"
    /\ res' = [res EXCEPT ![t] = ErrRes("25P02")]
    /\ UNCHANGED <<ver, clock, tx, wr, lk, stmt, sread, serlog>>

-----------------------------------------------------------------------------
(* Statements *)

\* SELECT ... WHERE P (no locking). Never waits.
Select(t, tb, P(_, _)) ==
    /\ CanIssue(t)
    /\ LET r == WithSnap(Eff(t))
           s == SnapOf(r)
           rows == UNION {{<<k, vt[2]>> : vt \in {x \in ViewTuples(t, tb, k, s) : P(k, x[2])}} : k \in Keys}
       IN /\ res' = [res EXCEPT ![t] = OkRes(rows)]
          /\ tx' = [tx EXCEPT ![t] = IF r.auto THEN NoTx ELSE r]
          /\ AddRead(t, r, tb, s, P)
    /\ UNCHANGED <<ver, clock, wr, lk, stmt, serlog>>

\* UPDATE / DELETE / SELECT FOR UPDATE ("lock") ... WHERE P, part 1: take the
\* statement snapshot and its candidates. Own tuples need no lock; committed
\* ("snap") tuples are locked one by one by WriteLock.
WriteStart(t, kind, tb, P(_, _)) ==
    /\ CanIssue(t) /\ kind \in WriteKinds
    /\ LET r == WithSnap(Eff(t))
           s == SnapOf(r)
           ownC  == {k \in Keys : OwnLive(t, tb, k) /\ P(k, Own(t, tb, k).row)}
           snapC == {k \in Keys : VisibleAt(tb, k, s) # Absent /\ SnapAllowed(t, tb, k)
                                   /\ P(k, VisibleAt(tb, k, s))}
       IN /\ stmt' = [stmt EXCEPT ![t] = [NoStmt EXCEPT !.kind = kind, !.tb = tb, !.snap = s,
                                                         !.todo = snapC, !.own = ownC]]
          /\ tx' = [tx EXCEPT ![t] = r]
          /\ AddRead(t, r, tb, s, P)
    /\ UNCHANGED <<ver, clock, wr, lk, res, serlog>>

\* Silent step: process one committed candidate (any order; PostgreSQL's scan
\* order is not observable).
WriteLock(t, k) ==
    /\ stmt[t].kind \in WriteKinds /\ k \in stmt[t].todo
    /\ LET tb      == stmt[t].tb
           s       == stmt[t].snap
           changed == Latest(tb, k)[1] > s
       IN IF changed /\ tx[t].iso # "RC"
          THEN Fail(t, "40001")                                   \* no waiting first
          ELSE IF changed /\ ~ChainAlive(tb, k, s)
          THEN /\ stmt' = [stmt EXCEPT ![t].todo = @ \ {k}]       \* tuple deleted: skipped
               /\ UNCHANGED <<ver, clock, tx, wr, lk, res, sread, serlog>>
          ELSE /\ lk[tb][k] \in {NoTxn, t}                         \* else wait
               /\ lk' = [lk EXCEPT ![tb][k] = t]
               /\ stmt' = [stmt EXCEPT ![t].todo = @ \ {k}, ![t].locked = @ \cup {k}]
               /\ UNCHANGED <<ver, clock, tx, wr, res, sread, serlog>>

\* Part 2, once every candidate is processed. A locked committed tuple is
\* affected if its newest version still satisfies P (READ COMMITTED re-check;
\* under REPEATABLE READ it is the snapshot's). F is the UPDATE's new-row
\* function. Result rows are what RETURNING / SELECT FOR UPDATE returns.
WriteFinish(t, P(_, _), F(_, _)) ==
    /\ stmt[t].kind \in WriteKinds /\ stmt[t].todo = {}
    /\ LET s     == stmt[t]
           tb    == s.tb
           ownT  == {k \in s.own : P(k, Own(t, tb, k).row)}
           snapT == {k \in s.locked : LatestRow(tb, k) # Absent /\ P(k, LatestRow(tb, k))}
           before(k) == IF k \in ownT THEN Own(t, tb, k).row ELSE LatestRow(tb, k)
           after(k)  == CASE s.kind = "update" -> F(k, before(k))
                          [] s.kind = "delete" -> Absent
                          [] OTHER             -> before(k)
           entry(k) == IF k \in ownT THEN [Own(t, tb, k) EXCEPT !.row = after(k)]
                       ELSE [row |-> after(k), fresh |-> FALSE, delc |-> TRUE]
           w == [wr[t] EXCEPT ![tb] = [k \in Keys |->
                    IF k \in ownT \cup snapT /\ s.kind # "lock" THEN entry(k) ELSE wr[t][tb][k]]]
           shown(k) == IF s.kind = "update" THEN after(k) ELSE before(k)
       IN Complete(t, w, lk, OkRes({<<k, shown(k)>> : k \in ownT \cup snapT}))

\* INSERT, part 1.
InsertStart(t, tb, k, row) ==
    /\ CanIssue(t)
    /\ stmt' = [stmt EXCEPT ![t] = [NoStmt EXCEPT !.kind = "insert", !.tb = tb, !.key = k, !.row = row]]
    /\ tx' = [tx EXCEPT ![t] = WithSnap(Eff(t))]
    /\ UNCHANGED <<ver, clock, wr, lk, res, sread, serlog>>

NIdx(tb) == Len(UniqueCols[tb]) + 1
\* Index i: 1 is the primary key, i > 1 is UniqueCols[tb][i - 1].
IdxConflict(tb, i, k1, r1, k2, r2) ==
    IF i = 1 THEN k1 = k2 ELSE \A c \in UniqueCols[tb][i - 1] : r1[c] = r2[c]

\* u is changing the committed tuple at j: wrote it, or an in-flight UPDATE/DELETE locked it.
Modifying(u, tb, j) ==
    \/ wr[u][tb][j] # NoWrite /\ wr[u][tb][j].delc
    \/ stmt[u].kind \in {"update", "delete"} /\ stmt[u].tb = tb /\ j \in stmt[u].locked

\* Definite conflict on index i: with t's own row, or a committed row nobody is changing.
Definite(t, i) ==
    LET s == stmt[t] IN
    \E j \in Keys :
        \/ OwnLive(t, s.tb, j) /\ IdxConflict(s.tb, i, s.key, s.row, j, Own(t, s.tb, j).row)
        \/ /\ LatestRow(s.tb, j) # Absent /\ SnapAllowed(t, s.tb, j)
           /\ IdxConflict(s.tb, i, s.key, s.row, j, LatestRow(s.tb, j))
           /\ \A u \in Txns \ {t} : ~Modifying(u, s.tb, j)

\* Sessions whose in-progress change makes a conflict on index i uncertain.
UncertainBy(t, i) ==
    LET s == stmt[t] IN
    {u \in Txns \ {t} : \E j \in Keys :
        \/ /\ wr[u][s.tb][j] # NoWrite /\ wr[u][s.tb][j].row # Absent
           /\ IdxConflict(s.tb, i, s.key, s.row, j, wr[u][s.tb][j].row)
        \/ /\ LatestRow(s.tb, j) # Absent /\ SnapAllowed(t, s.tb, j) /\ Modifying(u, s.tb, j)
           /\ IdxConflict(s.tb, i, s.key, s.row, j, LatestRow(s.tb, j))}

\* The first index with any conflict, or 0.
FirstIdx(t) ==
    LET hit == {i \in 1..NIdx(stmt[t].tb) : Definite(t, i) \/ UncertainBy(t, i) # {}}
    IN IF hit = {} THEN 0 ELSE CHOOSE i \in hit : \A j \in hit : i <= j

InsertWaitsOn(t) == IF FirstIdx(t) = 0 THEN {} ELSE UncertainBy(t, FirstIdx(t))

\* INSERT, part 2. Unique checks see every committed row, even ones outside the
\* snapshot. If the first conflicting index has both kinds of conflict, either
\* outcome is allowed (index scan order is not observable).
InsertFinish(t) ==
    /\ stmt[t].kind = "insert"
    /\ LET s == stmt[t]
           i == FirstIdx(t)
           e == Own(t, s.tb, s.key)
           new == [row |-> s.row, fresh |-> TRUE, delc |-> e # NoWrite /\ e.delc]
       IN \/ /\ i = 0
             /\ Complete(t, [wr[t] EXCEPT ![s.tb][s.key] = new],
                         [lk EXCEPT ![s.tb][s.key] = t], OkRes({<<s.key, s.row>>}))
          \/ i # 0 /\ Definite(t, i) /\ Fail(t, "23505")

-----------------------------------------------------------------------------
(* Waiting, deadlocks and serialization failures *)

WaitsOn(t) ==
    CASE stmt[t].kind \in WriteKinds ->
             {lk[stmt[t].tb][k] : k \in stmt[t].todo} \ {NoTxn, t}
      [] stmt[t].kind = "insert" -> InsertWaitsOn(t)
      [] OTHER -> {}

\* Blocked: has a statement in flight that can make no progress.
Blocked(t) ==
    CASE stmt[t].kind \in WriteKinds ->
             /\ stmt[t].todo # {}
             /\ \A k \in stmt[t].todo :
                    /\ lk[stmt[t].tb][k] \notin {NoTxn, t}
                    /\ ~(Latest(stmt[t].tb, k)[1] > stmt[t].snap
                         /\ (tx[t].iso # "RC" \/ ~ChainAlive(stmt[t].tb, k, stmt[t].snap)))
      [] stmt[t].kind = "insert" -> FirstIdx(t) # 0 /\ ~Definite(t, FirstIdx(t))
      [] OTHER -> FALSE

BlockedOn(t) == IF Blocked(t) THEN WaitsOn(t) ELSE {}

WaitReach(t) ==
    LET R[n \in 0..Cardinality(Txns)] ==
            IF n = 0 THEN BlockedOn(t) ELSE R[n - 1] \cup UNION {BlockedOn(u) : u \in R[n - 1]}
    IN R[Cardinality(Txns)]

\* PostgreSQL aborts one member of a wait cycle (whichever detects it first).
DeadlockAbort(t) ==
    /\ Blocked(t) /\ t \in WaitReach(t)
    /\ Fail(t, "40P01")

\* SERIALIZABLE: a statement in flight may fail with a serialization failure.
SerAbort(t) ==
    /\ tx[t].st = "active" /\ tx[t].iso = "SER" /\ stmt[t] # NoStmt
    /\ Fail(t, "40001")

\* SERIALIZABLE: a SELECT may fail with a serialization failure instead of returning.
SerSelectFail(t) ==
    /\ CanIssue(t) /\ Eff(t).iso = "SER"
    /\ Fail(t, "40001")

\* SERIALIZABLE: COMMIT may fail with a serialization failure; the transaction ends.
SerCommitFail(t) ==
    /\ stmt[t] = NoStmt /\ tx[t].st = "active" /\ tx[t].iso = "SER"
    /\ res' = [res EXCEPT ![t] = ErrRes("40001")]
    /\ UNCHANGED <<ver, clock, serlog>>
    /\ EndTxn(t)

\* Silent steps an app model must allow alongside its own actions.
DBSilent ==
    \E t \in Txns :
        \/ \E k \in Keys : WriteLock(t, k)
        \/ DeadlockAbort(t)
        \/ SerAbort(t)

-----------------------------------------------------------------------------
(* Queries for app invariants *)

\* Committed state as of now.
Committed(tb, k) == LatestRow(tb, k)
CommittedKeys(tb) == {k \in Keys : Committed(tb, k) # Absent}

=============================================================================
