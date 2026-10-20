# The webhook receiver as an OCI image (TO-BE §5, E5): the platform's only
# public surface, and the smallest thing that can be — the receiver package,
# secret-refs and structlog, on a slim Python. No control-plane code, no ledger
# client, no GitHub client, no git, no docker: `tests/test_webhook_receiver.py`
# checks the import graph, and CI checks this image cannot `import control_plane`.
#
# Configuration is env (webhook_receiver.cli): WEBHOOK_APPS, WAKE_URL,
# RECEIVER_BIND. The webhook secrets are refs — env:/file:/helper: — so a
# deployment mounts a file or sets a variable, and the image holds nothing.
#
# Build (repo root): docker build -f docker/webhook-receiver/Dockerfile -t usf/webhook-receiver:latest .

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
# --package webhook-receiver: only the receiver and what it depends on — the
# rest of the workspace is not installed, so it is not in the image either
RUN uv sync --locked --no-dev --no-editable --package webhook-receiver


FROM python:3.12-slim

COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

RUN useradd --create-home --uid 1000 receiver
USER receiver

EXPOSE 8080
ENTRYPOINT ["webhook-receiver"]
CMD ["serve"]
