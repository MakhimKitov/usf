"""What the pod is, and what the pod is not (G3, #44).

`JobsRunner`'s behaviour under a real cluster is `test_runner_contract.py`'s —
it is a `Runner` like any other and the contract does not care where it runs.
What is left for here is the manifest: the walls the pod spec *is*, readable off
a dict without a cluster, plus one live burst that looks for the credential the
spec says it does not have. A wall nobody provokes is a claim.
"""

import json
from pathlib import Path

import pytest
from control_plane.jobs import (
    AGENT_EXIT_FILE,
    CLONE_PARENT,
    DEFAULT_PHASES_IMAGE,
    JobsRunner,
    job_name,
)
from control_plane.phases import artifacts_key
from control_plane.runner import BurstSpec, RunnerError
from control_plane.store import ArchiveError, LocalSessionArchive, session_key
from control_plane.volume import Snapshot
from dev_agent import env as agentenv
from jobs_cluster import JobsRig

TASK = "owner/product#42"
BURST = "b1"
NAMESPACE = "bursts"


class Presigning(LocalSessionArchive):
    """A local archive that hands out URLs, which the real local one refuses to
    do — the manifest is what is under test, not the store."""

    def presign_get(self, key: str, expires_in: int) -> str:
        return f"https://store.example/{key}?op=get&X-Amz-Expires={expires_in}&sig=abc"

    def presign_put(self, key: str, expires_in: int) -> str:
        return f"https://store.example/{key}?op=put&X-Amz-Expires={expires_in}&sig=abc"


@pytest.fixture
def runner(tmp_path):
    return JobsRunner(Presigning(tmp_path / "archive"), namespace=NAMESPACE, deadline_s=1800)


def _spec(**overrides) -> BurstSpec:
    defaults = dict(
        task_id=TASK,
        burst_id=BURST,
        volume_dir=Path("/data/volumes/b1"),
        repo_mount=Path("/clones/product"),
        archive_as=session_key(TASK, BURST),
        clone=("https://github.com/owner/product.git", "main"),
        clone_token="ghs-clone",
    )
    return BurstSpec(**(defaults | overrides))


def manifest(runner: JobsRunner, spec: BurstSpec, parent: Snapshot | None = None) -> dict:
    runner.materialize(spec, parent)
    return runner.job_manifest(spec, runner._pending[spec.burst_id].env)


def container(job: dict, name: str) -> dict:
    pod = job["spec"]["template"]["spec"]
    for candidate in [*pod["initContainers"], *pod["containers"]]:
        if candidate["name"] == name:
            return candidate
    raise AssertionError(f"no container named {name!r}")


def env_of(job: dict, name: str) -> dict[str, str]:
    return {pair["name"]: pair["value"] for pair in container(job, name)["env"]}


# --- the absences ---


def test_the_pod_holds_no_kubernetes_credential(runner):
    """"A burst can't touch the cluster" is a fact about the pod, not a promise
    about a NetworkPolicy: there is no token in it to use."""
    job = manifest(runner, _spec())
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_the_agent_container_holds_no_capability_and_no_clone_token(runner):
    """Everything the phases were given is a capability or a credential, and the
    agent gets none of it: it is handed `burst_env` and nothing else."""
    job = manifest(runner, _spec(), parent=Snapshot("sessions/owner/product#42/b0/session.tar.gz", "a" * 64))
    agent = env_of(job, "agent")
    assert not any(value.startswith("http") for value in agent.values())
    assert not any(name.startswith("BURST_") for name in agent)
    assert "ghs-clone" not in json.dumps(agent)


def test_the_clone_token_is_the_init_containers_alone(runner):
    """A reviewer or tester agent gets a working tree and still holds no GitHub
    credential — the token lives in a container that is dead before the agent's
    first turn (TO-BE §6)."""
    job = manifest(runner, _spec())
    assert env_of(job, "init")["BURST_CLONE_TOKEN"] == "ghs-clone"
    assert "ghs-clone" not in json.dumps(env_of(job, "harvest"))
    assert "ghs-clone" not in json.dumps(env_of(job, "agent"))


