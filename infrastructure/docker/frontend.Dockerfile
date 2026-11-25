FROM node:24-alpine AS dependencies
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY apps/frontend/package.json apps/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM dependencies AS build
COPY apps/frontend/ ./
ARG API_BASE_URL=http://api:8100
ENV API_BASE_URL=$API_BASE_URL
RUN npm test && npm run typecheck && npm run build

FROM node:24-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3100
RUN addgroup -g 10001 murrvideo \
    && adduser -D -H -u 10001 -G murrvideo murrvideo
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.next/standalone ./
COPY --from=build --chown=10001:10001 /app/.next/static ./.next/static
COPY --from=build --chown=10001:10001 /app/public ./public
USER 10001:10001
EXPOSE 3100
CMD ["node", "server.js"]
