------------------------------- MODULE Shop -------------------------------
(***************************************************************************)
(* Reference model of apps/seeded-shop (hand-written for M3; in a pipeline *)
(* run the agent writes this, as Specula's base.tla).                     *)
(*                                                                         *)
(* Actors are HTTP requests (WebDB sessions). Each handler is a sequence   *)
(* of steps, one per SQL statement it issues, citing src/server.ts. A      *)
(* transaction does not make its statements atomic: other requests run    *)
(* between steps, with PostgreSQL semantics from WebDB.                    *)
(*                                                                         *)
(* Modelled columns only (see eval/seeded-shop/mapping.toml). Ids come     *)
(* from per-table sequences that, as in PostgreSQL, do not roll back.      *)
(* Foreign-key checks are not modelled: their KEY SHARE locks never block  *)
(* this app's statements (stock updates change no key column).            *)
(***************************************************************************)
EXTENDS WebDB, TLC

ShopTables == {"User", "Product", "Order", "Coupon", "CouponRedemption"}

\* Unique indexes after the primary key, in creation order (prisma/migrations/0001_init).
ShopUniqueCols ==
    [tb \in ShopTables |->
        CASE tb = "User"             -> << {"apiToken"} >>
          [] tb = "Coupon"           -> << {"code"} >>
          [] tb = "CouponRedemption" -> << {"orderId"}, {"couponId", "userId"} >>
          [] OTHER                   -> << >>]

\* The app runs at READ COMMITTED only, so no SERIALIZABLE row domain is needed.
ShopRowDomain == [tb \in ShopTables |-> {}]

Routes == {"Checkout", "GetProduct", "ListOrders", "Cancel", "Restock", "Health"}

NoInput == [route |-> "", token |-> "", productId |-> 0, quantity |-> 0, couponCode |-> "",
            idem |-> "", orderId |-> 0]
NoLocal == [user |-> 0, price |-> 0, total |-> 0, couponId |-> 0, orderId |-> 0,
            oprod |-> 0, oqty |-> 0]
NoResp  == [status |-> 0, id |-> -1]

VARIABLES
    pc,      \* per request: handler step
    rq,      \* per request: inputs (route, token, body and path fields)
    lv,      \* per request: handler locals
    rs,      \* per request: HTTP response
    nextId   \* per table: next value of the id sequence

appVars == <<pc, rq, lv, rs, nextId>>
vars == <<dbVars, appVars>>

ShopInit(seq) ==
    /\ DBInit
    /\ pc = [r \in Txns |-> "idle"]
    /\ rq = [r \in Txns |-> NoInput]
    /\ lv = [r \in Txns |-> NoLocal]
    /\ rs = [r \in Txns |-> NoResp]
    /\ nextId = seq

\* Helpers ------------------------------------------------------------------

Go(r, step) == pc' = [pc EXCEPT ![r] = step]
Reply(r, status, id) == /\ rs' = [rs EXCEPT ![r] = [status |-> status, id |-> id]]
                        /\ Go(r, "respond")
SetLv(r, f) == lv' = [lv EXCEPT ![r] = f]
Rows(r) == res'[r].rows
Failed(r) == stmt[r] = NoStmt /\ ~res[r].ok     \* the in-flight statement failed while waiting

\* JavaScript Math.round(n / 100) for n >= 0.
RoundDiv100(n) == (2 * n + 100) \div 200

\* Request arrival ---------------------------------------------------------

AuthRoutes == {"Checkout", "ListOrders", "Cancel", "Restock"}

Arrive(r, input) ==
    /\ pc[r] = "idle"
    /\ rq' = [rq EXCEPT ![r] = input]
    /\ IF input.route \in AuthRoutes /\ input.token = ""            \* server.ts:L21
       THEN Reply(r, 401, -1)
       ELSE IF input.route = "Health"                               \* server.ts:L71
       THEN Reply(r, 200, -1)
       ELSE /\ Go(r, CASE input.route = "Checkout"   -> "co_auth"
                       [] input.route = "ListOrders" -> "lo_auth"
                       [] input.route = "Cancel"     -> "ca_auth"
                       [] input.route = "Restock"    -> "rs_auth"
                       [] OTHER                      -> "gp_read")
            /\ UNCHANGED rs
    /\ UNCHANGED <<lv, nextId, dbVars>>

Respond(r) ==
    /\ pc[r] = "respond"
    /\ Go(r, "done")
    /\ UNCHANGED <<rq, lv, rs, nextId, dbVars>>

\* authenticate(): server.ts:L22-L23 (shared by three routes)
Auth(r, next) ==
    /\ Select(r, "User", LAMBDA k, row : row.apiToken = rq[r].token)
    /\ IF Rows(r) = {}
       THEN Reply(r, 401, -1) /\ UNCHANGED lv
       ELSE /\ SetLv(r, [lv[r] EXCEPT !.user = (CHOOSE p \in Rows(r) : TRUE)[1]])
            /\ Go(r, next) /\ UNCHANGED rs

\* Error in an explicit transaction: Prisma rolls back, the handler returns status.
ToRollback(r, step, status) ==
    /\ rs' = [rs EXCEPT ![r] = [status |-> status, id |-> -1]]
    /\ Go(r, step)

\* GET /products/:id ------------------------------------------------------- server.ts:L73-L76

GetProduct_Read(r) ==
    /\ pc[r] = "gp_read"
    /\ Select(r, "Product", LAMBDA k, row : k = rq[r].productId)
    /\ IF Rows(r) = {} THEN Reply(r, 404, -1) ELSE Reply(r, 200, rq[r].productId)
    /\ UNCHANGED <<rq, lv, nextId>>

\* GET /orders ----------------------------------------------------------- server.ts:L79-L82

ListOrders_Auth(r) == pc[r] = "lo_auth" /\ Auth(r, "lo_read") /\ UNCHANGED <<rq, nextId>>

ListOrders_Read(r) ==
    /\ pc[r] = "lo_read"
    /\ Select(r, "Order", LAMBDA k, row : row.userId = lv[r].user)
    /\ Reply(r, 200, -1)
    /\ UNCHANGED <<rq, lv, nextId>>

\* POST /checkout -------------------------------------------------------- server.ts:L86-L125

Checkout_Auth(r) ==
    /\ pc[r] = "co_auth"
    /\ Auth(r, IF rq[r].idem # "" THEN "co_idem" ELSE "co_begin")
    /\ UNCHANGED <<rq, nextId>>

\* L92-L93: findFirst outside the transaction (autocommit); LIMIT 1 returns any match.
Checkout_FindByKey(r) ==
    /\ pc[r] = "co_idem"
    /\ Select(r, "Order", LAMBDA k, row : row.userId = lv[r].user /\ row.idempotencyKey = rq[r].idem)
    /\ IF Rows(r) = {}
       THEN Go(r, "co_begin") /\ UNCHANGED rs
       ELSE \E p \in Rows(r) : Reply(r, 200, p[1])
    /\ UNCHANGED <<rq, lv, nextId>>

\* L96: prisma.$transaction (READ COMMITTED, PostgreSQL's default)
Checkout_Begin(r) ==
    /\ pc[r] = "co_begin"
    /\ Begin(r, "RC")
    /\ Go(r, "co_product")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

\* L98-L100: read the product, check stock
Checkout_ReadProduct(r) ==
    /\ pc[r] = "co_product"
    /\ Select(r, "Product", LAMBDA k, row : k = rq[r].productId)
    /\ IF Rows(r) = {}
       THEN ToRollback(r, "co_rollback", 404) /\ UNCHANGED lv
       ELSE LET p == CHOOSE p \in Rows(r) : TRUE IN
            IF p[2].stock < rq[r].quantity
            THEN ToRollback(r, "co_rollback", 409) /\ UNCHANGED lv
            ELSE /\ SetLv(r, [lv[r] EXCEPT !.price = p[2].priceCents,
                                           !.total = p[2].priceCents * rq[r].quantity])
                 /\ Go(r, IF rq[r].couponCode # "" THEN "co_coupon" ELSE "co_ins")
                 /\ UNCHANGED rs
    /\ UNCHANGED <<rq, nextId>>

\* L104-L108: look up the coupon and apply the discount
Checkout_ReadCoupon(r) ==
    /\ pc[r] = "co_coupon"
    /\ Select(r, "Coupon", LAMBDA k, row : row.code = rq[r].couponCode)
    /\ IF Rows(r) = {}
       THEN ToRollback(r, "co_rollback", 404) /\ UNCHANGED lv
       ELSE LET c == CHOOSE p \in Rows(r) : TRUE IN
            /\ SetLv(r, [lv[r] EXCEPT !.couponId = c[1],
                                      !.total = RoundDiv100(@ * (100 - c[2].percentOff))])
            /\ Go(r, "co_ins") /\ UNCHANGED rs
    /\ UNCHANGED <<rq, nextId>>

\* L111-L113: create the order (its id comes from the sequence, which never rolls back)
Checkout_InsertOrder_start(r) ==
    /\ pc[r] = "co_ins"
    /\ InsertStart(r, "Order", nextId["Order"],
                   [userId |-> lv[r].user, productId |-> rq[r].productId, quantity |-> rq[r].quantity,
                    totalCents |-> lv[r].total, status |-> "PENDING", idempotencyKey |-> rq[r].idem])
    /\ nextId' = [nextId EXCEPT !["Order"] = @ + 1]
    /\ Go(r, "co_ins_f")
    /\ UNCHANGED <<rq, lv, rs>>

Checkout_InsertOrder_finish(r) ==
    /\ pc[r] = "co_ins_f"
    /\ \/ /\ InsertFinish(r)
          /\ IF res'[r].ok
             THEN /\ SetLv(r, [lv[r] EXCEPT !.orderId = stmt[r].key])
                  /\ Go(r, IF rq[r].couponCode # "" THEN "co_red" ELSE "co_dec") /\ UNCHANGED rs
             ELSE ToRollback(r, "co_rollback", 500) /\ UNCHANGED lv
       \/ Failed(r) /\ ToRollback(r, "co_rollback", 500) /\ UNCHANGED <<lv, dbVars>>
    /\ UNCHANGED <<rq, nextId>>

\* L114-L116: record the coupon redemption (UNIQUE (couponId, userId))
Checkout_InsertRedemption_start(r) ==
    /\ pc[r] = "co_red"
    /\ InsertStart(r, "CouponRedemption", nextId["CouponRedemption"],
                   [couponId |-> lv[r].couponId, userId |-> lv[r].user, orderId |-> lv[r].orderId])
    /\ nextId' = [nextId EXCEPT !["CouponRedemption"] = @ + 1]
    /\ Go(r, "co_red_f")
    /\ UNCHANGED <<rq, lv, rs>>

\* L122: a unique violation (P2002) becomes 409 after the rollback
Checkout_InsertRedemption_finish(r) ==
    /\ pc[r] = "co_red_f"
    /\ \/ /\ InsertFinish(r)
          /\ IF res'[r].ok
             THEN Go(r, "co_dec") /\ UNCHANGED rs
             ELSE ToRollback(r, "co_rollback", IF res'[r].code = "23505" THEN 409 ELSE 500)
       \/ Failed(r) /\ ToRollback(r, "co_rollback", 500) /\ UNCHANGED dbVars
    /\ UNCHANGED <<rq, lv, nextId>>

\* L117: stock = stock - quantity (atomic decrement; nothing re-checks stock >= 0)
Checkout_DecrementStock_start(r) ==
    /\ pc[r] = "co_dec"
    /\ WriteStart(r, "update", "Product", LAMBDA k, row : k = rq[r].productId)
    /\ Go(r, "co_dec_f")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

Checkout_DecrementStock_finish(r) ==
    /\ pc[r] = "co_dec_f"
    /\ \/ /\ WriteFinish(r, LAMBDA k, row : k = rq[r].productId,
                            LAMBDA k, row : [row EXCEPT !.stock = @ - rq[r].quantity])
          /\ Go(r, "co_commit") /\ UNCHANGED rs
       \/ Failed(r) /\ ToRollback(r, "co_rollback", 500) /\ UNCHANGED dbVars
    /\ UNCHANGED <<rq, lv, nextId>>

Checkout_Commit(r) ==
    /\ pc[r] = "co_commit"
    /\ Commit(r)
    /\ Reply(r, 201, lv[r].orderId)
    /\ UNCHANGED <<rq, lv, nextId>>

Checkout_Rollback(r) ==
    /\ pc[r] = "co_rollback"
    /\ Rollback(r)
    /\ Go(r, "respond")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

\* POST /orders/:id/cancel ------------------------------------------------ server.ts:L127-L141

Cancel_Auth(r) == pc[r] = "ca_auth" /\ Auth(r, "ca_begin") /\ UNCHANGED <<rq, nextId>>

Cancel_Begin(r) ==
    /\ pc[r] = "ca_begin"
    /\ Begin(r, "RC")
    /\ Go(r, "ca_mark")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

CancelP(r, k, row) == k = rq[r].orderId /\ row.userId = lv[r].user /\ row.status = "PENDING"

\* L131-L135: conditional status change; 0 rows means it cannot be cancelled
Cancel_MarkCancelled_start(r) ==
    /\ pc[r] = "ca_mark"
    /\ WriteStart(r, "update", "Order", LAMBDA k, row : CancelP(r, k, row))
    /\ Go(r, "ca_mark_f")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

Cancel_MarkCancelled_finish(r) ==
    /\ pc[r] = "ca_mark_f"
    /\ \/ /\ WriteFinish(r, LAMBDA k, row : CancelP(r, k, row),
                            LAMBDA k, row : [row EXCEPT !.status = "CANCELLED"])
          /\ IF Rows(r) = {} THEN ToRollback(r, "ca_rollback", 409) ELSE Go(r, "ca_read") /\ UNCHANGED rs
       \/ Failed(r) /\ ToRollback(r, "ca_rollback", 500) /\ UNCHANGED dbVars
    /\ UNCHANGED <<rq, lv, nextId>>

\* L136
Cancel_ReadOrder(r) ==
    /\ pc[r] = "ca_read"
    /\ Select(r, "Order", LAMBDA k, row : k = rq[r].orderId)
    /\ LET o == CHOOSE p \in Rows(r) : TRUE IN
       SetLv(r, [lv[r] EXCEPT !.oprod = o[2].productId, !.oqty = o[2].quantity])
    /\ Go(r, "ca_del")
    /\ UNCHANGED <<rq, rs, nextId>>

\* L137
Cancel_DeleteRedemptions_start(r) ==
    /\ pc[r] = "ca_del"
    /\ WriteStart(r, "delete", "CouponRedemption", LAMBDA k, row : row.orderId = rq[r].orderId)
    /\ Go(r, "ca_del_f")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

Cancel_DeleteRedemptions_finish(r) ==
    /\ pc[r] = "ca_del_f"
    /\ \/ /\ WriteFinish(r, LAMBDA k, row : row.orderId = rq[r].orderId, LAMBDA k, row : row)
          /\ Go(r, "ca_inc") /\ UNCHANGED rs
       \/ Failed(r) /\ ToRollback(r, "ca_rollback", 500) /\ UNCHANGED dbVars
    /\ UNCHANGED <<rq, lv, nextId>>

\* L138
Cancel_IncrementStock_start(r) ==
    /\ pc[r] = "ca_inc"
    /\ WriteStart(r, "update", "Product", LAMBDA k, row : k = lv[r].oprod)
    /\ Go(r, "ca_inc_f")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

Cancel_IncrementStock_finish(r) ==
    /\ pc[r] = "ca_inc_f"
    /\ \/ /\ WriteFinish(r, LAMBDA k, row : k = lv[r].oprod,
                            LAMBDA k, row : [row EXCEPT !.stock = @ + lv[r].oqty])
          /\ Go(r, "ca_commit") /\ UNCHANGED rs
       \/ Failed(r) /\ ToRollback(r, "ca_rollback", 500) /\ UNCHANGED dbVars
    /\ UNCHANGED <<rq, lv, nextId>>

Cancel_Commit(r) ==
    /\ pc[r] = "ca_commit"
    /\ Commit(r)
    /\ Reply(r, 200, rq[r].orderId)
    /\ UNCHANGED <<rq, lv, nextId>>

Cancel_Rollback(r) ==
    /\ pc[r] = "ca_rollback"
    /\ Rollback(r)
    /\ Go(r, "respond")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

\* POST /products/:id/restock ------------------------------------------- server.ts:L137-L153

Restock_Auth(r) == pc[r] = "rs_auth" /\ Auth(r, "rs_begin") /\ UNCHANGED <<rq, nextId>>

Restock_Begin(r) ==
    /\ pc[r] = "rs_begin"
    /\ Begin(r, "RC")
    /\ Go(r, "rs_lock")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

\* L144-L145: SELECT "stock" ... FOR UPDATE (a locking read)
Restock_LockProduct_start(r) ==
    /\ pc[r] = "rs_lock"
    /\ WriteStart(r, "lock", "Product", LAMBDA k, row : k = rq[r].productId)
    /\ Go(r, "rs_lock_f")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

\* L146-L147: 404 if absent; the new stock is computed in the app
Restock_LockProduct_finish(r) ==
    /\ pc[r] = "rs_lock_f"
    /\ \/ /\ WriteFinish(r, LAMBDA k, row : k = rq[r].productId, LAMBDA k, row : row)
          /\ IF Rows(r) = {}
             THEN ToRollback(r, "rs_rollback", 404) /\ UNCHANGED lv
             ELSE /\ SetLv(r, [lv[r] EXCEPT !.total = (CHOOSE p \in Rows(r) : TRUE)[2].stock + rq[r].quantity])
                  /\ Go(r, "rs_set") /\ UNCHANGED rs
       \/ Failed(r) /\ ToRollback(r, "rs_rollback", 500) /\ UNCHANGED <<lv, dbVars>>
    /\ UNCHANGED <<rq, nextId>>

\* L148: write the computed value (absolute)
Restock_SetStock_start(r) ==
    /\ pc[r] = "rs_set"
    /\ WriteStart(r, "update", "Product", LAMBDA k, row : k = rq[r].productId)
    /\ Go(r, "rs_set_f")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

Restock_SetStock_finish(r) ==
    /\ pc[r] = "rs_set_f"
    /\ \/ /\ WriteFinish(r, LAMBDA k, row : k = rq[r].productId,
                            LAMBDA k, row : [row EXCEPT !.stock = lv[r].total])
          /\ Go(r, "rs_commit") /\ UNCHANGED rs
       \/ Failed(r) /\ ToRollback(r, "rs_rollback", 500) /\ UNCHANGED dbVars
    /\ UNCHANGED <<rq, lv, nextId>>

Restock_Commit(r) ==
    /\ pc[r] = "rs_commit"
    /\ Commit(r)
    /\ Reply(r, 200, rq[r].productId)
    /\ UNCHANGED <<rq, lv, nextId>>

Restock_Rollback(r) ==
    /\ pc[r] = "rs_rollback"
    /\ Rollback(r)
    /\ Go(r, "respond")
    /\ UNCHANGED <<rq, lv, rs, nextId>>

\* All handler steps ----------------------------------------------------------

\* The trace action name of the step request r takes next (one per SQL statement).
StepName(r) ==
    CASE pc[r] \in {"co_auth", "lo_auth", "ca_auth", "rs_auth"} -> "Auth_ReadUser"
      [] pc[r] = "gp_read"     -> "GetProduct_Read"
      [] pc[r] = "lo_read"     -> "ListOrders_Read"
      [] pc[r] = "co_idem"     -> "Checkout_FindByKey"
      [] pc[r] \in {"co_begin", "ca_begin", "rs_begin"} -> "Begin"
      [] pc[r] = "co_product"  -> "Checkout_ReadProduct"
      [] pc[r] = "co_coupon"   -> "Checkout_ReadCoupon"
      [] pc[r] = "co_ins"      -> "Checkout_InsertOrder_start"
      [] pc[r] = "co_ins_f"    -> "Checkout_InsertOrder_finish"
      [] pc[r] = "co_red"      -> "Checkout_InsertRedemption_start"
      [] pc[r] = "co_red_f"    -> "Checkout_InsertRedemption_finish"
      [] pc[r] = "co_dec"      -> "Checkout_DecrementStock_start"
      [] pc[r] = "co_dec_f"    -> "Checkout_DecrementStock_finish"
      [] pc[r] \in {"co_commit", "ca_commit", "rs_commit"} -> "Commit"
      [] pc[r] \in {"co_rollback", "ca_rollback", "rs_rollback"} -> "Rollback"
      [] pc[r] = "rs_lock"     -> "Restock_LockProduct_start"
      [] pc[r] = "rs_lock_f"   -> "Restock_LockProduct_finish"
      [] pc[r] = "rs_set"      -> "Restock_SetStock_start"
      [] pc[r] = "rs_set_f"    -> "Restock_SetStock_finish"
      [] pc[r] = "ca_mark"     -> "Cancel_MarkCancelled_start"
      [] pc[r] = "ca_mark_f"   -> "Cancel_MarkCancelled_finish"
      [] pc[r] = "ca_read"     -> "Cancel_ReadOrder"
      [] pc[r] = "ca_del"      -> "Cancel_DeleteRedemptions_start"
      [] pc[r] = "ca_del_f"    -> "Cancel_DeleteRedemptions_finish"
      [] pc[r] = "ca_inc"      -> "Cancel_IncrementStock_start"
      [] pc[r] = "ca_inc_f"    -> "Cancel_IncrementStock_finish"
      [] OTHER                 -> "none"

Step(r) ==
    \/ GetProduct_Read(r)
    \/ ListOrders_Auth(r) \/ ListOrders_Read(r)
    \/ Checkout_Auth(r) \/ Checkout_FindByKey(r) \/ Checkout_Begin(r) \/ Checkout_ReadProduct(r)
    \/ Checkout_ReadCoupon(r) \/ Checkout_InsertOrder_start(r) \/ Checkout_InsertOrder_finish(r)
    \/ Checkout_InsertRedemption_start(r) \/ Checkout_InsertRedemption_finish(r)
    \/ Checkout_DecrementStock_start(r) \/ Checkout_DecrementStock_finish(r)
    \/ Checkout_Commit(r) \/ Checkout_Rollback(r)
    \/ Cancel_Auth(r) \/ Cancel_Begin(r) \/ Cancel_MarkCancelled_start(r) \/ Cancel_MarkCancelled_finish(r)
    \/ Cancel_ReadOrder(r) \/ Cancel_DeleteRedemptions_start(r) \/ Cancel_DeleteRedemptions_finish(r)
    \/ Cancel_IncrementStock_start(r) \/ Cancel_IncrementStock_finish(r)
    \/ Cancel_Commit(r) \/ Cancel_Rollback(r)
    \/ Restock_Auth(r) \/ Restock_Begin(r) \/ Restock_LockProduct_start(r) \/ Restock_LockProduct_finish(r)
    \/ Restock_SetStock_start(r) \/ Restock_SetStock_finish(r) \/ Restock_Commit(r) \/ Restock_Rollback(r)

=============================================================================
