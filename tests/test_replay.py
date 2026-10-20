"""Golden replay (G4, #45): one archived node and one task, run under each runtime.

The harness is a caller of the `Runner` seam like the scheduler is, so it is
tested the way the contract suite tests a runner: the docker rig in-process (a
shell in the daemon's seat), the cluster rig when there is a cluster. What a
trial was *given* is read back out of what the burst said, and the runtime it
was given is read off the spec the runner was handed — the one thing a shell in
docker's seat cannot honour, and the one thing the harness varies.
"""

import json
import os
from pathlib import Path

import pytest
from control_plane import cli
from control_plane.replay import DEFAULT_RUNTIME, GoldenBurst, ReplayError, matrix, render, replay
from control_plane.runner import RunnerError
from control_plane.store import LocalSessionArchive, session_key
from control_plane.volume import Snapshot
from dev_agent.env import BURST_RESULT_FILENAME
from test_runner_contract import ShellDocker

TASK = "owner/product#7"
RUN = "r1"

# What `dev-agent check` writes, said by a shell: the box looks for the
# transcript of the session it was told to resume, where the CLI would.
CHECK = (
    'if [ -f "$CLAUDE_CONFIG_DIR/projects/-workspace/sess-1.jsonl" ]; then seen=true; else seen=false; fi; '
    f"""printf '{{"check": {{"resume_transcript": %s}}, "ok": %s}}' "$seen" "$seen" """
    f'> "$CLAUDE_CONFIG_DIR/{BURST_RESULT_FILENAME}"'
)


class RecordingShell(ShellDocker):
    """The docker rig, scripted for every trial id, remembering each spec."""

    def __init__(self, tmp_path, archive, refuse: set[str] = frozenset()):
        (tmp_path / "pids").mkdir(parents=True, exist_ok=True)
        super().__init__(tmp_path / "pids", archive=archive)
        self.specs = []
        self.refuse = refuse

    def run(self, spec, log_path=None, heartbeat=None):
        self.specs.append(spec)
        if spec.runtime_class in self.refuse:
            raise RunnerError(f"docker: unknown runtime {spec.runtime_class!r}")
        self.scripts[spec.burst_id] = CHECK
        return super().run(spec, log_path, heartbeat)


def _seed(tmp_path) -> Path:
    seed = tmp_path / "fixture" / "node"
    (seed / "projects" / "-workspace").mkdir(parents=True, exist_ok=True)
    (seed / "projects" / "-workspace" / "sess-1.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")
    return seed


def _fixture(tmp_path, **fields) -> Path:
    _seed(tmp_path)
    raw = {
        "name": "check",
        "task_id": TASK,
        "image": "usf/dev-agent:latest",
        "command": "check",
        "node": {"seed": "node"},
        "resume_session_id": "sess-1",
    } | fields
    path = tmp_path / "fixture" / "golden.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_the_same_node_runs_under_every_runtime_and_the_matrix_says_so(tmp_path):
    """The done-when, on the docker rig (#45): one golden burst, two runtimes,
    one matrix. Every trial is handed the same node and its own runtime, holds
    no GitHub credential, and archives under its own key."""
    archive = LocalSessionArchive(tmp_path / "archive")
    runner = RecordingShell(tmp_path, archive)
    golden = GoldenBurst.load(_fixture(tmp_path))
    trials = replay(golden, [DEFAULT_RUNTIME, "gvisor"], runner, archive, tmp_path / "work", run_id=RUN)

    assert [spec.runtime_class for spec in runner.specs] == [None, "gvisor"]
    assert all(spec.github_token is None and spec.git_user_name is None for spec in runner.specs)
    assert len({spec.archive_as for spec in runner.specs}) == 2
    assert all(spec.resume_session_id == "sess-1" and spec.command == "check" for spec in runner.specs)

    table = matrix(golden, trials)
    assert table["runtimes"] == ["default", "gvisor"]
    assert table["identical"] is True and table["errors"] == {}
    rows = table["rows"]
    assert rows["outcome"] == {"default": "succeeded", "gvisor": "succeeded"}
    assert rows["restored"] == {"default": True, "gvisor": True}
    assert rows["archived"] == {"default": True, "gvisor": True}
    assert rows["exit_code"] == {"default": 0, "gvisor": 0}
    assert all(isinstance(value, float) for value in rows["run_s"].values())
    assert rows["boot_s"] == {"default": None, "gvisor": None}  # docker has no pod to read it off

    # the node was archived once, under a key no ledger row names
    node_key = session_key(TASK, f"golden-check-{RUN}")
    assert archive.recorded_checksum(node_key)

    markdown = render(table)
    assert "| criterion | default | gvisor |" in markdown
    assert "| restored | yes | yes |" in markdown
    assert "egress policy enforcement" in markdown  # what the matrix does not say, said


