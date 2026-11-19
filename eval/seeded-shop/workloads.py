"""seeded-shop workloads for webkit/workload/runner.py (the reference harness for this eval target).

    python3 webkit/workload/runner.py run --stack eval/seeded-shop/stack.toml \
        --mapping eval/seeded-shop/mapping.toml --workloads eval/seeded-shop/workloads.py --traces DIR

Scenarios cover every endpoint and the concurrent interactions the model must reproduce: a sequential
mix (including error paths) and races between overlapping requests. These are workloads for trace
validation, not bug reproductions: they record whatever the app does.
"""


def checkout(c, token, product, qty, coupon=None, idem=None):
    body = {"productId": product, "quantity": qty}
    if coupon:
        body["couponCode"] = coupon
    return c.post("/checkout", json=body, token=token, headers={"idempotency-key": idem} if idem else None)


def sequential(c):
    c.get("/health")
    c.get("/products/1"), c.get("/products/9")
    checkout(c, "tok-bob", 2, 2, idem="s-1"), checkout(c, "tok-bob", 2, 2, idem="s-1")
    checkout(c, "tok-alice", 2, 1, coupon="WELCOME10"), checkout(c, "tok-alice", 2, 1, coupon="WELCOME10")
    checkout(c, "tok-carol", 2, 1, coupon="NOPE"), checkout(c, "tok-carol", 9, 1)
    checkout(c, "tok-carol", 1, 3), checkout(c, "nope", 2, 1)
    c.post("/checkout", json={"productId": 2, "quantity": 1})
    c.get("/orders", token="tok-bob")
    c.post("/orders/1/cancel", token="tok-bob"), c.post("/orders/1/cancel", token="tok-bob")
    c.post("/orders/2/cancel", token="tok-bob")
    c.post("/products/2/restock", json={"quantity": 3}, token="tok-alice")
    c.post("/products/9/restock", json={"quantity": 1}, token="tok-alice")
    c.get("/products/2")


SCENARIOS = {
    "sequential": sequential,
    # Two concurrent restocks of one product.
    "restock_race": lambda c: c.together(
        lambda: c.post("/products/2/restock", json={"quantity": 1}, token="tok-alice"),
        lambda: c.post("/products/2/restock", json={"quantity": 2}, token="tok-carol")),
    # Two buyers of the last item.
    "last_item": lambda c: c.together(lambda: checkout(c, "tok-alice", 1, 1), lambda: checkout(c, "tok-carol", 1, 1)),
    # A client retry overlapping the original request, same Idempotency-Key.
    "retry_same_key": lambda c: c.together(lambda: checkout(c, "tok-bob", 2, 1, idem="r-1"),
                                           lambda: checkout(c, "tok-bob", 2, 1, idem="r-1")),
    # Three concurrent redemptions of one coupon by one user.
    "coupon_race": lambda c: c.together(*[lambda: checkout(c, "tok-bob", 2, 1, coupon="WELCOME10")] * 3),
    # Concurrent cancels of the same order, after placing it.
    "cancel_race": lambda c: [checkout(c, "tok-bob", 2, 3, coupon="WELCOME10"),
                              c.together(lambda: c.post("/orders/1/cancel", token="tok-bob"),
                                         lambda: c.post("/orders/1/cancel", token="tok-bob"))],
    # Eight mixed requests at once on a pool of two connections.
    "mixed": lambda c: c.together(
        lambda: checkout(c, "tok-alice", 1, 1), lambda: checkout(c, "tok-carol", 1, 1),
        lambda: checkout(c, "tok-bob", 2, 1, idem="m-1"), lambda: checkout(c, "tok-bob", 2, 1, idem="m-1"),
        lambda: checkout(c, "tok-alice", 2, 2, coupon="WELCOME10"),
        lambda: checkout(c, "tok-carol", 2, 1, coupon="WELCOME10"),
        lambda: c.get("/products/1"), lambda: c.get("/orders", token="tok-bob")),
}
