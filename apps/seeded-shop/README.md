# seeded-shop

A small shop API: products with limited stock, checkout with optional coupons, and order cancellation.

Stack: Node.js 24, TypeScript (run with `tsx`), Fastify 5, Prisma 7 with the `pg` driver adapter, PostgreSQL 17.

## Run

```bash
docker compose -p seeded-shop up -d --build
curl localhost:3000/health
```

On start the app applies migrations and reseeds the database (`npm run db:seed` resets it at any time).

## API

All order endpoints authenticate with `Authorization: Bearer <apiToken>`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/products/:id` | Product with current stock |
| `GET` | `/orders` | The caller's orders |
| `POST` | `/checkout` | Place an order: `{ "productId", "quantity", "couponCode"? }` |
| `POST` | `/orders/:id/cancel` | Cancel a pending order and return its stock |
| `POST` | `/products/:id/restock` | Add stock: `{ "quantity" }`. Concurrent restocks all count |

### Checkout guarantees

- An order is only placed when enough stock is available; stock never goes negative.
- Each coupon can be redeemed at most once per user.
- **Retries:** clients may retry a checkout safely by sending the same `Idempotency-Key` header. A retried request returns the original order instead of placing a new one.

### Responses

`201` order placed · `200` existing order returned for a repeated `Idempotency-Key` · `401` bad token · `404` unknown product or coupon · `409` insufficient stock, coupon already redeemed, or order not cancellable.

## Seed data

| Users (token) | Products | Coupons |
|---|---|---|
| alice (`tok-alice`), bob (`tok-bob`), carol (`tok-carol`) | 1: Limited Edition Print, stock 1 · 2: T-Shirt, stock 100 | `WELCOME10` (10% off) |
