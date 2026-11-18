import Fastify, { type FastifyReply, type FastifyRequest } from "fastify";
import { prisma } from "./db.js";
import type { Order, User } from "./generated/prisma/client.js";

class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function isUniqueViolation(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { code?: unknown }).code === "P2002";
}

async function authenticate(req: FastifyRequest): Promise<User> {
  const header = req.headers.authorization ?? "";
  const token = header.startsWith("Bearer ") ? header.slice("Bearer ".length) : "";
  if (!token) throw new HttpError(401, "missing bearer token");
  const user = await prisma.user.findUnique({ where: { apiToken: token } });
  if (!user) throw new HttpError(401, "invalid token");
  return user;
}

function serializeOrder(order: Order) {
  return {
    id: order.id,
    productId: order.productId,
    quantity: order.quantity,
    totalCents: order.totalCents,
    status: order.status,
    createdAt: order.createdAt.toISOString(),
  };
}

interface CheckoutBody {
  productId: number;
  quantity: number;
  couponCode?: string;
}

const checkoutSchema = {
  body: {
    type: "object",
    required: ["productId", "quantity"],
    additionalProperties: false,
    properties: {
      productId: { type: "integer", minimum: 1 },
      quantity: { type: "integer", minimum: 1, maximum: 10 },
      couponCode: { type: "string", minLength: 1, maxLength: 64 },
    },
  },
} as const;

export function buildServer() {
  const app = Fastify({ logger: process.env.LOG_LEVEL ? { level: process.env.LOG_LEVEL } : false });

  app.setErrorHandler((err, _req, reply: FastifyReply) => {
    if (err instanceof HttpError) {
      return reply.code(err.status).send({ error: err.message });
    }
    if ((err as { validation?: unknown }).validation) {
      return reply.code(400).send({ error: err.message });
    }
    app.log.error(err);
    return reply.code(500).send({ error: "internal error" });
  });

  app.get("/health", async () => ({ ok: true }));

  app.get<{ Params: { id: string } }>("/products/:id", async (req) => {
    const product = await prisma.product.findUnique({ where: { id: Number(req.params.id) } });
    if (!product) throw new HttpError(404, "product not found");
    return { id: product.id, name: product.name, priceCents: product.priceCents, stock: product.stock };
  });

  app.get("/orders", async (req) => {
    const user = await authenticate(req);
    const orders = await prisma.order.findMany({ where: { userId: user.id }, orderBy: { id: "asc" } });
    return orders.map(serializeOrder);
  });

  // Places an order. Clients may retry safely by resending the same Idempotency-Key header.
  app.post<{ Body: CheckoutBody }>("/checkout", { schema: checkoutSchema }, async (req, reply) => {
    const user = await authenticate(req);
    const { productId, quantity, couponCode } = req.body;
    const idempotencyKey = (req.headers["idempotency-key"] as string | undefined) || null;

    if (idempotencyKey) {
      const existing = await prisma.order.findFirst({ where: { userId: user.id, idempotencyKey } });
      if (existing) return reply.code(200).send(serializeOrder(existing));
    }

    try {
      const order = await prisma.$transaction(async (tx) => {
        const product = await tx.product.findUnique({ where: { id: productId } });
        if (!product) throw new HttpError(404, "product not found");
        if (product.stock < quantity) throw new HttpError(409, "insufficient stock");

        let totalCents = product.priceCents * quantity;
        let couponId: number | null = null;
        if (couponCode) {
          const coupon = await tx.coupon.findUnique({ where: { code: couponCode } });
          if (!coupon) throw new HttpError(404, "coupon not found");
          totalCents = Math.round((totalCents * (100 - coupon.percentOff)) / 100);
          couponId = coupon.id;
        }

        const created = await tx.order.create({
          data: { userId: user.id, productId, quantity, totalCents, idempotencyKey },
        });
        if (couponId !== null) {
          await tx.couponRedemption.create({ data: { couponId, userId: user.id, orderId: created.id } });
        }
        await tx.product.update({ where: { id: productId }, data: { stock: { decrement: quantity } } });
        return created;
      });
      return reply.code(201).send(serializeOrder(order));
    } catch (err) {
      if (isUniqueViolation(err)) throw new HttpError(409, "coupon already redeemed");
      throw err;
    }
  });

  app.post<{ Params: { id: string } }>("/orders/:id/cancel", async (req) => {
    const user = await authenticate(req);
    const orderId = Number(req.params.id);
    return prisma.$transaction(async (tx) => {
      const { count } = await tx.order.updateMany({
        where: { id: orderId, userId: user.id, status: "PENDING" },
        data: { status: "CANCELLED" },
      });
      if (count === 0) throw new HttpError(409, "order cannot be cancelled");
      const order = await tx.order.findUniqueOrThrow({ where: { id: orderId } });
      await tx.couponRedemption.deleteMany({ where: { orderId } });
      await tx.product.update({ where: { id: order.productId }, data: { stock: { increment: order.quantity } } });
      return serializeOrder(order);
    });
  });

  return app;
}

const port = Number(process.env.PORT ?? 3000);
const app = buildServer();
app.listen({ host: "0.0.0.0", port }).catch((err) => {
  console.error(err);
  process.exit(1);
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    void app.close().then(() => prisma.$disconnect()).then(() => process.exit(0));
  });
}
