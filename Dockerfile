# The frontend, deployable.
#
# This is a TanStack Start app, which builds a Nitro SERVER — not a static
# bundle. `vite build` writes .output/ (server/index.mjs plus public/), and
# never writes dist/. The first version of this file copied /app/dist and
# served it with `serve -s dist`, so the build either failed on a missing
# directory or produced a container serving nothing at all.

FROM node:20-alpine AS builder

WORKDIR /app
COPY package*.json ./
RUN npm ci

# VITE_* variables are inlined into the bundle at BUILD time, so they have to
# arrive as build args. Setting them in the container's `environment:` — as
# docker-compose did — has no effect whatsoever: by then the bundle is
# already compiled and the value is baked in.
#
# The default points at localhost rather than the `engine` service name on
# purpose. This URL is fetched by the BROWSER, which runs on the host, and
# `http://engine:8001` only resolves inside the compose network.
ARG VITE_ENGINE_URL=http://localhost:8001
ENV VITE_ENGINE_URL=$VITE_ENGINE_URL

COPY . .

# Build a NODE server, not a Cloudflare Worker.
#
# This project's vite config (@lovable.dev/vite-tanstack-config) defaults Nitro
# to the cloudflare preset, so `npm run build` wrote .output/server/index.mjs
# as a Worker module — an exported fetch handler, plus a wrangler.json. Running
# that under `node` loads the module, finds nothing to execute, and exits 0.
# The container went up and straight back down with no logs at all, which is
# the hardest kind of failure to read.
RUN NITRO_PRESET=node-server npm run build  && test -f .output/server/index.mjs  && grep -q '"preset": *"node' .output/nitro.json

# ── runtime ────────────────────────────────────────────────────────────────
FROM node:20-alpine

WORKDIR /app
# .output holds both halves: server/index.mjs is the entrypoint and public/
# the compiled assets it serves.
COPY --from=builder /app/.output ./.output

ENV PORT=8080
ENV HOST=0.0.0.0
EXPOSE 8080

CMD ["node", ".output/server/index.mjs"]
