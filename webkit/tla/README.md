# WebDB.tla — PostgreSQL semantics for app models

`WebDB.tla` is a reusable TLA+ module with the transaction semantics of PostgreSQL 17. An app's model calls
one WebDB action per SQL statement its handlers issue, so the app model never re-derives isolation-level
behavior. It is checked against a real PostgreSQL server (see Validation below).

## Using it in an app model

```tla
EXTENDS WebDB                   \* or: DB == INSTANCE WebDB WITH ...

\* A checkout handler step, one action per statement:
Checkout_ReadStock(r) ==        \* src/server.ts:L101
    /\ pc[r] = "read"
    /\ Select(r, "Product", LAMBDA k, row : k = ProductId)
    /\ stock' = [stock EXCEPT ![r] = (CHOOSE p \in res'[r].rows : TRUE)[2].stock]
    /\ pc' = [pc EXCEPT ![r] = "insert"]

Next == (\E r \in Requests : AppSteps(r)) \/ (DBSilent /\ UNCHANGED appVars)
```

- **Sessions.** `Txns` is the set of database sessions. Use the request or job actor ids.
- **Rows.** Each row is a record over the table's columns, keyed by `Keys`. `Absent` means "no row".
  Predicates and update functions are operators over `(key, row)`.
- **Transaction control:** `Begin(t, iso)` with `iso` one of `"RC"`, `"RR"`, `"SER"`, then `Commit(t)` or
  `Rollback(t)`. Under SERIALIZABLE, `SerCommitFail(t)` is an alternative to `Commit(t)`. Statements issued
  outside `Begin` are autocommit, at level `AutoIso`.
- **Statements that never wait:** `Select(t, tb, P)`.
- **Statements that can wait** are split into a start and a finish:
  - `WriteStart(t, kind, tb, P)` then `WriteFinish(t, P, F)`, where `kind` is `"update"`, `"delete"` or
    `"lock"` (`SELECT … FOR UPDATE`);
  - `InsertStart(t, tb, k, row)` then `InsertFinish(t)`.

  Between the two, other sessions run, and the silent `DBSilent` steps acquire locks, detect deadlocks
  (`40P01`), and raise SERIALIZABLE failures. App models must include `DBSilent` in `Next`.
- **Results.** After each action, `res[t]` holds `ok`, `code` (SQLSTATE), `tag` (`COMMIT` or `ROLLBACK`) and
  `rows` (a set of `<<key, row>>`). After an error, the transaction is failed: `FailedStmt(t)` gives `25P02`
  until `Commit` or `Rollback`.
- **Invariants** read committed state with `Committed(tb, k)` and `CommittedKeys(tb)`.
- **`RowDomain`** (`[Tables -> SUBSET row]`) must list every row value the model can produce, for example the
  product of the column domains. It is only used for SERIALIZABLE transactions, so models that never use
  SERIALIZABLE pay nothing for it.

## What is modelled

- **Tuple identity, not just keys.**
  - An UPDATE continues a row's version chain, a DELETE ends it, and an INSERT always starts a new tuple,
    even when the key was used before.
  - So a REPEATABLE READ snapshot can show a tuple another session deleted after the snapshot, next to the
    transaction's own new tuple with the same key.
- **Snapshots.** READ COMMITTED takes one per statement. REPEATABLE READ and SERIALIZABLE take one at the
  transaction's first statement.
- **Row locks** are held to transaction end, with waiting and deadlock detection.
- **READ COMMITTED re-check.** When a row changed while the statement waited for its lock, WebDB follows the
  update chain and re-checks the newest version against the WHERE clause (PostgreSQL's EvalPlanQual).
  - A deleted tuple is skipped, even if its key was re-inserted.
  - Rows that only started matching after the snapshot are never seen.
- **REPEATABLE READ** fails with `40001` as soon as it touches a tuple whose snapshot version was changed by a
  transaction that committed after the snapshot. This happens before waiting for whoever holds the newer
  version's lock.
- **Unique checks go index by index:** the primary key first, then `UniqueCols` in index order.
  - A conflict with the session's own row, or with a committed row nobody is changing, fails with `23505` at
    once.
  - A conflict with a row another session is inserting, updating or deleting waits for that session. Rows
    locked by an in-flight UPDATE or DELETE count as being changed.
  - Unique checks see every committed row, even ones outside the snapshot.
- **Errors abort the transaction immediately.** Locks are released and writes dropped.

- **SERIALIZABLE** is REPEATABLE READ plus PostgreSQL's guarantee: the committed SERIALIZABLE transactions
  are serializable.
  - WebDB records each SERIALIZABLE transaction's predicate reads and writes.
  - A `COMMIT` that would close a dependency cycle among committed SERIALIZABLE transactions cannot succeed.
    Cycles can be made of write–write, write–read and read–write edges.
  - Beyond that, `40001` is allowed at any statement or `COMMIT`. PostgreSQL's SSI also aborts
    conservatively, depending on lock granularity and memory limits, which are not modelled.
  - Transactions at other levels are invisible to SSI, as in PostgreSQL. So a cycle that passes through a
    READ COMMITTED transaction is not prevented.
  - Predicates of SERIALIZABLE reads are evaluated over the constant `RowDomain`, which must contain every row
    value the model can produce.

## Known gaps

- **Not modelled:** FOR SHARE and KEY SHARE locks (so foreign-key lock waits), CHECK constraints,
  `ON CONFLICT`, savepoints, advisory locks, and UPDATEs that change a UNIQUE column.
- **Unique checks are not SERIALIZABLE reads.** The index reads an INSERT does for its unique check are not
  recorded. That can only miss a dependency, which makes the model slightly more permissive, never stricter
  than PostgreSQL.

## Validation

### `difftest/`: WebDB must accept every real PostgreSQL execution

- `cmd/dbdiff` runs random concurrent programs, plus Hermitage-style anomaly schedules, on a real server with
  deterministic interleavings. It records every statement's result and every lock wait.
- `difftest/check.py` asks TLC, through `TraceWebDB.tla`, to reproduce each trace exactly. When it can't, it
  reports the first event WebDB cannot produce.
- Consecutive `done` events completed in the same settle window, for example an error that wakes a waiting
  session, so their logged order is not significant. They may be matched in any order.
- `difftest/coverage.py` reports which behaviors the traces exercised.

```bash
docker run -d --name wk-difftest-pg -e POSTGRES_USER=wk -e POSTGRES_PASSWORD=wk -e POSTGRES_DB=wk \
  -p 55432:5432 postgres:17 -c deadlock_timeout=20ms -c max_connections=50
go run ./cmd/dbdiff -cases random -iso RC -n 500 -seed 42 -out /tmp/traces/rc
python3 tla/difftest/check.py /tmp/traces/rc
python3 tla/difftest/coverage.py /tmp/traces/rc
```

### `tests/`: WebDB must reach the anomalies PostgreSQL allows

`tests/run.py` model-checks small two-actor scenarios (lost update, atomic increment, oversell, write skew)
at each isolation level and compares the result with PostgreSQL's documented behavior. Each run also checks
non-vacuity: a behavior in which both actors commit must be reachable.

| Scenario | READ COMMITTED | REPEATABLE READ | SERIALIZABLE |
|---|---|---|---|
| lost update (read, then write the computed value) | anomaly | prevented | prevented |
| atomic increment (`SET v = v + 1`) | prevented | prevented | prevented |
| oversell (check stock, then decrement) | anomaly | prevented | prevented |
| write skew (both check, each updates a different row) | anomaly | anomaly | prevented |