def test_each_phase_gets_only_the_urls_it_needs(runner):
    """One capability per object, and the two phases do not share: init reads,
    the harvester writes."""
    parent = Snapshot("sessions/owner/product#42/b0/session.tar.gz", "a" * 64)
    job = manifest(runner, _spec(), parent=parent)
    init, harvest = env_of(job, "init"), env_of(job, "harvest")
    assert parent.key in init["BURST_SESSION_GET_URL"]
    assert "BURST_SESSION_PUT_URL" not in init and "BURST_ARTIFACTS_PUT_URL" not in init
    assert "BURST_SESSION_GET_URL" not in harvest
    assert session_key(TASK, BURST) in harvest["BURST_SESSION_PUT_URL"]
    # and the record goes under *this* burst's prefix, never its parent's
    assert artifacts_key(session_key(TASK, BURST)) in harvest["BURST_ARTIFACTS_PUT_URL"]


def test_a_burst_that_archives_nothing_still_reports(runner):
    """The reviewer (B5): no snapshot to write, and the result still has to come
    home — which is why the record's key is the burst's own and not the
    snapshot's."""
    job = manifest(runner, _spec(archive_as=None))
    harvest = env_of(job, "harvest")
    assert "BURST_SESSION_PUT_URL" not in harvest
    assert artifacts_key(session_key(TASK, BURST)) in harvest["BURST_ARTIFACTS_PUT_URL"]


# --- the rails ---


def test_one_burst_is_one_attempt(runner):
    """A retry is the scheduler's decision, made with the result file in hand.
    k8s retrying here would be a second scheduler, one that cannot read."""
    job = manifest(runner, _spec())
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_the_wall_clock_rail_is_the_jobs_deadline_and_the_capabilities_share_it(runner):
    """One number: how long a burst may run, and how long its URLs are good for.
    Two would drift, and the drift would be a pod holding a live capability."""
    job = manifest(runner, _spec())
    assert job["spec"]["activeDeadlineSeconds"] == 1800
    assert "X-Amz-Expires=1800" in env_of(job, "harvest")["BURST_ARTIFACTS_PUT_URL"]


def test_the_harvester_waits_for_the_agent_and_the_agent_says_when_it_is_done(runner):
    """A pod has no "after the main container" hook, so the two agree through a
    file on a volume of their own — never the session volume, which is archived
    and materialized into the next burst."""
    job = manifest(runner, _spec())
    assert AGENT_EXIT_FILE.startswith("/signal/")
    agent = " ".join(container(job, "agent")["command"])
    # a trap, so a burst whose command ends in `exit` still reports
    assert f"trap 'echo $? > {AGENT_EXIT_FILE}' EXIT" in agent
    assert f"while [ ! -f {AGENT_EXIT_FILE} ]" in " ".join(container(job, "harvest")["command"])
    mounts = {m["mountPath"] for m in container(job, "harvest")["volumeMounts"]}
    assert mounts == {agentenv.CONTAINER_SESSION_DIR, "/signal"}  # no clone for the harvester


def test_the_working_tree_is_a_directory_the_clone_owns(runner):
    """A mount point stays root-owned whatever `fsGroup` says, and git refuses to
    work in a repository owned by somebody else. So the volume is mounted one
    level up and the tree is the clone's own directory — otherwise every burst on
    a cluster would begin by arguing with its checkout."""
    job = manifest(runner, _spec())
    for name in ("init", "agent"):
        paths = {m["mountPath"] for m in container(job, name)["volumeMounts"]}
        assert CLONE_PARENT in paths and agentenv.CONTAINER_REPO_DIR not in paths
    assert env_of(job, "init")["BURST_CLONE_DIR"] == agentenv.CONTAINER_REPO_DIR


def test_the_phases_run_the_platforms_image_and_the_burst_runs_its_own(runner):
    """What the burst may import is checked by building two images; the pod is
    where that separation either holds or quietly stops mattering."""
    job = manifest(runner, _spec(image="usf/dev-agent:v9"))
    assert container(job, "agent")["image"] == "usf/dev-agent:v9"
    assert container(job, "init")["image"] == DEFAULT_PHASES_IMAGE
    assert container(job, "harvest")["image"] == DEFAULT_PHASES_IMAGE
    # and a node that already holds them does not go looking for a registry: a
    # cluster without one is a first-class place to run bursts
    assert all(
        container(job, name)["imagePullPolicy"] == "IfNotPresent"
        for name in ("init", "agent", "harvest")
    )


