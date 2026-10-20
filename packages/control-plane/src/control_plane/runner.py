"""Where bursts run. One container = one burst, then it dies (§6).

`Runner` is the seam (E4, TO-BE §6) — the third protocol beside `Ledger` and
`SessionArchive`: `DockerRunner` here, `JobsRunner` in `jobs.py` (G3). What the
box is given is decided here once, for every runner: `BurstSpec` is the burst,
`burst_env` the environment the box gets. The env contract's names come from
``dev_agent.env`` (single source of truth; the dependency points control-plane →
dev-agent only).

Phase 0's `DockerRunner` shells out to the ``docker`` CLI — no daemon-API
dependency.
"""

from __future__ import annotations

import codecs
import os
import selectors
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import structlog
from dev_agent import env as agentenv

from control_plane.store import SessionArchive
from control_plane.telemetry import agent_telemetry_env
from control_plane.volume import (
    BurstArtifacts,
    Snapshot,
    collect_volume,
    materialize_volume,
)

log = structlog.get_logger()

DEFAULT_IMAGE = "usf/dev-agent:latest"

# The one variable that decides where bursts run (`open_runner`). Named here
# rather than in `jobs`, because the toggle belongs to the seam, not to one side
# of it — the way ARCHIVE_URL belongs to `open_archive` and not to boto3.
NAMESPACE_ENV = "BURST_NAMESPACE"


class RunnerError(RuntimeError):
    """This deployment cannot run bursts as configured."""

# How often a running burst's heartbeat is asked while the container is up (E1).
# The callback is cheap and decides for itself whether this beat renews the lease
# in the ledger; the runner only guarantees it is asked at least this often,
# whether or not the container has printed anything — a hung Bash call (finding
# #6) prints nothing for 18 minutes and must not look dead.
HEARTBEAT_POLL_S = 5.0

# The ceiling on one Bash call inside a burst (H2, #104). A ceiling in code, not
# a row: how long a single command may run is a platform-safety question, and a
# product that needs longer says so per call rather than raising a floor for
# everyone. The pair is the CLI's own — `BASH_DEFAULT_TIMEOUT_MS` is what a call
# gets when the model asks for nothing, `BASH_MAX_TIMEOUT_MS` the most it may ask
# for — so the enforcement is the engine's and this is the policy.
#
# Why these numbers: finding #6 measured 18 minutes lost to one stuck command,
# with the burst's budget draining and the heartbeat happily renewing a lease on
# work that had stopped. Two minutes covers the ordinary command; ten is the most
# a deliberate one (a full test run, an image build) may ask for.
#
# What the engine does at the ceiling, measured on a live burst (#104): it does
# not kill the command, it *backgrounds* it and hands control back. The agent is
# free at two minutes, which is the whole of finding #6; the process itself keeps
# running until the burst's container ends, which is what makes the wall-clock
# rail — `activeDeadlineSeconds` on a Job, the container's life under docker —
# the thing that actually bounds it. A rail, not a wall: the burst continues on
# its own terms rather than being stopped.
BASH_CALL_TIMEOUT_S = 120
BASH_CALL_CEILING_S = 600

# The runner's contract for the callback: True keeps going; False means the
# lease is lost — a peer has taken the task — and the runner fences the burst by
# killing its container. Work nobody holds a lease on must not keep pushing.
Heartbeat = Callable[[], bool]

# Docker's default seccomp profile blocks unprivileged user namespaces, which
# codex's bubblewrap read-only sandbox needs (issue #3). This is moby v28.3.0's
# default profile plus an allow rule for the userns/mount syscalls — measured
# widening, not `unconfined`. Verified live: bwrap + a real read-only codex
# review run inside the container.
SECCOMP_PROFILE = Path(__file__).parent / "resources" / "seccomp-userns.json"

# The burst model is always pinned explicitly — "unset" must never mean whatever
# `claude` the image happens to ship with (that default drifts across CLI
# releases, silently changing the experiment's key variable). Since D5 the pin
# is the role row's `model` (seeded 'claude-sonnet-5'; `role set … --model`),
# read at every spawn — the env override this module used to carry is retired.


