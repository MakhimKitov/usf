# The control plane as an OCI image (TO-BE §5, E5): one stateless replica per
# container, N of them over one ledger. Everything a replica is comes in through
# 12-factor env — LEDGER_DSN, ARCHIVE_URL, the App refs, WAKE_BIND — and through
# `--data-dir`, which holds only this process's own scratch (volumes, logs, the
# platform clone).
#
# Bursts run one of two ways and the image carries both clients, because which
# one a deployment uses is one env variable (`BURST_NAMESPACE`) and not a second
# image. `DockerRunner` needs the docker *CLI* and the host's daemon socket
# mounted at /var/run/docker.sock — that daemon reads bind-mount paths in *its*
# namespace, so the data dir must be mounted at the same absolute path inside and
# out, which docker/compose.yaml's `platform` profile does. `JobsRunner` (G3)
# needs `kubectl` and a kubeconfig or an in-cluster service account.
#
# Build (repo root): docker build -f docker/control-plane/Dockerfile -t usf/control-plane:latest .

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /usr/local/bin/uv

# The workspace, resolved from the lockfile: control-plane and the two leaf
# packages it depends on (dev-agent for the env contract, secret-refs for the
# refs), nothing editable — the venv is the artifact, the sources stay here.
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
RUN uv sync --locked --no-dev --no-editable --package control-plane


FROM python:3.12-slim

# git: the platform-owned clone (SCHEDULER.md, "The clone").
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# The docker CLI alone — no daemon, no dind: `docker run` here talks to the
# socket the deployment mounts. Static binary, pinned.
ARG DOCKER_CLI_VERSION=28.3.3
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in arm64) target="aarch64" ;; amd64) target="x86_64" ;; *) echo "unsupported arch: $arch" && exit 1 ;; esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${target}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && docker --version

# kubectl, for `JobsRunner` (G3). Pinned, and the client alone — the runner
# speaks four verbs in one namespace and the account it speaks them with is the
# cluster's to grant (examples/k8s/burst-runner.yaml).
ARG KUBECTL_VERSION=v1.34.1
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL -o /usr/local/bin/kubectl \
       "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client=true

COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# Root, on purpose and for now: the daemon socket a deployment mounts is
# root-owned on most hosts. The replica holds the App keys as refs either way.
WORKDIR /srv/usf
ENTRYPOINT ["control-plane"]
CMD ["run", "--help"]
