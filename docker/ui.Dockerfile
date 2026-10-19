# The reviewer UI: Next.js production build (standalone output). /api/* is forwarded to PRIORAUTH_API_URL, read at
# request time, so one image works with any API address.
# Behind a TLS-intercepting proxy: docker build --secret id=extra_ca,src=/path/to/ca-bundle.crt ... (optional).
#   docker build -f docker/ui.Dockerfile -t priorauth-ui .
ARG BASE_REGISTRY=docker.io/library
FROM ${BASE_REGISTRY}/node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci --no-audit --no-fund
COPY ui/ ./
ENV NEXT_TELEMETRY_DISABLED=1 NEXT_OUTPUT=standalone
RUN npm run build

FROM ${BASE_REGISTRY}/node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
WORKDIR /ui
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /ui/.next/standalone ./
COPY --from=build --chown=node:node /ui/.next/static ./.next/static
USER node
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
    CMD node -e "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