@dataclass(slots=True)
class BurstSpec:
    """Everything needed to run one burst container."""

    task_id: str
    burst_id: str
    volume_dir: Path
    repo_mount: Path
    prompt: str = ""
    role: str = "dev"
    kind: str = "work"
    command: str = "run"  # or "check" (boot smoke, no network)
    image: str = DEFAULT_IMAGE
    resume_session_id: str | None = None
    # A new branch of the session tree (H5): the node being resumed already has
    # an abandoned attempt grown from it, so the SDK forks to a new session id
    # rather than continuing the one that attempt continued.
    fork_session: bool = False
    max_budget_usd: float | None = None
    max_turns: int | None = None
    model: str | None = None
    traceparent: str | None = None
    github_token: str | None = None
    git_user_name: str | None = None
    git_user_email: str | None = None
    # Host dir with codex auth.json (ChatGPT subscription). Mounted rw at
    # /home/agent/.codex so token refresh persists across bursts.
    codex_auth_dir: Path | None = None
    # Reviewer bursts get the clone read-only — "never modify" is a wall, not
    # an instruction (REVIEWERS.md); anything they execute copies to /tmp first.
    repo_read_only: bool = False
    # The Anthropic OAuth token this burst runs on, resolved at spawn from the
    # picked subscription's ref (F2). None = an empty pool, and the box gets
    # whatever credential this machine holds — the MVP's shape. Never logged,
    # never archived, never written to a row: the ledger keeps the id.
    oauth_token: str | None = None
    # Where this burst's snapshot goes, or None for a burst whose volume is not
    # archived (the reviewer, B5). Decided at spawn rather than asked for at
    # `collect`, because a runner whose phases run beside the burst (G3) has to
    # mint the PUT for that key *before* the pod starts — and a key that is
    # settled at spawn is the same key on both substrates.
    archive_as: str | None = None
    # What a pod-side init container has to fetch to make the working tree the
    # host path mounts: ``(url, ref)`` — origin and the exact commit the
    # platform's own clone is pinned to, so the tree in the pod is the tree
    # `repo_mount` would have been. Ignored by runners that share a filesystem
    # with the control plane.
    clone: tuple[str, str] | None = None
    # The credential for that clone: `contents:read` on the one repository,
    # handed to the init container and to no other (TO-BE §6). It is a second
    # token on purpose — a reviewer or tester agent still holds none.
    clone_token: str | None = None
    # The isolation runtime this burst runs under (G4, TO-BE §6): a k8s
    # `RuntimeClass` name on a cluster, a `docker run --runtime` name under
    # docker. None is the substrate's default. The scheduler never sets it — the
    # deployment's default is the decision — and the golden-replay harness sets
    # it per trial, so one archived node can be run under each candidate.
    runtime_class: str | None = None
    # The hosts the product declared for this burst on top of the platform's own
    # (G6): a tester's sandbox endpoints, from `TESTING.md`'s `egress:` — the
    # same manifest its sandbox credentials come from. Data for the egress
    # declaration (`control_plane.egress`); nothing in the box reads it.
    egress: tuple[str, ...] = ()
    extra_env: dict[str, str] = field(default_factory=dict)