def test_the_job_is_named_after_the_burst_and_says_which_task(runner):
    job = manifest(runner, _spec())
    assert job["metadata"]["name"] == job_name(BURST) == "burst-b1"
    # a task id carries '/' and '#', which no label value may, and it is the one
    # thing a human looking at a stuck pod wants
    assert job["metadata"]["annotations"]["usf/task"] == TASK
    assert job["metadata"]["labels"]["burst"] == BURST


# --- the phases are a sequence, and skipping one is a refusal ---


def test_a_burst_that_was_never_materialized_cannot_run_or_collect(runner):
    spec = _spec()
    with pytest.raises(RunnerError, match="never materialized"):
        runner.run(spec)
    with pytest.raises(RunnerError, match="never materialized"):
        runner.collect(spec)


def test_a_missing_record_is_a_refusal_not_an_empty_burst(runner):
    """The volume died with the pod, so the record is the whole of what the burst
    left. Calling a missing one "the burst wrote nothing" would launder a broken
    harvest into a clean result."""
    spec = _spec()
    runner.materialize(spec, None)
    with pytest.raises(ArchiveError, match="left no record"):
        runner.collect(spec)


# --- and once, against a real pod ---


def test_the_pod_really_holds_no_service_account_token(tmp_path):
    """The manifest says the token is not mounted; this asks the burst.

    The burst is the adversary here: it looks where a service-account token
    would be — the path every in-cluster client reads — and reports what it
    found through the one channel it has. Finding nothing is the wall.
    """
    rig = JobsRig(tmp_path)
    try:
        spec = rig.burst(
            'saw=$(ls -A /var/run/secrets/kubernetes.io 2>/dev/null | tr "\n" " "); '
            '[ -n "$saw" ] || saw=nothing-there; '
            """printf '{"session_id": "s", "is_error": false, "saw": "%s"}' "$saw" """
            f'> "$CLAUDE_CONFIG_DIR/{agentenv.BURST_RESULT_FILENAME}"'
        )
        rig.runner.materialize(spec, None)
        assert rig.runner.run(spec) == 0
        assert rig.runner.collect(spec).result["saw"] == "nothing-there"
    finally:
        rig.teardown()


def test_the_runtime_class_is_the_pods_and_absent_by_default(runner):
    """G4's one knob: the golden replay runs a node under a chosen RuntimeClass,
    and a burst that names none gets the cluster's default — no field at all,
    rather than an empty one an API server would reject."""
    pod = manifest(runner, _spec())["spec"]["template"]["spec"]
    assert "runtimeClassName" not in pod
    pod = manifest(runner, _spec(burst_id="b2", runtime_class="gvisor"))["spec"]["template"]["spec"]
    assert pod["runtimeClassName"] == "gvisor"


def test_the_timings_are_read_off_the_pods_own_stamps(runner, monkeypatch):
    pod = {
        "metadata": {"creationTimestamp": "2026-10-01T10:00:00Z"},
        "status": {
            "initContainerStatuses": [{"name": "init", "state": {"terminated": {
                "startedAt": "2026-10-01T10:00:02Z", "finishedAt": "2026-10-01T10:00:05Z"}}}],
            "containerStatuses": [
                {"name": "agent", "state": {"terminated": {
                    "startedAt": "2026-10-01T10:00:06Z", "finishedAt": "2026-10-01T10:00:16Z"}}},
                {"name": "harvest", "state": {"terminated": {
                    "startedAt": "2026-10-01T10:00:06Z", "finishedAt": "2026-10-01T10:00:18Z"}}},
            ],
        },
    }
    monkeypatch.setattr(runner, "_pod", lambda burst_id: pod)
    assert runner.timings(BURST) == {"boot_s": 6.0, "init_s": 3.0, "agent_s": 10.0, "harvest_s": 12.0}
    monkeypatch.setattr(runner, "_pod", lambda burst_id: None)
    assert runner.timings(BURST) is None
