# The demo API: FastAPI with its worker pool in-process, a SQLite database seeded at start (state is lost on every
# restart, by design), example cases decided from recorded real model responses. No API key is needed or used.
#   docker build -f docker/api.Dockerfile -t priorauth-api .
# Base images are pulled from the public ECR mirror of Docker Hub (no anonymous pull limit), pinned by digest.
# Behind a TLS-intercepting proxy, pass its CA bundle as an optional build secret:
#   docker build --secret id=extra_ca,src=/path/to/ca-bundle.crt ...   (absent elsewhere, and then unused)
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-editable
# Only what the demo reads: the synthetic dev records and the recorded responses that decide them.
COPY data/dev ./data/dev
COPY eval/cassettes/agent_v2_haiku_pipeline/dev.jsonl ./eval/cassettes/agent_v2_haiku_pipeline/dev.jsonl
RUN useradd --uid 10001 --no-create-home demo
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["python", "-m", "priorauth.demo", "--host", "0.0.0.0", "--port", "8000"]