def burst_env(spec: BurstSpec) -> dict[str, str]:
    """The environment the box is given — the same wherever the burst runs.

    Every runner hands this to its container verbatim, so the rules in it are
    decided once: which credential enters (exactly one Anthropic credential,
    subscription first, none on a ``check``), what the agent's own OTel is told,
    and which sandbox refs a tester burst resolved at spawn (``extra_env``, D4).
    A runner packages it — ``-e`` flags, a pod's ``env`` — and adds nothing.
    """
    env: dict[str, str] = {
        agentenv.TASK_ID: spec.task_id,
        agentenv.BURST_ID: spec.burst_id,
        agentenv.ROLE: spec.role,
        agentenv.BURST_KIND: spec.kind,
        agentenv.CLAUDE_CONFIG_DIR: agentenv.CONTAINER_SESSION_DIR,
    }
    if spec.prompt:
        env[agentenv.TASK_PROMPT] = spec.prompt
    if spec.resume_session_id:
        env[agentenv.RESUME_SESSION_ID] = spec.resume_session_id
        if spec.fork_session:
            env[agentenv.FORK_SESSION] = "1"
    if spec.max_budget_usd is not None:
        env[agentenv.MAX_BUDGET_USD] = str(spec.max_budget_usd)
    if spec.max_turns is not None:
        env[agentenv.MAX_TURNS] = str(spec.max_turns)
    if spec.model:
        env[agentenv.MODEL] = spec.model
    if spec.traceparent:
        env[agentenv.TRACEPARENT] = spec.traceparent
    if spec.github_token:
        env[agentenv.GITHUB_TOKEN] = spec.github_token
        env[agentenv.GH_TOKEN] = spec.github_token  # the gh CLI reads this one
    if spec.git_user_name:
        env[agentenv.GIT_USER_NAME] = spec.git_user_name
    if spec.git_user_email:
        env[agentenv.GIT_USER_EMAIL] = spec.git_user_email
    # Exactly one Anthropic credential enters the box, subscription first —
    # the CLI prefers ANTHROPIC_API_KEY over the OAuth token, so passing
    # both would silently switch billing to the API. The pool's pick wins over
    # this machine's own credential when there is one (F2): a burst stamped with
    # a member's subscription has to actually run on it, or the attribution is
    # a fiction. No pick — an empty pool — is the machine's, as before.
    if spec.command == "run":
        oauth = spec.oauth_token or os.environ.get(agentenv.CLAUDE_CODE_OAUTH_TOKEN)
        api_key = None if spec.oauth_token else os.environ.get(agentenv.ANTHROPIC_API_KEY)
        if oauth:
            env[agentenv.CLAUDE_CODE_OAUTH_TOKEN] = oauth
        elif api_key:
            env[agentenv.ANTHROPIC_API_KEY] = api_key
        # No single command may hang the burst (H2): the CLI's own per-call
        # ceiling, set here because the policy is the platform's. Never on a
        # `check`, which runs no tools at all.
        env[agentenv.BASH_DEFAULT_TIMEOUT_MS] = str(int(BASH_CALL_TIMEOUT_S * 1000))
        env[agentenv.BASH_MAX_TIMEOUT_MS] = str(int(BASH_CALL_CEILING_S * 1000))
        # The agent's own OTel (B2) — empty unless a collector is configured, and
        # never on a `check`, which is the no-network boot smoke.
        env.update(agent_telemetry_env(spec.task_id, spec.burst_id, spec.role, spec.kind))
    env.update(spec.extra_env)
    return env


class HostPhases:
    """`materialize` and `collect` on this machine's filesystem — the two bytes
    phases as every runner that shares a filesystem with the control plane does
    them (G1).

    `DockerRunner` mounts the volume this writes; the suite's fakes in docker's
    seat inherit the same two, because a fake that ran a burst has to leave the
    same trail a real one does. A Job pod cannot inherit it: there the phases are
    an init container and a harvester, talking to the archive through presigned
    URLs minted at spawn (G2), which is exactly why they are phases of the
    *runner* and not steps in `execute_burst`.

    The archive is the runner's, not the burst's: nothing that runs beside a
    burst is handed storage credentials, then or now.
    """

    archive: SessionArchive | None = None
    # The bytes phases run here, in the control plane's own process, on the
    # control plane's own filesystem. `JobsRunner` says False and the difference
    # is visible to the caller exactly once: a burst whose phases run beside it
    # has to be *told* what to clone, because there is no mount to inherit.
    pod_side = False

    def materialize(self, spec: BurstSpec, parent: Snapshot | None = None) -> None:
        materialize_volume(spec.volume_dir, self.archive, parent)

    def collect(self, spec: BurstSpec) -> BurstArtifacts:
        return collect_volume(spec.volume_dir, self.archive, spec.archive_as)