def test_a_runtime_that_cannot_run_the_burst_is_a_finding_not_a_crash(tmp_path):
    archive = LocalSessionArchive(tmp_path / "archive")
    runner = RecordingShell(tmp_path, archive, refuse={"kata"})
    golden = GoldenBurst.load(_fixture(tmp_path))
    trials = replay(golden, ["kata", DEFAULT_RUNTIME], runner, archive, tmp_path / "work", run_id=RUN)
    table = matrix(golden, trials)
    assert table["rows"]["outcome"] == {"kata": "did not run", "default": "succeeded"}
    assert table["identical"] is False
    assert "unknown runtime" in table["errors"]["kata"]
    assert table["rows"]["archived"]["kata"] is None  # unknown, not "no"


def test_a_node_whose_bytes_do_not_match_is_refused_on_every_trial(tmp_path):
    archive = LocalSessionArchive(tmp_path / "archive")
    key = session_key(TASK, "recorded")
    archive.put(key, _seed(tmp_path))
    golden = GoldenBurst(name="bad", task_id=TASK, image="i", command="check", node=Snapshot(key, "0" * 64))
    runner = RecordingShell(tmp_path, archive)
    trials = replay(golden, [DEFAULT_RUNTIME, "gvisor"], runner, archive, tmp_path / "work", run_id=RUN)
    assert [trial.outcome for trial in trials] == ["did not run", "did not run"]
    assert runner.specs == []  # refused before any burst ran


def test_a_fixture_says_what_it_replays_or_is_refused(tmp_path):
    golden = GoldenBurst.load(_fixture(tmp_path, clone={"url": "https://example/p.git", "ref": "abc"}))
    assert golden.seed == (tmp_path / "fixture" / "node").resolve()
    assert golden.clone == ("https://example/p.git", "abc")

    pinned = GoldenBurst.load(_fixture(tmp_path, node={"key": "k", "checksum": "c"}))
    assert pinned.node == Snapshot("k", "c") and pinned.seed is None

    for bad, says in [
        ({"image": ""}, "missing image"),
        ({"command": "deploy"}, "must be one of run, check, walls"),
        ({"node": {}}, "a seed, or a key"),
        ({"node": {"seed": "nowhere"}}, "not a directory"),
        ({"clone": {"url": "https://example/p.git"}}, "a url and a ref"),
    ]:
        with pytest.raises(ReplayError, match=says):
            GoldenBurst.load(_fixture(tmp_path, **bad))


def test_the_verb_writes_the_matrix_and_fails_only_on_what_it_should(tmp_path, monkeypatch):
    """`control-plane replay`: matrix.json and matrix.md under --out; exit 1
    when a trial did not run, and under --strict when outcomes differ."""
    runners = []

    def open_runner(archive):  # the verb's own archive, the rig's shell
        runners.append(RecordingShell(tmp_path / f"rig{len(runners)}", archive, refuse={"kata"}))
        return runners[-1]

    monkeypatch.setattr(cli, "open_runner", open_runner)
    fixture = _fixture(tmp_path)
    out = tmp_path / "out"
    base = ["replay", str(fixture), "--data-dir", str(tmp_path / "data"), "--out", str(out)]

    assert cli.main([*base, "--runtime", "default", "--runtime", "gvisor"]) == 0
    table = json.loads((out / "matrix.json").read_text())
    assert table["identical"] is True
    assert (out / "matrix.md").read_text().startswith("# Golden replay: check")

    assert cli.main([*base, "--runtime", "default", "--runtime", "kata"]) == 1


def test_the_verb_replays_a_ledger_node_by_its_burst_id(tmp_path, monkeypatch):
    from control_plane.bursts import PlatformStores
    from control_plane.models import BurstRecord, BurstStatus

    stores = PlatformStores.open(tmp_path / "data")
    key = session_key(TASK, "b-real")
    checksum = stores.archive.put(key, _seed(tmp_path))
    stores.ledger.record_burst(
        BurstRecord(
            burst_id="b-real", task_id=TASK, status=BurstStatus.SUCCEEDED,
            session_id="sess-1", session_key=key, session_checksum=checksum,
        )
    )
    runner = RecordingShell(tmp_path, stores.archive)
    monkeypatch.setattr(cli, "open_runner", lambda archive_: runner)
    fixture = _fixture(tmp_path, task_id="other/x#1", resume_session_id=None)
    args = ["replay", str(fixture), "--node", "b-real", "--runtime", "default",
            "--data-dir", str(tmp_path / "data"), "--out", str(tmp_path / "out")]
    assert cli.main(args) == 0
    (spec,) = runner.specs
    assert (spec.task_id, spec.resume_session_id) == (TASK, "sess-1")

    assert cli.main([*args[:3], "nobody", *args[4:]]) == 2


