# usf (ultimate slop factory)

The **platform** in an autonomous-maintenance experiment: a control plane plus a
dev-agent image that maintains a *product* repo — any codebase, passed as a parameter
(first instance: the sibling `discord-bot`) — by working like an engineer — branch,
code, test, open a PR — through GitHub. Built on
[`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python).

Design docs live here: **`ARCHITECTURE.md`** (the full design), **`GITHUB-INTERFACE.md`**
(the GitHub protocol), **`PHASE-0.md`** (the first build slice), **`TO-BE.md`** (the
org-scale target), **`PHASE-1.md`** (the TO-BE build plan).

## Quick start

```bash
uv sync
cp .env.example .env   # add CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)

# Build the dev-agent image, then drive one burst by hand:
docker build -f docker/dev-agent/Dockerfile -t usf/dev-agent:latest .
uv run control-plane burst --issue 1 --repo ../discord-bot --mode check --data-dir data

uv run pytest          # structural tests, no network
```

The service stores are optional and off by default. To run against them, bring up
the dev rig (Postgres + SeaweedFS) and point `LEDGER_DSN` at it:

```bash
docker compose -f docker/compose.yaml up -d --wait
export LEDGER_DSN='postgresql://platform_admin:platform@localhost:5432/platform?sslmode=disable'
uv run control-plane migrate          # apply the schema; a second run is a no-op
docker compose -f docker/compose.yaml down -v
```

## Status

**Phase 0, M0 done** — the `control-plane` / `dev-agent` workspace seam
(`packages/`), the warm dev-agent image (claude CLI + burst worker, no platform
code inside), and the manual burst choreography: prepare session volume → burst
span (`TRACEPARENT` into the container) → `docker run` → harvest (result +
violations → SQLite ledger, session volume → local archive). Next: M1 — one real
issue → PR burst with the walls verified. Build plan: `PHASE-0.md`.
