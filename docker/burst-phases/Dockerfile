# The burst's two side containers as one OCI image (TO-BE §6, G2): `burst-init`
# fills the volume, `burst-harvest` ships what the burst left. A pod runs the
# first as its init container and the third as its harvester; the agent image in
# between is untouched.
#
# What is deliberately *not* here is the point of the image. No control-plane
# code, no ledger client, no storage client, no cloud SDK — the phases hold
# presigned URLs for single objects, minted at spawn and expiring inside the
# burst's rail, so there is nothing for a credential to be. CI checks this image
# cannot `import control_plane`, the way it checks the receiver's.
#
# `git` is the one tool: init clones the product with a `contents:read`,
# single-repo token, which is how a reviewer or tester agent gets a working tree
# without ever seeing a GitHub credential itself.
#
# Configuration is env (burst_phases.phases): BURST_VOLUME_DIR,
# BURST_SESSION_GET_URL + BURST_SESSION_SHA256, BURST_SESSION_PUT_URL +
# BURST_SESSION_KEY, BURST_ARTIFACTS_PUT_URL, and the clone's four.
#
# Build (repo root): docker build -f docker/burst-phases/Dockerfile -t usf/burst-phases:latest .

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
# --package burst-phases: this package and `session-volume`, and nothing else in
# the workspace — what is not installed cannot be imported in a pod
RUN uv sync --locked --no-dev --no-editable --package burst-phases


FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# The volume is mounted at the same path in every container of the pod, and the
# phases are told which one it is — they never guess.
RUN useradd --create-home --uid 1000 phases
USER phases

ENTRYPOINT ["burst-init"]
