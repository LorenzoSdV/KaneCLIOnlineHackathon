----------------------------- MODULE TraceShop -----------------------------
(***************************************************************************)
(* Trace validation of the Shop model against recordings of the real app. *)
(*                                                                         *)
(* Each trace event is matched to the step its request is at (StepName),   *)
(* and the step's outcome is checked against what really happened:         *)
(*   - SELECT / RETURNING rows (modelled columns), affected-row counts and  *)
(*     SQLSTATE errors of every statement;                                  *)
(*   - the rows each COMMIT committed, from logical decoding;               *)
(*   - each request's HTTP status (and order id for checkouts).             *)
(* Ordering between requests comes from WebTrace (overlapping events may   *)
(* match in either order); lock waits and deadlocks are WebDB silent steps. *)
(***************************************************************************)
EXTENDS Shop, WebTrace

\* Constants of WebDB taken from the trace header (substituted in the .cfg) ------

TraceActors == {Header.actors[i] : i \in 1..Len(Header.actors)}
TraceKeys == 1..Header.maxkey
Strip(rec) == [c \in DOMAIN rec \ {"k"} |-> rec[c]]
TraceInitRows ==
    [tb \in ShopTables |-> [k \in TraceKeys |->
        LET rows == Header.tables[tb] IN
        IF \E i \in 1..Len(rows) : rows[i].k = k
        THEN Strip(rows[CHOOSE i \in 1..Len(rows) : rows[i].k = k])
        ELSE Absent]]

\* Event decoding ---------------------------------------------------------------

Arg(e, f, d) == IF f \in DOMAIN e.args THEN e.args[f] ELSE d

RouteOf(action) ==
    CASE action = "Checkout_Begin"   -> "Checkout"
      [] action = "GetProduct_Begin" -> "GetProduct"
      [] action = "ListOrders_Begin" -> "ListOrders"
      [] action = "Cancel_Begin"     -> "Cancel"
      [] action = "Health_Begin"     -> "Health"

InputOf(e) ==
    [route |-> RouteOf(e.action), token |-> Arg(e, "authorization", ""),
     productId |-> Arg(e, "productId", 0), quantity |-> Arg(e, "quantity", 0),
     couponCode |-> Arg(e, "couponCode", ""), idem |-> Arg(e, "idempotency_key", ""),
     orderId |-> Arg(e, "orderId", 0)]

ModelRows(S) == {[c \in {"k"} \cup DOMAIN p[2] |-> IF c = "k" THEN p[1] ELSE p[2][c]] : p \in S}
ObsRows(e) == {e.rows[i] : i \in 1..Len(e.rows)}

\* Post-state checks -------------------------------------------------------------

\* A statement's result (res' after the step) equals the recorded one.
ResultOK(r, e) ==
    IF e.err # ""
    THEN ~res'[r].ok /\ res'[r].code = e.err
    ELSE /\ res'[r].ok
         /\ IF e.limit1
            THEN /\ ObsRows(e) \subseteq ModelRows(res'[r].rows)
                 /\ Cardinality(ObsRows(e)) = IF res'[r].rows = {} THEN 0 ELSE 1
            ELSE e.has_rows => ObsRows(e) = ModelRows(res'[r].rows)
         /\ e.kind = "write" => e.count = Cardinality(res'[r].rows)

CommittedP(tb, k) == ver'[tb][k][Len(ver'[tb][k])][2]

\* COMMIT: exactly the rows logical decoding recorded, with the same values.
CommitOK(r, e) ==
    /\ res'[r].ok
    /\ {<<w.table, w.k>> : w \in {e.writes[i] : i \in 1..Len(e.writes)}}
         = {p \in ShopTables \X TraceKeys : Produces(wr[r][p[1]][p[2]])}
    /\ \A i \in 1..Len(e.writes) :
           LET w == e.writes[i] IN
           CommittedP(w.table, w.k) = IF w.present THEN Strip(w.row) ELSE Absent

StatementOK(r, e) ==
    CASE e.kind = "commit"            -> CommitOK(r, e)
      [] e.kind \in {"begin", "rollback"} -> res'[r].ok
      [] e.phase = "start"            -> TRUE
      [] OTHER                        -> ResultOK(r, e)

ResponseOK(r, e) ==
    /\ rs[r].status = e.status
    /\ ("id" \in DOMAIN e.resp /\ e.status \in {200, 201}) => rs[r].id = e.resp.id

\* Matching -----------------------------------------------------------------------

MatchEvent(j) ==
    LET e == Ev(j)
        r == e.actor
    IN \/ /\ e.kind = "request"
          /\ Arrive(r, InputOf(e))
       \/ /\ e.kind = "response"
          /\ pc[r] = "respond"
          /\ ResponseOK(r, e)
          /\ Respond(r)
       \/ /\ e.kind \notin {"request", "response"}
          /\ StepName(r) = e.action
          /\ Step(r)
          /\ StatementOK(r, e)

Init == ShopInit([tb \in ShopTables |-> Header.seq[tb]]) /\ TraceInit

TraceNext ==
    \/ \E j \in Ready : MatchEvent(j) /\ Consume(j)
    \/ DBSilent /\ UNCHANGED <<appVars, done>>

TraceSpec == Init /\ [][TraceNext]_<<vars, done>>

=============================================================================
