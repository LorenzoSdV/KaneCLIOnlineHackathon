----------------------------- MODULE Anomalies -----------------------------
(***************************************************************************)
(* Known concurrency anomalies, each as a tiny two-actor app model on      *)
(* WebDB, checked at each isolation level. run.py compares TLC's verdict    *)
(* with what PostgreSQL is documented to allow, so that WebDB is shown to  *)
(* reach real anomalies (not just accept real traces).                    *)
(*                                                                         *)
(* Each actor: BEGIN; SELECT (read); decide; UPDATE; COMMIT (or ROLLBACK   *)
(* after an error).                                                        *)
(*   lost_update       read v, write v+1 computed in the app              *)
(*   atomic_increment  UPDATE ... SET v = v + 1                           *)
(*   oversell          check stock >= 1, then UPDATE stock = stock - 1    *)
(*   write_skew        check both on call, then take yourself off call    *)
(***************************************************************************)
EXTENDS WebDB, TLC

CONSTANTS Scenario, Iso

MyRow(t) == IF t = "A" THEN 1 ELSE 2

ScenarioInitRows ==
    [tb \in {"t"} |-> [k \in Keys |->
        CASE Scenario = "write_skew" /\ k \in {1, 2} -> [v |-> 1, u |-> k]
          [] Scenario = "oversell" /\ k = 1           -> [v |-> 1, u |-> k]
          [] k = 1                                    -> [v |-> 0, u |-> k]
          [] OTHER                                    -> Absent]]
ScenarioUniqueCols == [tb \in {"t"} |-> << {"u"} >>]

VARIABLES pc, local, ok
vars == <<dbVars, pc, local, ok>>

ReadPred(k, row) == IF Scenario = "write_skew" THEN row.v = 1 ELSE k = 1
ReadVal(rows) ==
    CASE Scenario = "write_skew" -> Cardinality(rows)
      [] rows = {}               -> 0
      [] OTHER                   -> (CHOOSE p \in rows : TRUE)[2].v
Proceed(x) ==
    CASE Scenario = "oversell"   -> x >= 1
      [] Scenario = "write_skew" -> x >= 2
      [] OTHER                   -> TRUE
WritePred(t, k, row) == IF Scenario = "write_skew" THEN k = MyRow(t) ELSE k = 1
NewRow(t, k, row) ==
    CASE Scenario = "lost_update"      -> [row EXCEPT !.v = local[t] + 1]
      [] Scenario = "atomic_increment" -> [row EXCEPT !.v = @ + 1]
      [] Scenario = "oversell"         -> [row EXCEPT !.v = @ - 1]
      [] Scenario = "write_skew"       -> [row EXCEPT !.v = 0]

Init == DBInit /\ pc = [t \in Txns |-> "begin"] /\ local = [t \in Txns |-> 0]
        /\ ok = [t \in Txns |-> FALSE]

Step(t) ==
    \/ pc[t] = "begin" /\ Begin(t, Iso) /\ pc' = [pc EXCEPT ![t] = "read"] /\ UNCHANGED <<local, ok>>
    \/ /\ pc[t] = "read"
       /\ Select(t, "t", ReadPred)
       /\ local' = [local EXCEPT ![t] = ReadVal(res'[t].rows)]
       /\ pc' = [pc EXCEPT ![t] = "decide"] /\ UNCHANGED ok
    \/ /\ pc[t] = "decide"
       /\ pc' = [pc EXCEPT ![t] = IF Proceed(local[t]) THEN "wstart" ELSE "commit"]
       /\ UNCHANGED <<dbVars, local, ok>>
    \/ /\ pc[t] = "wstart"
       /\ WriteStart(t, "update", "t", LAMBDA k, row : WritePred(t, k, row))
       /\ pc' = [pc EXCEPT ![t] = "wfinish"] /\ UNCHANGED <<local, ok>>
    \/ /\ pc[t] = "wfinish"
       /\ WriteFinish(t, LAMBDA k, row : WritePred(t, k, row), LAMBDA k, row : NewRow(t, k, row))
       /\ pc' = [pc EXCEPT ![t] = "commit"] /\ UNCHANGED <<local, ok>>
    \/ /\ pc[t] = "wfinish" /\ stmt[t] = NoStmt /\ ~res[t].ok   \* the UPDATE failed while waiting
       /\ pc' = [pc EXCEPT ![t] = "rollback"] /\ UNCHANGED <<dbVars, local, ok>>
    \/ /\ pc[t] = "commit"
       /\ (Commit(t) \/ SerCommitFail(t))
       /\ ok' = [ok EXCEPT ![t] = res'[t].tag = "COMMIT"]
       /\ pc' = [pc EXCEPT ![t] = "done"] /\ UNCHANGED local
    \/ pc[t] = "rollback" /\ Rollback(t) /\ pc' = [pc EXCEPT ![t] = "done"] /\ UNCHANGED <<local, ok>>

Next == (\E t \in Txns : Step(t)) \/ (DBSilent /\ UNCHANGED <<pc, local, ok>>)

Spec == Init /\ [][Next]_vars

AllDone == \A t \in Txns : pc[t] = "done"

\* Non-vacuity: violated iff some behavior has both actors commit.
NotBothCommitted == ~(AllDone /\ \A t \in Txns : ok[t])

Property ==
    CASE Scenario \in {"lost_update", "atomic_increment"} ->
             (AllDone /\ \A t \in Txns : ok[t]) => Committed("t", 1).v = 2
      [] Scenario = "oversell"   -> Committed("t", 1).v >= 0
      [] Scenario = "write_skew" -> AllDone => \E k \in {1, 2} : Committed("t", k).v = 1

=============================================================================
