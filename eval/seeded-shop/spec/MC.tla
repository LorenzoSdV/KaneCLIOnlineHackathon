--------------------------------- MODULE MC ---------------------------------
(***************************************************************************)
(* Model checking wrapper for the Shop model (Specula's MC.tla role).      *)
(*                                                                         *)
(* A few concurrent requests, each choosing one input from Inputs, start   *)
(* from the seeded database. Concurrency between requests is normal        *)
(* operation, not a fault, so it is on in every configuration. The hunt    *)
(* configs narrow Inputs to one scenario each (scenario projection by      *)
(* action bounds).                                                         *)
(***************************************************************************)
EXTENDS ShopInvariants

CONSTANT Inputs   \* set of request inputs an actor may send

\* Seed data (prisma/seed.ts): three users, the last-item print and t-shirts, one coupon.
MCKeys == 1..4
MCInitRows ==
    [tb \in ShopTables |-> [k \in MCKeys |->
        CASE tb = "User" /\ k \in 1..3 ->
                 [apiToken |-> CASE k = 1 -> "tok-alice" [] k = 2 -> "tok-bob" [] OTHER -> "tok-carol"]
          [] tb = "Product" /\ k = 1 -> [priceCents |-> 5000, stock |-> 1]
          [] tb = "Product" /\ k = 2 -> [priceCents |-> 2000, stock |-> 2]
          [] tb = "Coupon" /\ k = 1  -> [code |-> "WELCOME10", percentOff |-> 10]
          [] OTHER                   -> Absent]]
MCSeq == [tb \in ShopTables |-> CASE tb = "User" -> 4 [] tb = "Product" -> 3 [] tb = "Coupon" -> 2 [] OTHER -> 1]

In(route, token, product, qty, coupon, idem, order) ==
    [route |-> route, token |-> token, productId |-> product, quantity |-> qty,
     couponCode |-> coupon, idem |-> idem, orderId |-> order]

\* Input sets (substituted for Inputs in the cfg files).
LastItemInputs == {In("Checkout", "tok-alice", 1, 1, "", "", 0), In("Checkout", "tok-carol", 1, 1, "", "", 0)}
RetryInputs    == {In("Checkout", "tok-bob", 2, 1, "", "k1", 0)}
CouponInputs   == {In("Checkout", "tok-bob", 2, 1, "WELCOME10", "", 0)}
CancelInputs   == {In("Checkout", "tok-bob", 2, 1, "", "", 0), In("Cancel", "tok-bob", 0, 0, "", "", 1)}
RestockInputs  == {In("Restock", "tok-alice", 2, 1, "", "", 0), In("Restock", "tok-carol", 2, 2, "", "", 0)}
AllInputs      == LastItemInputs \cup RetryInputs \cup CouponInputs \cup CancelInputs
                  \cup {In("GetProduct", "", 1, 0, "", "", 0)}

MCInit == ShopInit(MCSeq)

MCNext ==
    \/ \E r \in Txns : \E input \in Inputs : Arrive(r, input)
    \/ \E r \in Txns : Step(r) \/ Respond(r)
    \/ DBSilent /\ UNCHANGED appVars

MCSpec == MCInit /\ [][MCNext]_vars

\* Every committed order's stock was available: orders of a product never exceed its initial stock
\* plus cancellations (cross-entity consistency).
StockConserved ==
    \A p \in {1, 2} :
        Committed("Product", p).stock
          = MCInitRows["Product"][p].stock
            - LET live == {k \in CommittedKeys("Order") : Committed("Order", k).productId = p
                                                          /\ Committed("Order", k).status # "CANCELLED"}
                  qty[S \in SUBSET live] == IF S = {} THEN 0
                                            ELSE LET k == CHOOSE k \in S : TRUE
                                                 IN Committed("Order", k).quantity + qty[S \ {k}]
              IN qty[live]

\* README: "concurrent restocks all count". When every request is done, product 2's stock is its
\* initial stock plus the quantities of the restocks that succeeded (restock hunts only).
RestocksAllCount ==
    (\A r \in Txns : pc[r] = "done") =>
        LET ok == {r \in Txns : rq[r].route = "Restock" /\ rs[r].status = 200 /\ rq[r].productId = 2}
            sum[S \in SUBSET ok] == IF S = {} THEN 0
                                    ELSE LET r == CHOOSE r \in S : TRUE IN rq[r].quantity + sum[S \ {r}]
        IN Committed("Product", 2).stock = MCInitRows["Product"][2].stock + sum[ok]

=============================================================================
