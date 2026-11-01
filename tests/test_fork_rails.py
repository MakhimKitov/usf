"""Fork rails (H5): the scheduler decides retry versus branch.

The done-when, end to end through the real engine: a `Scheduler` over a real
`execute_burst`, with a scripted runner in docker's seat whose bytes phases are
the real host ones — so what a burst finds in its volume is exactly what the
session tree handed it. Every scripted burst leaves a marker file on its volume,
which is how "the next burst does not carry the poisoned tail" is observed: the
tail is a file the next volume must not contain.
"""

import json
from pathlib import Path

import pytest
from control_plane.bursts import BurstOutcome, PlatformStores, branch_summary
from control_plane.github_api import PullRequest
from control_plane.models import BurstRecord, BurstStatus, TaskStatus
from control_plane.runner import HostPhases
from control_plane.scheduler import Rails, Scheduler
from control_plane.volume import HarvestOutcome
from dev_agent.env import BURST_RESULT_FILENAME
from test_scheduler import COPILOT_ON, OWNER, FakeApi, _issue, tid


class ScriptedRunner(HostPhases):
    """One scripted ending per burst, in order. Records what each burst was
    handed — its volume's markers, the resume, the fork flag, the prompt."""

    def __init__(self, archive, api, endings):
        self.archive = archive
        self.api = api
        self.endings = list(endings)
        self.seen = []

    def run(self, spec, log_path=None, heartbeat=None):
        ending = self.endings.pop(0)
        n = len(self.seen) + 1
        self.seen.append({
            "burst": spec.burst_id,
            "markers": sorted(p.name for p in spec.volume_dir.glob("marker-*")),
            "resume": spec.resume_session_id,
            "fork": spec.fork_session,
            "prompt": spec.prompt,
        })
        (spec.volume_dir / f"marker-{n}").write_text(ending, encoding="utf-8")
        result = {"session_id": f"sess-{n}", "model": "m", "num_turns": 12, "total_cost_usd": 0.5}
        if ending == "pr":
            self.api.open_prs.append(PullRequest(number=2, state="open", merged=False, head_ref="dev/1-x", head_sha="a"))
            result.update(is_error=False, subtype="success")
            code = 0
        elif ending == "no-pr":
            result.update(is_error=False, subtype="success")
            code = 0
        elif ending == "died":  # the stream died: no result from the engine at all
            result.update(is_error=True, error="ProcessError: the CLI went away")
            code = 1
        else:  # the engine's own ending: error_max_turns / error_max_budget_usd
            result.update(is_error=True, subtype=ending)
            code = 1
        (spec.volume_dir / BURST_RESULT_FILENAME).write_text(json.dumps(result), encoding="utf-8")
        return code

    def alive(self, burst_id):
        return False


def _rig(tmp_path, endings):
    stores = PlatformStores.open(tmp_path / "data")
    (tmp_path / "repo").mkdir()
    api = FakeApi()
    api.ready_issues = [_issue(1)]
    runner = ScriptedRunner(stores.archive, api, endings)
    rails = Rails(max_attempts=5, max_review_rounds=2, failure_cooldown_s=0)
    scheduler = Scheduler(stores, api, tmp_path / "repo", OWNER, rails=rails, review=COPILOT_ON, runner=runner)
    return scheduler, stores, runner


def _tick_until_spent(scheduler, runner):
    for _ in range(20):
        if not runner.endings:
            return
        scheduler.tick()
    raise AssertionError("the scheduler stopped spawning with endings left")


