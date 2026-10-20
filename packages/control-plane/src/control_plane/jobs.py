"""Bursts as k8s Jobs (G3, #44, TO-BE §6).

One burst is one Job, and one Job is one pod of three containers: **init**
fills an `emptyDir` from a presigned GET and clones the product into a second
one; **agent** — the dev-agent image, unchanged — runs against both; **harvest**
waits for the agent to exit and ships what it left to two presigned PUTs. The
pod then dies with everything it held, which is the point: nothing that ran
beside the burst survives it, and nothing it held was a credential.

What the pod is *not* given is the security story, and it is all absence:

- `automountServiceAccountToken: false` — the burst holds no k8s credential at
  all, so "a burst cannot touch the cluster" is a fact about the pod rather
  than a promise about a NetworkPolicy. The k8s client lives on this side of
  the seam and nowhere else.
- no storage credential — the phases hold presigned URLs for exactly the keys
  this burst may read and write, minted at spawn and dead before the next burst
  (`control_plane.phases`, G2).
- no GitHub credential in a reviewer or tester agent — the clone token is
  `contents:read` on one repository and appears in the init container's
  environment only.
- `backoffLimit: 0` and `restartPolicy: Never` — a burst that failed is a fact
  the scheduler decides about, with the result file in hand. Retrying it here
  would be a second scheduler, one that cannot read anything.

**Why kubectl and not a k8s client library.** For the same reason `DockerRunner`
shells out to `docker`: the streaming half of `run` — output pumped line by
line, the heartbeat asked on the clock whether or not a line arrives, the first
False fencing the burst — is subtle, tested, and already written. Against a
subprocess it is *the same code* (`_pump`), so the two runners cannot drift on
the guarantee that matters most. The cost is one binary in the image; the
alternative was a second implementation of the pump against a socket.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog
from dev_agent import env as agentenv

from control_plane import runner as runner_mod
from control_plane.egress import EGRESS_ANNOTATION, as_json, declaration
from control_plane.phases import (
    PHASE_URL_TTL_S,
    PhaseEnv,
    artifacts_key,
    mint_phase_env,
    read_artifacts,
)
from control_plane.runner import (
    BurstSpec,
    Heartbeat,
    RunnerError,
    _pump,
    burst_env,
)
from control_plane.store import SessionArchive, session_key
from control_plane.volume import BurstArtifacts, Snapshot

log = structlog.get_logger()

# The image the two phase containers run. The agent's is the burst's own
# (`BurstSpec.image`); this one is the platform's, and the two are never the
# same image — that separation is what makes "the burst cannot import the
# control plane" checkable.
DEFAULT_PHASES_IMAGE = "usf/burst-phases:latest"

# A burst's images are pinned by the deployment — the agent's by the spec, the
# phases' by this module — so the tag is the decision and re-pulling it every
# burst is not. `IfNotPresent` also means a node can hold an image nobody can
# pull: a cluster with no registry (kind, an air-gapped node) is a first-class
# place to run bursts, and it is where the contract suite runs.
IMAGE_PULL_POLICY = "IfNotPresent"

# The wall-clock rail, as k8s enforces it. It is `PHASE_URL_TTL_S` on purpose:
# the capabilities the pod holds have to outlive the burst and die with it, so
# "how long may a burst run" and "how long is its GET good for" are one number,
# not two that can drift apart.
BURST_DEADLINE_S = PHASE_URL_TTL_S

# How long a finished Job's record sticks around for a human to look at. The
# ledger is the record; this is the debugging window.
TTL_AFTER_FINISHED_S = 3600

# How long to wait for a pod to reach the agent container. Image pulls and a
# snapshot download happen first, and none of it is this runner's to hurry.
POD_START_TIMEOUT_S = 600.0

# How long to wait for the harvester after the agent has exited. The snapshot is
# megabytes and the PUT is the only way the ledger hears about this burst.
HARVEST_TIMEOUT_S = 600.0

# How often the cluster is asked anything while waiting. The heartbeat's clock
# is `runner.HEARTBEAT_POLL_S` and is read through the module, so the suite can
# speed it up the way it does for `DockerRunner`.
POLL_S = 0.5

# Where the agent's exit code lands for the harvester to see. Its own volume,
# deliberately: the session volume is archived, and a sentinel the pod invented
# has no business in a snapshot the next burst resumes from.
SIGNAL_DIR = "/signal"
AGENT_EXIT_FILE = f"{SIGNAL_DIR}/agent-exit"

# Both images run as this uid; an `emptyDir` is root-owned until a pod asks
# otherwise, so without this the burst cannot write its own volume.
POD_UID = 1000

# The clone's volume is mounted one level *above* the working tree, so that the
# tree itself is a directory the clone creates and therefore owns. A mount point
# stays root-owned however the pod's `fsGroup` is set, and git refuses to work in
# a repository owned by somebody else ("dubious ownership") — which would make
# every burst on a cluster start by arguing with its own checkout.
#
# The other half of that arrangement is in the agent image: it must not declare a
# `VOLUME` for the working tree. containerd honours image volumes by mounting an
# empty ephemeral directory at that path, which would shadow the clone this pod
# just made — a thing that is invisible under docker, where the bind mount lands
# on the same path and wins.
CLONE_PARENT = str(Path(agentenv.CONTAINER_REPO_DIR).parent)

# What a fenced burst's exit code is when the pod is gone before it can be read
# — the same number a SIGKILLed container reports, for the same reason.
FENCED_EXIT_CODE = 137


@dataclass(frozen=True, slots=True)
class _Pending:
    """What `materialize` minted and `run` and `collect` then need."""

    env: PhaseEnv


class JobsRunner:
    """Bursts as Jobs in one namespace. `DockerRunner`'s peer behind `Runner`."""

    # the phases run beside the burst, so the burst has to be told what to clone
    pod_side = True

    def __init__(
        self,
        archive: SessionArchive,
        *,
        namespace: str,
        phases_image: str = DEFAULT_PHASES_IMAGE,
        deadline_s: float = BURST_DEADLINE_S,
        kubectl: tuple[str, ...] = ("kubectl",),
    ) -> None:
        self.archive = archive
        self.namespace = namespace
        self.phases_image = phases_image
        self.deadline_s = deadline_s
        self.kubectl = kubectl
        self._pending: dict[str, _Pending] = {}

    # --- phase one: the capabilities the pod moves bytes with (G1/G2) ---

    def materialize(self, spec: BurstSpec, parent: Snapshot | None = None) -> None:
        """On a cluster the bytes move *inside* the pod, so this phase mints what
        moves them: a GET for the parent snapshot, PUTs for this burst's two
        keys, and the clone's read token. Nothing is fetched here — there is no
        filesystem on this side to fetch it onto.

        The checksum still travels with the GET, and the init container makes the
        same refusal `materialize_volume` makes: a burst is never handed bytes
        nobody recorded, wherever the bytes land.
        """
        clone = None
        if spec.clone is not None:
            url, ref = spec.clone
            clone = (url, ref, agentenv.CONTAINER_REPO_DIR, spec.clone_token or "")
        minted = mint_phase_env(
            self.archive,
            volume_dir=agentenv.CONTAINER_SESSION_DIR,
            # every burst reports, and only some archive: the record goes under
            # this burst's own prefix whether or not its volume is kept
            record_key=artifacts_key(session_key(spec.task_id, spec.burst_id)),
            parent=parent,
            session_key=spec.archive_as,
            clone=clone,
            ttl_s=self.deadline_s,
        )
        self._pending[spec.burst_id] = _Pending(env=minted)

    # --- phase two: the burst ---

    def agent_script(self, spec: BurstSpec) -> str:
        """The command the agent container runs, as one shell line.

        The image's own entrypoint, spelled out — because the container's command
        has to end by recording the agent's exit code where the harvester can see
        it, and a pod has no post-stop hook to do that in. The suite overrides
        this the way it replaces `DockerRunner.build_command`.
        """
        return f"dev-agent {spec.command}"

    def job_manifest(self, spec: BurstSpec, phase_env: PhaseEnv) -> dict:
        """The Job, as a dict. A pure function of the spec and the minted
        capabilities, so the walls in it can be read off in a test without a
        cluster (`tests/test_jobs_runner.py`)."""
        session = {"name": "session", "mountPath": agentenv.CONTAINER_SESSION_DIR}
        repo = {"name": "repo", "mountPath": CLONE_PARENT}
        # what this pod may reach, declared as data (G6): enforcement is the
        # adopter's CNI, and this is what its policy is generated from
        urls = [v for v in (*phase_env.init.values(), *phase_env.harvest.values()) if v.startswith("http")]
        egress_annotation = {EGRESS_ANNOTATION: json.dumps(as_json(declaration(spec, urls)))}
        signal = {"name": "signal", "mountPath": SIGNAL_DIR}
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name(spec.burst_id),
                "namespace": self.namespace,
                "labels": {"app": "burst", "burst": spec.burst_id, "role": spec.role},
                # the task id carries '/' and '#', which no label value may — and
                # it is the one thing a human looking at a stuck pod wants
                "annotations": {"usf/task": spec.task_id, **egress_annotation},
            },
            "spec": {
                # one burst, one attempt: a retry is the scheduler's decision,
                # made with the result file in hand (TO-BE §6)
                "backoffLimit": 0,
                "activeDeadlineSeconds": int(self.deadline_s),
                "ttlSecondsAfterFinished": TTL_AFTER_FINISHED_S,
                "template": {
                    # on the pod too: a policy engine admits pods, not Jobs
                    "metadata": {
                        "labels": {"app": "burst", "burst": spec.burst_id, "role": spec.role},
                        "annotations": egress_annotation,
                    },
                    "spec": {
                        **({"runtimeClassName": spec.runtime_class} if spec.runtime_class else {}),
                        "restartPolicy": "Never",
                        # the burst holds no k8s credential at all
                        "automountServiceAccountToken": False,
                        "securityContext": {"fsGroup": POD_UID},
                        "initContainers": [
                            {
                                "name": "init",
                                "image": self.phases_image,
                                "imagePullPolicy": IMAGE_PULL_POLICY,
                                "command": ["burst-init"],
                                "env": _env_list(phase_env.init),
                                "volumeMounts": [session, repo],
                            }
                        ],
                        "containers": [
                            {
                                "name": "agent",
                                "image": spec.image,
                                "imagePullPolicy": IMAGE_PULL_POLICY,
                                # a trap, not a trailing line: the burst's own
                                # command may end in `exec` or `exit`, and the
                                # harvester has to hear about it either way. What
                                # nothing can catch is a SIGKILL — and there the
                                # pod is going away regardless.
                                "command": [
                                    "sh",
                                    "-c",
                                    f"trap 'echo $? > {AGENT_EXIT_FILE}' EXIT; "
                                    "trap 'exit 143' TERM; trap 'exit 130' INT; "
                                    f"{self.agent_script(spec)}",
                                ],
                                # exactly what every other runner hands the box,
                                # and nothing else: no URL, no storage key, no
                                # clone token
                                "env": _env_list(burst_env(spec)),
                                "volumeMounts": [session, repo, signal],
                            },
                            {
                                "name": "harvest",
                                "image": self.phases_image,
                                "imagePullPolicy": IMAGE_PULL_POLICY,
                                # a pod has no "after the main container" hook, so
                                # the harvester waits for the exit code the agent
                                # writes and then ships what is on the volume
                                "command": [
                                    "sh",
                                    "-c",
                                    f"while [ ! -f {AGENT_EXIT_FILE} ]; do sleep 1; done; "
                                    "exec burst-harvest",
                                ],
                                "env": _env_list(phase_env.harvest),
                                "volumeMounts": [session, signal],
                            },
                        ],
                        "volumes": [
                            {"name": "session", "emptyDir": {}},
                            {"name": "repo", "emptyDir": {}},
                            {"name": "signal", "emptyDir": {}},
                        ],
                    },
                },
            },
        }

    def run(
        self, spec: BurstSpec, log_path: Path | None = None, heartbeat: Heartbeat | None = None
    ) -> int:
        """Create the Job, stream the agent's output, return the agent's exit code.

        Three waits, and the heartbeat is asked through all of the first two: for
        the pod to reach the agent container (an image pull and a snapshot
        download happen first, and a burst is not dead because it is still
        starting), for the agent's output, and — once the agent has exited — for
        the harvester to finish. Nothing is fenced during that last one: the
        burst is over, and killing the pod then would throw away the only copy
        of what it did.
        """
        pending = self._pending.get(spec.burst_id)
        if pending is None:
            raise RunnerError(
                f"burst {spec.burst_id} was never materialized — a Job's capabilities are "
                "minted by that phase, and a pod cannot be given what nobody minted"
            )
        manifest = self.job_manifest(spec, pending.env)
        # the manifest carries presigned URLs and the clone token; it is written
        # to kubectl's stdin and to no log, here or in a failure
        self._kubectl("create", "-f", "-", stdin=json.dumps(manifest))

        fenced = False

        def fence() -> None:
            nonlocal fenced
            fenced = True
            self.kill(spec.burst_id)

        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        sink = log_path.open("w", encoding="utf-8") if log_path is not None else None
        try:
            # the wait is part of the burst: a pod pulling an image or unpacking a
            # snapshot has printed nothing yet and is not dead, so the heartbeat is
            # asked through it and a False here fences a burst that never spoke
            pod = self._wait_for_agent(spec.burst_id, heartbeat, fence)
            if pod is None:
                return FENCED_EXIT_CODE if fenced else self._exit_code(spec.burst_id)
            with subprocess.Popen(
                [*self.kubectl, "-n", self.namespace, "logs", "-f", pod, "-c", "agent"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ) as proc:
                assert proc.stdout is not None
                for line in _pump(proc.stdout, heartbeat, fence=fence):
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    if sink is not None:
                        sink.write(line)
        finally:
            if sink is not None:
                sink.close()

        if fenced:
            return FENCED_EXIT_CODE
        self._wait_for_harvest(spec.burst_id)
        return self._exit_code(spec.burst_id)

    def kill(self, burst_id: str) -> None:
        """The fence: the Job goes, and the pod with it."""
        self._kubectl(
            "delete", "job", job_name(burst_id),
            "--grace-period=0", "--wait=false", "--ignore-not-found",
            check=False,
        )

    def alive(self, burst_id: str) -> bool:
        """Is this burst still running where this runner can see?

        Two ends are trimmed off the Job's own lifetime, in opposite directions.
        A pod that is pulling an image or unpacking a snapshot **is** this burst,
        and answering False there would let a reconciler absorb a task that is
        merely starting. And a Job outlives its agent by however long the
        harvester takes, so once the agent container has terminated the burst is
        over whatever the Job says.
        """
        pod = self._pod(burst_id)
        if pod is None or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            return False
        state = self._agent_state(pod)
        return state is None or "terminated" not in state

    def timings(self, burst_id: str) -> dict[str, float] | None:
        """How long each part of the pod took, in seconds, read off its status —
        what the golden-replay harness reports per runtime (G4). Asked after
        `run`, while the finished pod is still kept (`TTL_AFTER_FINISHED_S`).

        ``boot_s`` is the bake-off's first criterion: the pod's creation to the
        agent container's start — scheduling, the sandbox's own boot, image
        unpack and the init phase together, which is what a burst waits for.
        ``init_s``, ``agent_s`` and ``harvest_s`` split it. The cluster stamps
        whole seconds, so these are too. None: the pod is gone or never ran.
        """
        pod = self._pod(burst_id)
        if pod is None:
            return None
        status = pod.get("status", {})
        created = _stamp(pod.get("metadata", {}).get("creationTimestamp"))
        init = _terminated(status.get("initContainerStatuses"), "init")
        agent = _terminated(status.get("containerStatuses"), "agent")
        harvest = _terminated(status.get("containerStatuses"), "harvest")
        out: dict[str, float] = {}
        if created is not None and agent:
            out["boot_s"] = agent[0] - created
        for name, span in (("init_s", init), ("agent_s", agent), ("harvest_s", harvest)):
            if span:
                out[name] = span[1] - span[0]
        return out or None

    # --- phase three: what the harvester shipped ---

    def collect(self, spec: BurstSpec) -> BurstArtifacts:
        """Read the record the harvester PUT, as the shape a host phase returns.

        The volume itself is gone — it died with the pod — so this is the whole
        of what the burst left. A missing record is a refusal, not an empty
        burst: it means the harvester never got to run, and calling that "the
        burst wrote nothing" would launder a broken pod into a clean result.
        """
        pending = self._pending.pop(spec.burst_id, None)
        if pending is None:
            raise RunnerError(f"burst {spec.burst_id} was never materialized — nothing to collect")
        return read_artifacts(self.archive, pending.env.record)

    # --- the cluster, spoken in one place ---

    def _kubectl(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        result = subprocess.run(
            [*self.kubectl, "-n", self.namespace, *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise RunnerError(f"kubectl {args[0]} failed: {_scrub(result.stderr)}")
        return result.stdout

    def _pod(self, burst_id: str) -> dict | None:
        out = self._kubectl(
            "get", "pods", "-l", f"burst={burst_id}", "-o", "json", check=False
        )
        try:
            items = json.loads(out).get("items", [])
        except json.JSONDecodeError:
            return None
        return items[0] if items else None

    @staticmethod
    def _agent_state(pod: dict | None) -> dict | None:
        if pod is None:
            return None
        for status in pod.get("status", {}).get("containerStatuses", []) or []:
            if status.get("name") == "agent":
                return status.get("state") or {}
        return None

    def _wait_for_agent(
        self, burst_id: str, heartbeat: Heartbeat | None, fence: object
    ) -> str | None:
        """The pod's name, once its agent container has something to say — or
        None when the pod failed before the agent ran (a refused snapshot, a
        clone that would not authenticate) or the heartbeat fenced it."""
        deadline = time.monotonic() + POD_START_TIMEOUT_S
        next_beat = time.monotonic() + runner_mod.HEARTBEAT_POLL_S
        while time.monotonic() < deadline:
            pod = self._pod(burst_id)
            state = self._agent_state(pod)
            if state is not None and ("running" in state or "terminated" in state):
                return pod["metadata"]["name"]
            if pod is not None and pod.get("status", {}).get("phase") in ("Failed", "Succeeded"):
                return None  # it ended without the agent ever starting
            if heartbeat is not None and time.monotonic() >= next_beat:
                if not heartbeat():
                    log.error("lease lost before the burst started — deleting the job")
                    fence()  # type: ignore[operator]
                    return None
                next_beat = time.monotonic() + runner_mod.HEARTBEAT_POLL_S
            time.sleep(POLL_S)
        raise RunnerError(f"burst {burst_id} never reached its agent container")

    def _wait_for_harvest(self, burst_id: str) -> None:
        """Until the pod is done — the agent's exit is not the burst's end, the
        harvester's PUT is. Not fatal on timeout: `collect` is what decides
        whether the record is there, and its refusal is the honest one."""
        # never longer than the burst's own rail: the pod cannot outlive it, so
        # a wait that did would only be this process staring at a deleted pod
        deadline = time.monotonic() + min(HARVEST_TIMEOUT_S, self.deadline_s)
        while time.monotonic() < deadline:
            pod = self._pod(burst_id)
            if pod is None or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                return
            time.sleep(POLL_S)
        log.warning("the harvester is still running past its timeout", burst=burst_id)

    def _exit_code(self, burst_id: str) -> int:
        """The agent container's own exit code — never a guess. A pod that is
        already gone leaves none, and a burst whose exit nobody can read did not
        succeed, so the fenced code stands in."""
        state = self._agent_state(self._pod(burst_id)) or {}
        terminated = state.get("terminated")
        if terminated is None or "exitCode" not in terminated:
            return FENCED_EXIT_CODE
        return int(terminated["exitCode"])


def _stamp(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _terminated(statuses: list[dict] | None, name: str) -> tuple[float, float] | None:
    """``(started, finished)`` of a container that has ended, or None."""
    for status in statuses or []:
        if status.get("name") == name:
            ended = (status.get("state") or {}).get("terminated") or {}
            started, finished = _stamp(ended.get("startedAt")), _stamp(ended.get("finishedAt"))
            if started is not None and finished is not None:
                return started, finished
    return None


def job_name(burst_id: str) -> str:
    """One name per burst, the way a container is `burst-<id>` on docker."""
    return f"burst-{burst_id}"


def _env_list(env: dict[str, str]) -> list[dict[str, str]]:
    """A pod's env is a list of pairs; every runner's is the same mapping."""
    return [{"name": name, "value": value} for name, value in env.items()]


def _scrub(message: str) -> str:
    """kubectl's complaint, with anything that could be the manifest removed.

    A Job's spec holds presigned URLs and a clone token, and an API server that
    rejects one may quote the field it rejected. What reaches a log here is the
    first line, minus every word long enough or URL-shaped enough to be a
    capability — the same rule the pool's refusals follow (F1).
    """
    first = (message or "").strip().splitlines()[:1]
    words = (first[0] if first else "").split()
    kept = [w for w in words if len(w) <= 40 and "http" not in w.lower()]
    return " ".join(kept)[:300] or "no detail"