# --- on a cluster: the same node under each RuntimeClass the cluster has ---

REPLAY_RUNTIMES_ENV = "BURST_REPLAY_RUNTIMES"


def test_the_golden_burst_runs_under_each_runtime_class(tmp_path):
    """The rig's runtimes come from the environment — `scripts/burst-cluster.sh
    --gvisor` installs a second one — and the pod's own timestamps fill the
    boot rows the docker rig leaves empty."""
    from jobs_cluster import JobsRig

    rig = JobsRig(tmp_path)
    runtimes = os.environ.get(REPLAY_RUNTIMES_ENV, DEFAULT_RUNTIME).split(",")
    golden = GoldenBurst.load(_fixture(tmp_path, image=rig.runner.phases_image))
    run = rig._run
    for index in range(len(runtimes)):
        rig.runner.scripts[f"golden-{run}-{index}"] = CHECK
        rig._spawned.append(f"golden-{run}-{index}")
    try:
        trials = replay(golden, runtimes, rig.runner, rig.archive, tmp_path / "work", run_id=run)
    finally:
        rig.teardown()
    table = matrix(golden, trials)
    assert table["errors"] == {} and table["identical"] is True
    assert set(table["rows"]["restored"].values()) == {True}
    assert all(value is not None for value in table["rows"]["boot_s"].values())


# --- M4 (G6): a walls burst's result carries the breach rate ---


def _walls(breaches: int) -> str:
    probes_ = [
        {"name": "egress-undeclared-host", "kind": "attack", "succeeded": bool(breaches), "breached": bool(breaches)},
        {"name": "k8s-service-account-token", "kind": "attack", "succeeded": False, "breached": False},
        {"name": "egress-declared-host", "kind": "control", "succeeded": True, "breached": False},
    ]
    result = {"walls": probes_, "attempted": 2, "breaches": breaches, "controls_held": True, "ok": not breaches}
    return f"printf '%s' '{json.dumps(result)}' > \"$CLAUDE_CONFIG_DIR/{BURST_RESULT_FILENAME}\""


class WallsShell(RecordingShell):
    def __init__(self, tmp_path, archive, breaches_on: set[str] = frozenset()):
        super().__init__(tmp_path, archive)
        self.breaches_on = breaches_on

    def run(self, spec, log_path=None, heartbeat=None):
        self.specs.append(spec)
        self.scripts[spec.burst_id] = _walls(1 if spec.runtime_class in self.breaches_on else 0)
        return ShellDocker.run(self, spec, log_path, heartbeat)


def test_the_matrix_carries_the_breach_rate_and_each_probe(tmp_path):
    archive = LocalSessionArchive(tmp_path / "archive")
    runner = WallsShell(tmp_path, archive, breaches_on={"weak"})
    golden = GoldenBurst(name="walls", task_id=TASK, image="i", command="walls")
    trials = replay(golden, [DEFAULT_RUNTIME, "weak"], runner, archive, tmp_path / "work", run_id=RUN)
    table = matrix(golden, trials)
    assert table["rows"]["outcome"] == {"default": "held", "weak": "breached"}
    assert table["rows"]["breaches"] == {"default": "0/2", "weak": "1/2"}
    assert table["breaches"] == 1
    assert table["probes"]["egress-undeclared-host"] == {"default": "held", "weak": "BREACH"}
    assert table["probes"]["egress-declared-host"] == {"default": "ok", "weak": "ok"}
    assert "| egress-undeclared-host | held | BREACH |" in render(table)


def test_a_breach_fails_the_verb_whatever_the_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "open_runner", lambda archive: WallsShell(tmp_path / "rig", archive, {"weak"}))
    fixture = tmp_path / "walls.json"
    fixture.write_text(json.dumps({"name": "walls", "task_id": TASK, "image": "i", "command": "walls"}))
    base = ["replay", str(fixture), "--data-dir", str(tmp_path / "data"), "--out", str(tmp_path / "out")]
    assert cli.main([*base, "--runtime", "default"]) == 0
    assert cli.main([*base, "--runtime", "weak"]) == 1