def test_an_approach_failure_branches_and_an_environment_failure_retries(tmp_path):
    """The done-when (#107). Four bursts on one task:

    1. runs out of turns from the task's start — abandoned; the next grows
       from the start again, shown the summary, holding none of its bytes;
    2. dies with no result from the engine — the environment's failure, so it
       is *not* abandoned and the next burst resumes it, bytes and session;
    3. runs out of budget from node 2 — abandoned; the next branches from node 2,
       forking node 2's session, with 3's tail absent and 3's summary shown;
    4. opens the PR.
    """
    scheduler, stores, runner = _rig(
        tmp_path, ["error_max_turns", "died", "error_max_budget_usd", "pr"]
    )
    _tick_until_spent(scheduler, runner)
    first, second, third, fourth = runner.seen

    # 1 → 2: a branch from the root. Nothing to resume, so nothing to fork.
    assert second["markers"] == []
    assert (second["resume"], second["fork"]) == (None, False)
    assert f"Attempt {first['burst']}" in second["prompt"] and "ran out of turns" in second["prompt"]

    # 2 → 3: a retry of the same node — its bytes, its session, continued.
    assert third["markers"] == ["marker-2"]
    assert (third["resume"], third["fork"]) == ("sess-2", False)
    assert "ABANDONED ATTEMPTS" not in third["prompt"]  # node 2 has none of its own

    # 3 → 4: a branch from node 2. The tail (marker-3, sess-3) is not carried.
    assert fourth["markers"] == ["marker-2"]
    assert (fourth["resume"], fourth["fork"]) == ("sess-2", True)
    assert f"Attempt {third['burst']}" in fourth["prompt"] and "ran out of budget" in fourth["prompt"]
    assert f"Attempt {first['burst']}" not in fourth["prompt"]  # another node's attempt

    ledger = stores.ledger
    assert ledger.get_burst(fourth["burst"]).resumed_from_burst_id == second["burst"]
    assert ledger.get_burst(second["burst"]).branch_summary is None
    assert ledger.get_task(tid(1)).status != TaskStatus.BLOCKED

    # each summary is a memory proposal, with the burst that made it
    proposals = {entry.proposed_by_burst: entry for entry in ledger.memory_entries()}
    assert set(proposals) == {first["burst"], third["burst"]}
    entry = proposals[third["burst"]]
    assert (entry.scope, entry.repository, entry.status) == ("product", "owner1/product", "proposed")
    assert entry.claim == ledger.get_burst(third["burst"]).branch_summary
    assert "error_max_budget_usd" in entry.evidence


def test_a_refused_summary_is_a_log_line_and_the_scheduler_goes_on(tmp_path):
    """The abandon is what the rail needs; the proposal is a by-product (H4).
    A ledger that refuses the write — the node's, before 0023 granted
    platform_app INSERT on memory_entries — must not take the replica down with
    the burst it was judging: there, each crash left the task `working`, the
    reconciler scored it "killed mid-burst", and a PR the burst had opened was
    retried into a duplicate."""
    scheduler, stores, runner = _rig(tmp_path, ["no-pr", "pr"])

    def refuse(entry):
        raise RuntimeError("permission denied for table memory_entries")

    stores.ledger.propose_memory = refuse
    _tick_until_spent(scheduler, runner)
    first, second = runner.seen
    assert "without opening a pull request" in stores.ledger.get_burst(first["burst"]).branch_summary
    assert "ABANDONED ATTEMPTS" in second["prompt"]
    assert stores.ledger.get_task(tid(1)).pr_number == 2
    assert stores.ledger.memory_entries() == []


def test_success_without_a_pr_is_an_approach_failure(tmp_path):
    scheduler, stores, runner = _rig(tmp_path, ["no-pr", "pr"])
    _tick_until_spent(scheduler, runner)
    first, second = runner.seen
    assert "without opening a pull request" in stores.ledger.get_burst(first["burst"]).branch_summary
    assert second["markers"] == [] and second["resume"] is None
    assert "ABANDONED ATTEMPTS" in second["prompt"]


def _outcome(status=BurstStatus.FAILED, subtype="error_max_turns", **kwargs):
    return BurstOutcome(
        burst=BurstRecord(burst_id="b", task_id=tid(1), status=status),
        harvest=HarvestOutcome(result={"is_error": True, "subtype": subtype}, violation_count=0,
                               session_key=None, session_checksum=None),
        exit_code=1, log_path=Path("/dev/null"), **kwargs,
    )


@pytest.mark.parametrize(
    ("outcome", "branches"),
    [
        (_outcome(), True),
        (_outcome(subtype="error_max_budget_usd"), True),
        (_outcome(status=BurstStatus.KILLED), False),  # fenced: stopped, not wrong
        (_outcome(rate_limited_until="2099-01-01T00:00:00+00:00"), False),  # the token, not the task
        (_outcome(credential_refused=True), False),  # the member's credential
        (_outcome(subtype="error_during_execution"), False),  # the engine, not the approach
        (_outcome(subtype=None), False),  # a stream that died with no result
    ],
)
def test_only_a_spent_allowance_is_the_approachs_failure(outcome, branches):
    assert outcome.spent_its_allowance is branches


def test_the_summary_is_what_the_platform_holds_and_not_the_transcript():
    burst = BurstRecord(burst_id="b3", task_id=tid(1), resumed_from_burst_id="b2", num_turns=40, cost_usd=1.25)
    text = branch_summary(burst, {"subtype": "error_max_turns"}, "it spent what it was given")
    assert text == (
        "Attempt b3 (work, grown from burst b2) ran out of turns (40 turns, $1.25) "
        "and was abandoned: it spent what it was given."
    )
    root = branch_summary(BurstRecord(burst_id="b1", task_id=tid(1)), None, "why")
    assert "grown from the start of the task" in root and "without a result from the engine" in root
