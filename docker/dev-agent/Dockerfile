# Dev-agent warm base image (ARCHITECTURE.md §6): toolchain + claude CLI +
# the burst worker. A burst is then just clone/checkout + agent boot.
#
# Walls baked into the image (§8):
#   - only the dev-agent package is installed — no control-plane code in the box
#   - non-root user; no credentials of any kind in the image
#   - CLAUDE_CONFIG_DIR is the /session mount the control plane owns
#
# Build (repo root): docker build -f docker/dev-agent/Dockerfile -t usf/dev-agent:latest .

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Build the burst worker's wheels in isolation — nothing else from the repo
# enters the image. Two packages, not one: `session-volume` is what the worker,
# the control plane and the pod-side harvester all agree a burst leaves behind
# (G2), and it is stdlib-only, so carrying it costs the image nothing and
# leaving it out costs the image everything — `pip install` cannot resolve a
# workspace dependency that was never built.
COPY packages/dev-agent /src/dev-agent
COPY packages/session-volume /src/session-volume
RUN uv build --wheel /src/session-volume --out-dir /wheels \
    && uv build --wheel /src/dev-agent --out-dir /wheels


FROM python:3.12-slim

# bubblewrap: codex's read-only sandbox needs it on slim Debian (issue #3)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates ripgrep bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# uv — the product's env/deps tool (warm base: the burst just runs `uv sync`).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Codex CLI — the pre-PR reviewer. Auth is a control-plane-mounted ~/.codex
# (ChatGPT subscription); nothing is baked in. The real binary is codex-real;
# `codex` on PATH is the platform shim (issue #1): credential scrub + refusal
# of sandbox-weakening flags, enforced — not just instructed.
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         arm64) target="aarch64-unknown-linux-musl" ;; \
         amd64) target="x86_64-unknown-linux-musl" ;; \
         *) echo "unsupported arch: $arch" && exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/codex.tar.gz \
        "https://github.com/openai/codex/releases/latest/download/codex-${target}.tar.gz" \
    && tar -xzf /tmp/codex.tar.gz -C /usr/local/bin \
    && rm /tmp/codex.tar.gz \
    && { [ -f "/usr/local/bin/codex-${target}" ] && mv "/usr/local/bin/codex-${target}" /usr/local/bin/codex-real || true; } \
    && { [ -f "/usr/local/bin/codex" ] && mv /usr/local/bin/codex /usr/local/bin/codex-real || true; } \
    && chmod +x /usr/local/bin/codex-real
COPY docker/dev-agent/codex-shim.sh /usr/local/bin/codex
RUN chmod 755 /usr/local/bin/codex && codex --version

# gh CLI — the agent's GitHub interface (PRs, issues); reads GH_TOKEN from env.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 agent \
    && mkdir -p /session /workspace/repo \
    && chown -R agent:agent /session /workspace

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# The toolkit plugin: image-baked procedures (e.g. the codex-review skill).
# Root-owned on purpose — the agent can use these, never edit them.
COPY docker/dev-agent/toolkit /opt/dev-agent-toolkit

USER agent
ENV HOME=/home/agent \
    PATH="/home/agent/.local/bin:${PATH}" \
    CLAUDE_CONFIG_DIR=/session

# Claude Code CLI — the engine the SDK drives. Native standalone install.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && claude --version

WORKDIR /workspace/repo
# No `VOLUME` here, deliberately. Both paths are always mounted by whatever runs
# the burst — a bind mount under docker, an `emptyDir` in a pod — so the
# declaration bought nothing, and under containerd it *costs*: an image volume
# becomes an ephemeral empty mount at that exact path, which shadowed the working
# tree the init container had just cloned into the pod's shared volume (G3).

ENTRYPOINT ["dev-agent"]
CMD ["run"]
