FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN DATABASE_URL=postgresql://build:build@localhost:5432/build npx prisma generate
ENV NODE_ENV=production PORT=3000
EXPOSE 3000
CMD ["sh", "-c", "npx prisma migrate deploy && npm run db:seed && npm start"]