class Runner(Protocol):
    """Where bursts run — the seam `DockerRunner` and `JobsRunner` sit behind
    (E4/G3, TO-BE §6); the suite's fakes take docker's seat through it.

    Four questions since G1, and what every answer holds wherever the burst runs.
    The first and the third are the bytes phases — one volume in, one volume out
    — and they are the runner's because *where* those bytes live is: a directory
    on this host, or an emptyDir a pod's init container fills from a presigned
    URL. Between them nothing about the burst changes.

    - ``materialize`` makes the burst's volume and fills it from ``parent`` —
      the snapshot the ledger named as the leaf of the task's session tree, or
      None for a burst that starts empty. A checksum mismatch raises rather
      than handing the burst bytes nobody recorded.
    - ``collect`` reads back what the burst left — its result file and its
      violations feed — and archives the volume under ``spec.archive_as``, or
      under nothing when that is None. Bytes only: turning them into ledger
      rows is `fold_artifacts`, in the control plane, because a burst's
      substrate holds no ledger credentials.

    And the two E4 already had:

    - ``run`` starts the burst ``spec`` describes and returns its exit code —
      the burst's own, never a guess. Output streams to this process's stdout
      and, when ``log_path`` is given, is teed there too (the file catches a
      burst that dies before writing any result; the runner makes the
      directory). ``heartbeat`` is asked at least every ``HEARTBEAT_POLL_S``
      while the burst runs, output or none, and the first False **fences** it:
      the runner ends the burst, and ``run`` still returns what the burst
      exited with. The caller remembers the answer (`execute_burst` records a
      fenced burst ``killed``, E3); the runner only acts on it.
    - ``alive`` is the reconciler's question: is this burst still running where
      this runner can see? True is proof of life that outranks a lapsed lease
      (the heartbeat failed, the work did not); False for a burst that has
      ended or that this runner never ran.

    ``pod_side`` says which of the two shapes this runner is: False when the
    phases run in the control plane's own process (a mount away from the
    burst), True when they run beside the burst and everything they need has to
    be *given* to them. It is the one thing about a substrate the caller has to
    know, and it buys exactly one decision — whether the burst is told what to
    clone (`execute_burst`), since a pod inherits no mount.

    `tests/test_runner_contract.py` is the contract, one suite parameterized over
    every runner there is — a test that passes on one and fails on another is a
    bug by definition, the way the ledger's and the archive's suites put it.
    """

    pod_side: bool

    def materialize(self, spec: BurstSpec, parent: Snapshot | None = None) -> None: ...

    def run(
        self, spec: BurstSpec, log_path: Path | None = None, heartbeat: Heartbeat | None = None
    ) -> int: ...

    def collect(self, spec: BurstSpec) -> BurstArtifacts: ...

    def alive(self, burst_id: str) -> bool: ...


class DockerRunner(HostPhases):
    """Bursts as containers on this host, through the ``docker`` CLI.

    The CLI is spoken in exactly three places — `build_command` (``docker run``),
    `kill` (``docker kill``, the fence) and `alive` (``docker ps``) — which is
    what lets the contract suite run this class with a shell in the daemon's
    seat: those three replaced, the pump, the tee, the clock and the fence are
    the real thing. The bytes phases are `HostPhases`': same filesystem as the
    control plane, so a directory is all they need.
    """

    def __init__(self, archive: SessionArchive | None = None) -> None:
        self.archive = archive

    def build_command(self, spec: BurstSpec) -> list[str]:
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            f"burst-{spec.burst_id}",
            "--security-opt",
            f"seccomp={SECCOMP_PROFILE}",
            "-v",
            f"{spec.volume_dir}:{agentenv.CONTAINER_SESSION_DIR}",
            "-v",
            f"{spec.repo_mount}:{agentenv.CONTAINER_REPO_DIR}{':ro' if spec.repo_read_only else ''}",
        ]
        if spec.codex_auth_dir and spec.command == "run":
            cmd += ["-v", f"{spec.codex_auth_dir}:/home/agent/.codex"]
        if spec.runtime_class:
            cmd += ["--runtime", spec.runtime_class]
        for key, value in burst_env(spec).items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [spec.image, spec.command]
        return cmd

    def run(
        self, spec: BurstSpec, log_path: Path | None = None, heartbeat: Heartbeat | None = None
    ) -> int:
        """Run the burst; when log_path is set, tee container output there.

        The transcript on the volume is the deep record, but it only exists
        once the SDK is up — the log file catches everything, including a
        container that dies before writing any result.

        ``heartbeat`` (E1) is asked every ``HEARTBEAT_POLL_S`` while the
        container runs, output or none; a False answer kills the container — the
        lease behind this burst is gone, and so is its right to keep working.
        """
        cmd = self.build_command(spec)
        if log_path is None and heartbeat is None:
            return subprocess.run(cmd, check=False).returncode
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        sink = log_path.open("w", encoding="utf-8") if log_path is not None else None
        try:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
                assert proc.stdout is not None
                for line in _pump(proc.stdout, heartbeat, fence=lambda: self.kill(spec.burst_id)):
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    if sink is not None:
                        sink.write(line)
        finally:
            if sink is not None:
                sink.close()
        return proc.returncode

    def kill(self, burst_id: str) -> None:
        """Stop a burst's container by the name every burst carries."""
        subprocess.run(
            ["docker", "kill", f"burst-{burst_id}"], check=False, capture_output=True
        )

    def alive(self, burst_id: str) -> bool:
        """``docker ps`` by that name: the container is running on *this* host.
        A peer's burst on another host is invisible here, and the reconciler
        does not ask about it — a live lease is that burst's proof of life."""
        out = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name=burst-{burst_id}"],
            capture_output=True,
            text=True,
        )
        return bool(out.stdout.strip())


