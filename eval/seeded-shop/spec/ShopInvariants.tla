--------------------------- MODULE ShopInvariants ---------------------------
(***************************************************************************)
(* Invariants of the Shop model, from the guarantees in the app's README.  *)
(* Shared by model checking (MC.tla) and by trace checks of reproductions  *)
(* (does a recorded run end in a state that violates one of these?).      *)
(***************************************************************************)
EXTENDS Shop

\* README: "stock never goes negative".
NoNegativeStock == \A k \in CommittedKeys("Product") : Committed("Product", k).stock >= 0

\* README: a retried checkout with the same Idempotency-Key returns the original order.
OneOrderPerIdempotencyKey ==
    \A k1, k2 \in CommittedKeys("Order") :
        LET o1 == Committed("Order", k1)
            o2 == Committed("Order", k2)
        IN (k1 # k2 /\ o1.idempotencyKey # "" /\ o1.userId = o2.userId) => o1.idempotencyKey # o2.idempotencyKey

\* README: "each coupon can be redeemed at most once per user".
CouponOncePerUser ==
    \A k1, k2 \in CommittedKeys("CouponRedemption") :
        LET a == Committed("CouponRedemption", k1)
            b == Committed("CouponRedemption", k2)
        IN k1 # k2 => ~(a.couponId = b.couponId /\ a.userId = b.userId)

=============================================================================
