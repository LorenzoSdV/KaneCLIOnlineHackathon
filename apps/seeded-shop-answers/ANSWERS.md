# seeded-shop answer key

**Never give this folder to an agent.** Specula runs point only at `apps/seeded-shop/`.
This file is the ground truth used to score pipeline runs.

| ID | Status | Expected verdict |
|---|---|---|
| B1 | planted (M1) | REPRODUCED |
| B3 | planted (M1) | REPRODUCED |
| C1 | control (M1) | no finding |
| B2 | planned (M7) | REPRODUCED |
| B4 | planned (M7) | MASKED |

## B1 — oversell (check-then-act inside a READ COMMITTED transaction)

- **Where:** `src/server.ts`, `POST /checkout`: `tx.product.findUnique` → `product.stock < quantity` check → `tx.order.create` → `tx.product.update({ stock: { decrement } })`.
- **Mechanism:** the interactive transaction runs at Postgres' default READ COMMITTED. Two checkouts both read `stock = 1`, both pass the check, both insert an order, and both decrement. The decrement is an atomic `stock = stock - 1`, so the second one waits for the first one's row lock, then applies to the committed value. Nothing re-checks the stock, and there is no `CHECK (stock >= 0)`.
- **Violated contract:** README, "stock never goes negative"; an order is only placed when stock is available.
- **Invariant:** `stock >= 0`, and the sum of quantities of non-cancelled orders per product is at most its initial stock.
- **Observable:** `GET /products/1` shows `stock: -1`, and two users both hold an order for the last item.
- **Black-box trigger:** two concurrent `POST /checkout` for product 1 from different users. It reproduced in 15 of 15 runs on the M1 sanity check.
- **Standard fixes:** a conditional update (`updateMany where stock >= quantity`, then check the count), `SELECT … FOR UPDATE`, or a `CHECK` constraint plus error handling.

## B3 — duplicate order on concurrent retry (idempotency check without a unique constraint)

- **Where:** `src/server.ts`, `POST /checkout`: `prisma.order.findFirst({ userId, idempotencyKey })` runs **outside** and before the transaction. `Order` has only a non-unique `@@index([userId, idempotencyKey])`.
- **Mechanism:** a client times out and retries while the first request is still in flight. Both lookups find nothing, and both create an order.
- **Violated contract:** README, "clients may retry a checkout safely by sending the same Idempotency-Key".
- **Invariant:** at most one order per `(userId, idempotencyKey)` when the key is non-null.
- **Observable:** `GET /orders` lists two orders created by one logical checkout, and stock is decremented twice.
- **Black-box trigger:** two concurrent `POST /checkout` with the same `Idempotency-Key` from the same user. It reproduced in 15 of 15 runs.
- **Standard fixes:** `@@unique([userId, idempotencyKey])`, then catch P2002 and return the existing order.

## C1 — coupon redemption (correct; false-positive control)

- **Where:** `src/server.ts`, `POST /checkout`: `tx.couponRedemption.create` inside the transaction, with `@@unique([couponId, userId])`. On P2002 the transaction rolls back (order and stock decrement included) and the request returns 409.
- **Why it is correct:** the database enforces one redemption per user per coupon, regardless of interleaving, and the rollback undoes the order.
- **Expected:** no finding. A reported coupon double-redemption is a false positive: the model is missing the UNIQUE constraint (Case B, SPEC_REPAIR).
- **Sanity check:** three concurrent redemptions gave statuses `[201, 409, 409]` and stock 99, with 0 anomalies in 15 runs.

## Notes on other behavior (not planted; do not score)

- Incidental defect found by M3 trace validation: a request with `content-type: application/json` and an
  empty body (e.g. `POST /orders/1/cancel` sent that way) gets **500** instead of 400. Fastify rejects the
  empty JSON body with a 400 error, but the app's error handler only special-cases `HttpError` and validation
  errors, so it reports 500. It is a sequential input-handling defect, outside the concurrency scope; leave it
  unfixed and do not score it.

- `POST /orders/:id/cancel` uses a conditional `updateMany` on `status: PENDING`, so a double cancel cannot restore stock twice.
- If B1 lets stock go to −1, cancelling restores it by the order's quantity. That is consistent behavior given B1, not a separate bug.