def open_runner(archive: SessionArchive | None = None) -> Runner:
    """The runner this deployment has — `open_ledger` / `open_archive`'s third.

    One presence toggle, like the other two: `BURST_NAMESPACE` set means this
    deployment runs bursts as k8s Jobs in that namespace (G3), unset means
    containers on this host's docker. Nothing downstream of this line names a
    substrate. The archive is handed in because the bytes phases are the
    runner's (G1): `DockerRunner` moves the bytes itself, `JobsRunner` mints
    the URLs a pod moves them with (G2), and the caller cannot tell which.
    """
    namespace = os.environ.get(NAMESPACE_ENV, "").strip()
    if not namespace:
        return DockerRunner(archive)
    from control_plane.jobs import JobsRunner  # local: keeps kubectl off every CLI verb

    if archive is None:
        raise RunnerError(
            f"{NAMESPACE_ENV} runs bursts on a cluster, where the phases reach the archive "
            "through presigned URLs — so this deployment needs an archive to mint them from"
        )
    log.info("bursts run as k8s Jobs", namespace=namespace)
    return JobsRunner(archive, namespace=namespace)


def _pump(
    stream, heartbeat: Heartbeat | None, fence: Callable[[], None]
) -> Iterator[str]:
    """Lines from ``stream`` as they arrive, with the heartbeat asked on the
    clock rather than on output.

    A plain ``for line in stream`` blocks until the container prints, which is
    exactly when a lease must *not* depend on it. So the wait is a ``select``
    with a deadline: output or not, every ``HEARTBEAT_POLL_S`` the callback is
    asked, and the first False fences the burst — once; the stream is then
    drained to EOF so the exit code is the container's own. Raw ``os.read`` on
    the fd, because a buffered reader in front of ``select`` can hold bytes the
    selector cannot see.
    """
    fd = stream.fileno()
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    next_beat = time.monotonic() + HEARTBEAT_POLL_S
    fenced = False
    try:
        while True:
            timeout: float | None = None
            if heartbeat is not None:
                now = time.monotonic()
                if now >= next_beat:
                    if not fenced and not heartbeat():
                        log.error("lease lost mid-burst — killing the container")
                        fence()
                        fenced = True
                    next_beat = now + HEARTBEAT_POLL_S
                timeout = max(0.0, next_beat - now)
            if not selector.select(timeout):
                continue  # the deadline, not data — loop back to the heartbeat
            chunk = os.read(fd, 65536)
            if not chunk:
                pending += decoder.decode(b"", final=True)
                if pending:
                    yield pending
                return
            pending += decoder.decode(chunk)
            *lines, pending = pending.split("\n")
            for line in lines:
                yield line + "\n"
    finally:
        selector.close()
