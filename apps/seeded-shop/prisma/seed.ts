import { prisma } from "../src/db.js";

// Resets the database to a known starting state. Safe to run repeatedly.
async function main() {
  await prisma.$executeRawUnsafe(
    'TRUNCATE "CouponRedemption", "Order", "Coupon", "Product", "User" RESTART IDENTITY CASCADE',
  );

  await prisma.user.createMany({
    data: [
      { email: "alice@example.com", apiToken: "tok-alice" },
      { email: "bob@example.com", apiToken: "tok-bob" },
      { email: "carol@example.com", apiToken: "tok-carol" },
    ],
  });

  await prisma.product.createMany({
    data: [
      { name: "Limited Edition Print", priceCents: 5000, stock: 1 },
      { name: "T-Shirt", priceCents: 2000, stock: 100 },
    ],
  });

  await prisma.coupon.create({ data: { code: "WELCOME10", percentOff: 10 } });
}

main()
  .then(() => prisma.$disconnect())
  .catch(async (err) => {
    console.error(err);
    await prisma.$disconnect();
    process.exit(1);
  });
