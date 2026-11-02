"""The M2 reconciler: state machine transitions, rails, kill switch, recovery.

GitHub is a recording fake; execute_burst is monkeypatched and the runner in
docker's seat never has a burst `alive` — these tests drive the decision layer
only (the burst choreography has its own tests).
"""

import json
from pathlib import Path

import pytest
from control_plane import scheduler as sched_mod
from control_plane.bursts import BurstOutcome, PlatformStores
from control_plane.clone import CloneError
from control_plane.github_api import COPILOT_REVIEWER, Issue, PullRequest, Review, ReviewComment
from control_plane.models import (
    BurstRecord,
    BurstStatus,
    RepositoryRecord,
    TaskRecord,
    TaskStatus,
    task_id_for,
    utc_now,
)
from control_plane.review import ReviewPolicy
from control_plane.store import ArchiveError, RegistryError
from control_plane.runner import HostPhases
from control_plane.scheduler import Rails, Scheduler
from control_plane.subscriptions import declare
from control_plane.testing import E2EPolicy
from control_plane.volume import HarvestOutcome

OWNER = "owner1"
REPOSITORY = "owner1/product"
COPILOT_ON = ReviewPolicy(enabled=True, providers=("copilot",))


class NoContainers(HostPhases):
    """Docker's seat (a `Runner`, E4) for a suite that fakes `execute_burst`:
    nothing ever runs here, so nothing is ever alive — and the bytes phases (G1)
    are never reached either, since no burst is spawned through them."""

    def run(self, spec, log_path=None, heartbeat=None):
        raise AssertionError("execute_burst is faked in this suite — the runner is never reached")

    def alive(self, burst_id):
        return False


class FakeApi:
    repo = REPOSITORY  # 'owner/name' — what the derived task id is built from

    def __init__(self):
        self.ready_issues = []
        self.issues = {}
        self.prs = {}
        self.open_prs = []
        self.reviews = {}
        self.review_comments = {}
        self.label_ops = []
        self.comments = []
        self.rerequests = []

    def list_ready_issues(self):
        return list(self.ready_issues)

    def get_issue(self, number):
        return self.issues.get(number) or Issue(number=number, title="t", body="b", labels=["human"])

    def add_labels(self, number, labels):
        self.label_ops.append(("add", number, tuple(labels)))

    def remove_label(self, number, label):
        self.label_ops.append(("remove", number, label))

    def comment(self, number, body):
        self.comments.append((number, body))

    def open_pr_with_branch_prefix(self, prefix):
        return next((pr for pr in self.open_prs if pr.head_ref.startswith(prefix)), None)

    def get_pr(self, number):
        return self.prs[number]

    def list_reviews(self, pr_number):
        return self.reviews.get(pr_number, [])

    def list_review_comments(self, pr_number):
        return self.review_comments.get(pr_number, [])

    def request_reviewers(self, pr_number, reviewers):
        self.rerequests.append((pr_number, tuple(reviewers)))
        return []  # nothing silently dropped


def _outcome(exit_code=0, is_error=False, rate_limited_until=None):
    return BurstOutcome(
        burst=BurstRecord(burst_id="fake", task_id=tid(1), status=BurstStatus.SUCCEEDED),
        harvest=HarvestOutcome(
            result={"is_error": is_error},
            violation_count=0,
            session_key="sessions/1/fake/session.tar.gz",
            session_checksum="c" * 64,
        ),
        exit_code=exit_code,
        log_path=Path("/dev/null"),
        rate_limited_until=rate_limited_until,
    )


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    stores = PlatformStores.open(tmp_path / "data")
    api = FakeApi()
    rails = Rails(max_attempts=2, max_review_rounds=2, failure_cooldown_s=0)
    scheduler = Scheduler(
        stores, api, tmp_path / "repo", OWNER, rails=rails, review=COPILOT_ON, runner=NoContainers()
    )
    spawned = []

    def fake_execute(stores_, task, repo, prompt, **kwargs):
        spawned.append(
            {
                "task": task.task_id,
                "prompt": prompt,
                "kind": kwargs.get("kind"),
                "role": kwargs.get("role"),
                "extra_env": kwargs.get("extra_env"),
                "heartbeat": kwargs.get("heartbeat"),
                "capacity": kwargs.get("capacity"),
            }
        )
        return fake_execute.next_outcome

    fake_execute.next_outcome = _outcome()
    monkeypatch.setattr(sched_mod, "execute_burst", fake_execute)
    return scheduler, api, stores, spawned, fake_execute


def _issue(number=1, labels=("ready", "human"), state="open"):
    return Issue(number=number, title="add /coinflip", body="do it", labels=list(labels), state=state)


def _pr(number=2, state="open", merged=False, head_ref="dev/1-coinflip", head_sha="abc"):
    return PullRequest(number=number, state=state, merged=merged, head_ref=head_ref, head_sha=head_sha)


def _changes_requested(review_id=10, author=OWNER):
    return Review(id=review_id, author=author, state="CHANGES_REQUESTED", body="tweak it")


def _copilot_review(review_id=20):
    return Review(id=review_id, author=COPILOT_REVIEWER, state="COMMENTED", body="overview")


def _finding(review_id=20, comment_id=900):
    return ReviewComment(
        id=comment_id, review_id=review_id, author="Copilot", path="bot/x.py", line=3, body="off-by-one"
    )


def tid(number=1):
    """The id the scheduler would derive for that issue — never typed by hand."""
    return task_id_for(REPOSITORY, number)


def _task(number=1, **kwargs):
    """A task row for an issue of the fixture's product."""
    return TaskRecord(
        task_id=tid(number), repository=REPOSITORY, issue_number=number, **kwargs
    )


def _awaiting(stores, **overrides):
    fields = dict(status=TaskStatus.AWAITING_REVIEW, pr_number=2) | overrides
    stores.ledger.upsert_task(_task(1, **fields))


def test_claims_ready_issue_and_swaps_labels(rig):
    scheduler, api, stores, _, _ = rig
    api.ready_issues = [_issue()]
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "work"
    assert ("remove", 1, "ready") in api.label_ops
    assert ("add", 1, ("in-progress",)) in api.label_ops

    scheduler.observe()  # label drift: already claimed → no double claim
    assert api.label_ops.count(("remove", 1, "ready")) == 1


def test_work_burst_to_awaiting_review(rig):
    scheduler, api, stores, spawned, _ = rig
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW
    assert task.pr_number == 2 and task.next_burst_kind is None
    assert spawned[0]["kind"] == "work"
    assert "trusted intent" in spawned[0]["prompt"]  # human provenance framing
    # deterministic on PR open — Copilot auto-review observed not firing (PR #4)
    assert api.rerequests == [(2, (COPILOT_REVIEWER, OWNER))]


def test_failed_bursts_retry_then_block(rig):
    scheduler, api, stores, spawned, fake_execute = rig
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    fake_execute.next_outcome = _outcome(exit_code=1)

    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1  # retry queued

    api.ready_issues = []
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.BLOCKED and task.attempts == 2  # rail tripped
    assert any("failed attempts" in body for _, body in api.comments)
    assert ("remove", 1, "in-progress") in api.label_ops
    assert len(spawned) == 2


def test_success_without_pr_is_a_failed_attempt(rig):
    scheduler, api, stores, _, _ = rig
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = []  # burst "succeeded" but the contract is a PR
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1


def test_a_burst_that_closes_its_issue_is_done_not_a_failed_attempt(rig, monkeypatch):
    """#93: some work needs no PR — the symptom was already fixed, the report is
    a duplicate — and the burst says so by closing the issue with its evidence.
    That is a finished task, not an attempt to retry until the rail trips."""
    scheduler, api, stores, spawned, fake_execute = rig

    def closes_the_issue(*args, **kwargs):
        api.issues[1] = _issue(labels=("in-progress", "human"), state="closed")
        return fake_execute(*args, **kwargs)

    monkeypatch.setattr(sched_mod, "execute_burst", closes_the_issue)
    api.ready_issues = [_issue()]
    api.issues[1] = _issue(labels=("in-progress", "human"))
    api.open_prs = []  # no PR — and none was needed
    scheduler.tick()

    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.DONE and task.attempts == 0
    assert task.next_burst_kind is None and task.claimed_by is None  # nothing queued, lease released
    assert ("remove", 1, "in-progress") in api.label_ops
    assert api.comments == []  # no "parking this task after N failed attempts"

    api.ready_issues = []
    scheduler.tick()
    assert len(spawned) == 1  # the task is over — no retry burst


def test_a_closed_issue_ends_a_claimed_task_before_any_burst(rig):
    """The general rule (#93): whoever closed the issue — a burst on an earlier
    tick, or a human mid-flight — the next observation ends the task instead of
    spawning against a closed issue."""
    scheduler, api, stores, spawned, _ = rig
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, attempts=1))
    api.issues[1] = _issue(labels=("in-progress", "human"), state="closed")
    scheduler.tick()

    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.DONE and task.attempts == 1  # no attempt spent on it
    assert spawned == [] and ("remove", 1, "in-progress") in api.label_ops


def test_a_closed_issue_leaves_a_task_whose_pr_is_still_open_alone(rig):
    """An `awaiting_review` task is ended by its PR, not by its issue: the issue
    can close early (a `Closes #<n>` from another PR, a human tidying up) while
    the reviewers still hold the ball."""
    scheduler, api, stores, _, _ = rig
    _awaiting(stores)
    api.issues[1] = _issue(labels=("in-progress",), state="closed")
    api.prs[2] = _pr()  # open, unmerged
    scheduler.observe()
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.AWAITING_REVIEW


def test_owner_changes_requested_triggers_review_round(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.AWAITING_REVIEW, pr_number=2)
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [_changes_requested()]
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "review"
    assert task.review_rounds == 1


def test_seen_and_findingless_reviews_never_trigger_rounds(rig):
    scheduler, api, stores, _, _ = rig
    _awaiting(stores, last_review_id=10)
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [
        _changes_requested(review_id=10),  # already processed (cursor)
        _copilot_review(review_id=11),  # overview only — no inline findings
    ]
    scheduler.observe()
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.AWAITING_REVIEW


def test_copilot_findings_trigger_an_autonomous_round(rig):
    """Copilot never requests changes (COMMENTED by design) — its inline
    findings are the trigger; no human involved."""
    scheduler, api, stores, _, _ = rig
    _awaiting(stores)
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [_copilot_review()]
    api.review_comments[2] = [_finding()]
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "review"
    assert task.review_rounds == 1


def test_copilot_round_with_push_re_requests_copilot_only(rig):
    scheduler, api, stores, _, _ = rig
    _awaiting(stores, status=TaskStatus.CLAIMED, next_burst_kind="review", review_rounds=1)
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [_copilot_review()]
    api.review_comments[2] = [_finding()]
    orig = sched_mod.execute_burst

    def push_and_succeed(stores_, task, repo, prompt, **kwargs):
        assert "off-by-one" in prompt  # the finding reached the dev
        api.prs[2] = _pr(head_sha="new")  # the burst pushed fixes
        return _outcome()

    sched_mod.execute_burst = push_and_succeed
    try:
        scheduler.tick()
    finally:
        sched_mod.execute_burst = orig
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW
    assert task.last_review_id == 20
    assert api.rerequests == [(2, (COPILOT_REVIEWER,))]  # fresh diff → Copilot; no owner ping


def test_copilot_reply_only_round_re_requests_nobody(rig):
    """Same diff after the round — re-requesting Copilot would just loop."""
    scheduler, api, stores, _, _ = rig
    _awaiting(stores, status=TaskStatus.CLAIMED, next_burst_kind="review", review_rounds=1)
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [_copilot_review()]
    api.review_comments[2] = [_finding()]
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW
    assert task.last_review_id == 20  # processed — won't re-trigger
    assert api.rerequests == []


def test_copilot_rounds_cap_softly_for_the_owner(rig):
    """Exhausted rounds on Copilot findings never park the task — the human
    merge gate decides; only owner rounds can block."""
    scheduler, api, stores, _, _ = rig
    _awaiting(stores, review_rounds=2)  # rig cap
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [_copilot_review(review_id=30)]
    api.review_comments[2] = [_finding(review_id=30)]
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW  # not blocked
    assert task.last_review_id == 30  # cursor advanced — no re-trigger loop
    assert any("cap" in body for _, body in api.comments)


def test_review_burst_advances_cursor_and_re_requests(rig):
    scheduler, api, stores, spawned, _ = rig
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED,
            pr_number=2,
            next_burst_kind="review",
            review_rounds=1,
        )
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [_changes_requested(review_id=10)]
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW
    assert task.last_review_id == 10  # advanced only on success
    assert api.rerequests == [(2, (COPILOT_REVIEWER, OWNER))]  # the M1 gap, deterministic
    assert spawned[0]["kind"] == "review"
    assert "tweak it" in spawned[0]["prompt"]


def test_merged_pr_completes_the_task(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.AWAITING_REVIEW, pr_number=2)
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr(state="closed", merged=True)
    scheduler.observe()
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.DONE
    assert ("remove", 1, "in-progress") in api.label_ops


def test_pr_closed_unmerged_blocks(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.AWAITING_REVIEW, pr_number=2)
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr(state="closed", merged=False)
    scheduler.observe()
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.BLOCKED
    assert any("closed without merge" in body for _, body in api.comments)


def test_agent_blocked_label_parks_the_task(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    api.issues[1] = _issue(labels=("in-progress", "blocked"))
    scheduler.observe()
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.BLOCKED


def test_an_exhausted_pool_idles_the_task_instead_of_failing_it(rig):
    """F2: the pool is asked before anything is synced or spawned. Nothing
    eligible is not a failed attempt — nothing was tried — so the task keeps its
    attempts and its `claimed` status, loses only the lease, and the adopter
    hears about it as `capacity_exhausted`."""
    scheduler, api, stores, spawned, _ = rig
    stores.ledger.register_subscription(
        declare("ada", "env:ADA_TOKEN", 1_000, "2027-09-01"), actor="op"
    )  # contributed, and already spent for this window
    burst = BurstRecord(burst_id="b-earlier", task_id=tid(1), subscription_id="ada")
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    stores.ledger.record_burst(burst)
    burst.status = BurstStatus.SUCCEEDED
    burst.finished_at = utc_now()
    burst.exit_code = 0
    burst.input_tokens = 1_000
    stores.ledger.finish_burst(burst)

    api.ready_issues = []
    scheduler.tick()

    task = stores.ledger.get_task(tid(1))
    assert spawned == []  # nothing ran
    assert task.status == TaskStatus.CLAIMED and task.attempts == 0  # and nothing failed
    assert task.claimed_by is None and task.claimed_until is None  # the lease is back
    (event,) = [e for e in stores.ledger.events() if e.kind == "capacity_exhausted"]
    assert event.payload["repository"] == REPOSITORY and event.payload["task"] == tid(1)
    assert api.comments == []  # nobody is told the task failed, because it did not


def test_a_burst_names_the_subscription_that_paid_for_it(rig, monkeypatch):
    """The stamp (F2): the pick happens once per burst, in the scheduler, and
    the id reaches the burst row — the token reaches the box and nothing else."""
    scheduler, api, stores, spawned, _ = rig
    monkeypatch.setenv("ADA_TOKEN", "adas-secret")
    stores.ledger.register_subscription(
        declare("ada", "env:ADA_TOKEN", 1_000_000, "2027-09-01"), actor="op"
    )
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    scheduler.tick()

    assert spawned[0]["capacity"].subscription_id == "ada"
    assert spawned[0]["capacity"].token == "adas-secret"
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.AWAITING_REVIEW


def test_a_spent_token_is_retried_on_another_without_spending_an_attempt(rig, monkeypatch):
    """F3's done-when: a forced limit flips tokens and keeps the session.

    The engine benches the subscription that came back limited (its own test);
    what the rail must do is *not* charge the task — no attempt, no cooldown,
    nothing said on the issue — and give the lease back, so the next tick admits
    the same task on the other member's token and resumes the session the
    limited burst had already written.
    """
    scheduler, api, stores, spawned, fake_execute = rig
    monkeypatch.setenv("ADA_TOKEN", "adas-secret")
    monkeypatch.setenv("BO_TOKEN", "bos-secret")
    stores.ledger.register_subscription(declare("ada", "env:ADA_TOKEN", 1_000_000, "2027-09-01"), "op")
    stores.ledger.register_subscription(declare("bo", "env:BO_TOKEN", 500_000, "2027-09-01"), "op")

    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    benched = "2027-01-01T00:00:00+00:00"  # a bench that has not lifted yet
    fake_execute.next_outcome = _outcome(exit_code=1, is_error=True, rate_limited_until=benched)

    scheduler.tick()
    stores.ledger.back_off_subscription("ada", benched)  # what execute_burst does for real
    stores.ledger.transition_task(  # and the session the limited burst had written
        tid(1), {"session_id": "sess-9"}, expected_status=TaskStatus.CLAIMED, expected_holder=None
    )

    task = stores.ledger.get_task(tid(1))
    assert spawned[0]["capacity"].subscription_id == "ada"  # the bigger cap went first
    assert (task.status, task.attempts) == (TaskStatus.CLAIMED, 0)  # no attempt charged
    assert task.claimed_by is None and task.claimed_until is None
    assert api.comments == []  # the task did not fail, so nobody is told it did

    api.ready_issues = []
    api.open_prs = [_pr()]
    fake_execute.next_outcome = _outcome()
    scheduler.tick()

    assert spawned[1]["capacity"].subscription_id == "bo"  # flipped: ada is benched
    assert spawned[1]["task"] == tid(1)
    # the session the limited burst left is still the task's — the flip changed
    # who pays, not what the work remembers (`execute_burst` resumes from it)
    assert stores.ledger.get_task(tid(1)).session_id == "sess-9"
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.AWAITING_REVIEW


def test_an_unusable_session_snapshot_fails_the_attempt_and_not_the_replica(rig, monkeypatch):
    """#87: the archive no longer holds the node the ledger named — pruned,
    offline, or bytes that disagree with the recorded checksum. That used to
    leave `execute_burst` with an uncaught `ArchiveError`, killing the replica;
    its peer then resolved the same leaf and died the same way. It is an attempt
    on the task, and the rails park it with the key in the comment."""
    scheduler, api, stores, spawned, _ = rig

    def gone(*args, **kwargs):
        raise ArchiveError("no snapshot at sessions/owner1/product#1/b-old/session.tar.gz")

    monkeypatch.setattr(sched_mod, "execute_burst", gone)
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()

    scheduler.tick()  # no exception reaches the loop
    task = stores.ledger.get_task(tid(1))
    assert (task.status, task.attempts) == (TaskStatus.CLAIMED, 1)

    api.ready_issues = []
    scheduler.tick()  # the rail trips at max_attempts=2
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.BLOCKED
    parked = [body for _, body in api.comments if "parking this task" in body]
    assert parked and "session snapshot unusable" in parked[0]
    assert "sessions/owner1/product#1/b-old/session.tar.gz" in parked[0]  # the key, for whoever looks


def test_a_task_the_ledger_refuses_fails_the_attempt_and_not_the_replica(rig, monkeypatch):
    """#90's other half: the registry wall can refuse a task on the way into a
    burst, and a refusal is an attempt — not an exception out of the tick. (The
    product would have to leave the registry between this tick's read and the
    spawn, which the grants make very unlikely and not impossible.)"""
    scheduler, api, stores, spawned, _ = rig

    def refused(*args, **kwargs):
        raise RegistryError("owner1/product is not a registered product — `control-plane product register …` first")

    monkeypatch.setattr(sched_mod, "execute_burst", refused)
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()

    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert (task.status, task.attempts) == (TaskStatus.CLAIMED, 1)


def test_the_org_stop_freezes_the_tick(rig):
    scheduler, api, stores, spawned, _ = rig
    api.ready_issues = [_issue()]
    stores.ledger.stop_org("op", "drill")
    scheduler.tick()
    assert stores.ledger.get_task(tid(1)) is None  # nothing claimed
    assert not spawned and not api.label_ops


def test_reconcile_rolls_back_a_crashed_burst(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.WORKING, next_burst_kind="work")
    )
    crashed = BurstRecord(burst_id="dead1", task_id=tid(1))  # status: running
    stores.ledger.record_burst(crashed)
    scheduler.reconcile_running()
    assert stores.ledger.running_bursts() == []
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1
    assert task.next_burst_kind == "work"  # retries the same kind


def test_failure_cooldown_defers_retry(rig):
    scheduler, _, stores, _, _ = rig
    scheduler.rails.failure_cooldown_s = 3600
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, attempts=1, last_burst_at=utc_now()
        )
    )
    assert scheduler.pick_next() is None
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED,
            attempts=1,
            last_burst_at="2020-01-01T00:00:00+00:00",
        )
    )
    assert scheduler.pick_next() is not None


def test_daily_ceiling_stops_spawning(rig):
    scheduler, _, stores, _, _ = rig
    scheduler.rails.daily_burst_ceiling = 0
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    assert scheduler.pick_next() is None


def test_re_ready_resets_rails_and_resumes_review_path(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.BLOCKED,
            pr_number=2,
            attempts=2,
            review_rounds=2,
        )
    )
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.prs[2] = _pr()  # the PR is still open → resume as a review round
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED
    assert task.attempts == 0 and task.review_rounds == 0
    assert task.next_burst_kind == "review"


def test_sync_clone_feeds_the_burst_and_failures_hit_the_rail(rig, tmp_path):
    scheduler, api, stores, spawned, _ = rig
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    platform_clone = tmp_path / "data" / "clones" / "owner__product"
    scheduler.sync_clone = lambda task: platform_clone
    captured = []
    orig = sched_mod.execute_burst

    def capture(stores_, task, repo, prompt, **kwargs):
        captured.append(repo)
        return orig(stores_, task, repo, prompt, **kwargs)

    sched_mod.execute_burst = capture
    try:
        scheduler.tick()
    finally:
        sched_mod.execute_burst = orig
    assert captured == [platform_clone]  # the platform clone, not the operator's

    def broken_clone(task):
        raise CloneError("origin unreachable")

    stores.ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED))
    api.issues[2] = _issue(number=2)
    api.prs[2] = _pr()  # task 1 sits in awaiting_review on PR #2
    scheduler.sync_clone = broken_clone
    scheduler.rails.failure_cooldown_s = 0
    scheduler.tick()
    task = stores.ledger.get_task(tid(2))
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1


def test_rails_from_env_overrides():
    rails = Rails.from_env(
        {"SCHEDULER_MAX_ATTEMPTS": "5", "SCHEDULER_BURST_BUDGET_USD": "0.5", "SCHEDULER_LEASE_S": "30"}
    )
    assert rails.max_attempts == 5
    assert rails.lease_s == 30
    assert rails.max_review_rounds == 3  # untouched default
    assert rails.concurrency == 1  # the registry row's, never the env's (E2)
    # per-burst budget and turns are the role row's since D5 — not a rail, and
    # the retired env name is ignored here (startup refuses it, test_registry_cli)
    assert not hasattr(rails, "burst_budget_usd") and not hasattr(rails, "burst_max_turns")


def test_review_off_requests_owner_only_on_pr_open(rig):
    scheduler, api, stores, _, _ = rig
    scheduler.review = ReviewPolicy()  # ships off
    scheduler.machine_reviewers = scheduler.review.machine_reviewers()
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    scheduler.tick()
    assert api.rerequests == [(2, (OWNER,))]  # no machine reviewer to fire


def test_review_off_ignores_copilot_findings(rig):
    # a Copilot review outside the policy's registry (e.g. requested by hand)
    # must not trigger autonomous rounds
    scheduler, api, stores, _, _ = rig
    scheduler.review = ReviewPolicy()
    scheduler.machine_reviewers = scheduler.review.machine_reviewers()
    _awaiting(stores)
    api.prs[2] = _pr()
    api.reviews[2] = [_copilot_review(review_id=30)]
    api.review_comments[2] = [_finding(review_id=30)]
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.review_rounds == 0


# --- the claude provider: assess bursts (REVIEWERS.md slice 2) ---

REVIEWER_LOGIN = "discord-ai-reviewer[bot]"
TESTER_LOGIN = "discord-ai-tester[bot]"
CLAUDE_ON = ReviewPolicy(enabled=True, providers=("claude", "copilot"))


class FakeReviewerApi:
    def __init__(self):
        self.posted = []
        self.commented = []
        self.filed = []
        self.open_auto = []

    def post_review(self, pr_number, body, findings, event="COMMENT"):
        self.posted.append((pr_number, body, findings, event))

    def comment(self, number, body):
        self.commented.append((number, body))

    def list_open_issues_by_label(self, label):
        return list(self.open_auto)

    def create_issue(self, title, body, labels):
        self.filed.append((title, body, labels))
        return 100 + len(self.filed)


def _claude_rig(rig):
    scheduler, api, stores, spawned, fake_execute = rig
    scheduler.review = CLAUDE_ON
    scheduler.reviewer_api = FakeReviewerApi()
    scheduler.reviewer_login = REVIEWER_LOGIN
    scheduler.machine_reviewers = set(CLAUDE_ON.machine_reviewers()) | {REVIEWER_LOGIN}
    return scheduler, api, stores, spawned, fake_execute


def _assess_outcome(final_text):
    outcome = _outcome()
    outcome.harvest.result["final_text"] = final_text
    return outcome


def test_work_burst_queues_assess_when_claude_active(rig):
    scheduler, api, stores, spawned, _ = _claude_rig(rig)
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "assess"
    assert api.rerequests == [(2, (COPILOT_REVIEWER, OWNER))]  # request providers still fire


def test_assess_burst_posts_findings_and_returns_to_awaiting(rig):
    scheduler, api, stores, spawned, fake_execute = _claude_rig(rig)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2,
            next_burst_kind="assess", session_id="dev-session",
        )
    )
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome(
        '{"verdict": "Sound.", "findings": [{"path": "bot/x.py", "line": 3, "body": "bug"}]}'
    )
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.next_burst_kind is None
    assert task.session_id == "dev-session"  # the reviewer never pollutes the dev's session
    assert spawned[0]["kind"] == "assess"
    (pr_number, body, findings, event) = scheduler.reviewer_api.posted[0]
    assert (pr_number, findings, event) == (
        2, [{"path": "bot/x.py", "line": 3, "body": "bug"}], "REQUEST_CHANGES"
    )
    # the overview wears the uniform: header + reviewed commit, verdict below
    assert "Machine review" in body and "Sound." in body and "reviewed `abc`" in body


def test_clean_assessment_approves_the_pr(rig):
    scheduler, api, stores, _, fake_execute = _claude_rig(rig)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="assess")
    )
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome('{"verdict": "Clean.", "findings": []}')
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW
    (pr_number, body, findings, event) = scheduler.reviewer_api.posted[0]
    assert (pr_number, findings, event) == (2, [], "APPROVE")
    assert "Clean." in body


def test_assess_failure_gives_up_softly_never_blocks(rig):
    scheduler, api, stores, _, fake_execute = _claude_rig(rig)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="assess")
    )
    api.prs[2] = _pr()
    fake_execute.next_outcome = _outcome(exit_code=1)
    scheduler.tick()  # attempt 1 → retry
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1
    scheduler.tick()  # attempt 2 = rails cap → give up on the review, not the task
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.attempts == 0
    assert any("machine review could not run" in body for _, body in api.comments)
    assert ("remove", 1, "in-progress") not in api.label_ops  # task is NOT parked


def test_unparseable_reviewer_output_is_a_failed_attempt(rig):
    scheduler, api, stores, _, fake_execute = _claude_rig(rig)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="assess")
    )
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome("I reviewed it, looks good!")
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1  # retried, not posted
    assert scheduler.reviewer_api.posted == []


def test_assess_skipped_when_pr_resolved_while_queued(rig):
    scheduler, api, stores, spawned, _ = _claude_rig(rig)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="assess")
    )
    api.prs[2] = _pr(merged=True, state="closed")
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW  # merged/closed handled next tick
    assert spawned == []


def test_reviewer_findings_trigger_dev_round(rig):
    scheduler, api, stores, _, _ = _claude_rig(rig)
    _awaiting(stores)
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [Review(id=40, author=REVIEWER_LOGIN, state="COMMENTED", body="Sound.")]
    api.review_comments[2] = [_finding(review_id=40)]
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "review"


def test_machine_changes_requested_triggers_even_without_inline_comments(rig):
    # the 422 body-only fallback loses the inline comments but keeps the
    # review state — the round must still fire
    scheduler, api, stores, _, _ = _claude_rig(rig)
    _awaiting(stores)
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [
        Review(id=42, author=REVIEWER_LOGIN, state="CHANGES_REQUESTED", body="findings in body")
    ]
    api.review_comments[2] = []
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "review"


def test_reviewer_approval_triggers_nothing(rig):
    scheduler, api, stores, _, _ = _claude_rig(rig)
    _awaiting(stores)
    api.prs[2] = _pr()
    api.reviews[2] = [Review(id=43, author=REVIEWER_LOGIN, state="APPROVED", body="Clean.")]
    api.review_comments[2] = []
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.review_rounds == 0


def test_pushed_dev_round_queues_assess_again(rig):
    scheduler, api, stores, spawned, fake_execute = _claude_rig(rig)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2,
            next_burst_kind="review", session_id="s",
        )
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.reviews[2] = [Review(id=41, author=REVIEWER_LOGIN, state="COMMENTED", body="findings")]
    api.review_comments[2] = [_finding(review_id=41)]
    api.prs[2] = _pr(head_sha="before")
    calls = {"n": 0}

    def get_pr(number):  # the dev pushes during the round: head sha changes
        calls["n"] += 1
        return _pr(head_sha="before") if calls["n"] == 1 else _pr(head_sha="after")

    api.get_pr = get_pr
    scheduler.run_task_burst(stores.ledger.get_task(tid(1)))
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "assess"
    assert task.last_review_id == 41


# --- the tester funnel (TESTER.md slice 1) ---

TEST_MANIFEST = """---
env: [DISCORD_TOKEN]
tools: []
---
Boot: run the bot in the sandbox guild.
"""


def _tester_rig(rig, monkeypatch, manifest=TEST_MANIFEST, provisioned=True):
    scheduler, api, stores, spawned, fake_execute = _claude_rig(rig)
    scheduler.testing = E2EPolicy(enabled=True)
    # D3: reports and auto issues post under the tester App's own identity —
    # a second fake, so a post to the wrong identity fails the assertion
    scheduler.tester_api = FakeReviewerApi()
    scheduler.tester_login = TESTER_LOGIN
    scheduler.machine_reviewers.add(TESTER_LOGIN)
    scheduler.repo.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (scheduler.repo / "TESTING.md").write_text(manifest, encoding="utf-8")
    if provisioned:
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "sandbox-token")
    else:
        monkeypatch.delenv("TEST_DISCORD_TOKEN", raising=False)
    return scheduler, api, stores, spawned, fake_execute


def _tester_transcript(stores, *calls, burst_id="fake"):
    """Write a CLI-shaped transcript into the fake burst's volume — what B6's
    resolver reads. Each call is (tool_use_id, command)."""
    projects = stores.data_dir / "volumes" / burst_id / "projects" / "-workspace-repo"
    projects.mkdir(parents=True, exist_ok=True)
    lines = []
    for use_id, command in calls:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": use_id,
                                "name": "Bash",
                                "input": {"command": command},
                            }
                        ]
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": use_id, "is_error": False}
                        ]
                    },
                }
            )
        )
    (projects / "sess.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _queued_assess(stores):
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="assess")
    )


def test_clean_assess_queues_test_when_product_plugs_in(rig, monkeypatch):
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch)
    _queued_assess(stores)
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome('{"verdict": "Clean.", "findings": []}')
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "test"
    assert task.attempts == 0


def test_assess_findings_never_queue_test(rig, monkeypatch):
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch)
    _queued_assess(stores)
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome(
        '{"verdict": "Broken.", "findings": [{"path": "bot/x.py", "line": 3, "body": "bug"}]}'
    )
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.next_burst_kind is None


def test_missing_sandbox_credentials_skip_test_with_comment(rig, monkeypatch):
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch, provisioned=False)
    _queued_assess(stores)
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome('{"verdict": "Clean.", "findings": []}')
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.next_burst_kind is None
    assert any("TEST_DISCORD_TOKEN" in body for _, body in api.comments)


def test_product_without_manifest_is_not_testable(rig, monkeypatch):
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch, manifest=None)
    _queued_assess(stores)
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome('{"verdict": "Clean.", "findings": []}')
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.next_burst_kind is None
    assert api.comments == []  # a logged skip, not a commented one


def test_test_burst_boots_sandbox_and_posts_report(rig, monkeypatch):
    scheduler, api, stores, spawned, fake_execute = _tester_rig(rig, monkeypatch)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2,
            next_burst_kind="test", session_id="dev-session",
        )
    )
    api.prs[2] = _pr()
    _tester_transcript(stores, ("toolu_boot", "grep -q synced /session/product.log"))
    fake_execute.next_outcome = _assess_outcome(
        '{"verdict": "Clean.", "scenarios": [{"name": "boot", "result": "pass", '
        '"tool_use_id": "toolu_boot", "evidence": "synced"}], "defects": [], "issues": []}'
    )
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.next_burst_kind is None
    assert task.session_id == "dev-session"  # the tester never pollutes the dev's session
    assert spawned[0]["kind"] == "test" and spawned[0]["role"] == "tester"
    # the injection is the TEST_ intersection, unprefixed on the way in
    assert spawned[0]["extra_env"] == {"DISCORD_TOKEN": "sandbox-token"}
    # the testing contract's prose rides the task prompt
    assert "sandbox guild" in spawned[0]["prompt"]
    ((pr_number, body),) = scheduler.tester_api.commented  # clean run → plain comment
    assert pr_number == 2 and "Machine test report" in body and "✅" in body
    # B6: the cited-call column is platform-extracted from the transcript
    assert "`Bash: grep -q synced /session/product.log`" in body
    assert scheduler.tester_api.posted == []  # no review costume without PR-caused defects
    # D3: nothing about the test run rides the reviewer identity
    assert scheduler.reviewer_api.commented == [] and scheduler.reviewer_api.filed == []


def test_registered_refs_govern_the_sandbox_injection(rig, monkeypatch, tmp_path):
    """D4: a registered product's row declares the refs its tester can reach;
    the resolver reads them at spawn. TEST_DISCORD_TOKEN sits in the process
    env, but this row points elsewhere — the prefix default is not consulted."""
    scheduler, api, stores, spawned, fake_execute = _tester_rig(rig, monkeypatch)
    token_file = tmp_path / "discord-token"
    token_file.write_text("from-the-row's-file\n", encoding="utf-8")
    scheduler.testing = E2EPolicy(enabled=True, env_refs={"DISCORD_TOKEN": f"file:{token_file}"})
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="test")
    )
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome(
        '{"verdict": "Clean.", "scenarios": [], "defects": [], "issues": []}'
    )
    scheduler.tick()
    assert spawned[0]["extra_env"] == {"DISCORD_TOKEN": "from-the-row's-file"}

    # the manifest drifts to a name the row never declared → a skip naming the
    # gap, not a reach for env:TEST_GUILD_ID
    (scheduler.repo / "TESTING.md").write_text(
        "---\nenv: [DISCORD_TOKEN, GUILD_ID]\n---\nprose", encoding="utf-8"
    )
    monkeypatch.setenv("TEST_GUILD_ID", "would-be-reachable-under-the-prefix")
    _queued_assess(stores)
    fake_execute.next_outcome = _assess_outcome('{"verdict": "Clean.", "findings": []}')
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.next_burst_kind is None
    assert any("GUILD_ID ← no ref declared" in body for _, body in api.comments)


def test_pr_caused_defects_return_to_the_developer(rig, monkeypatch):
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="test")
    )
    api.prs[2] = _pr()
    _tester_transcript(stores, ("toolu_roll", "python3 drive_roll.py"))
    fake_execute.next_outcome = _assess_outcome(
        '{"verdict": "One defect.", "scenarios": [{"name": "roll", "result": "fail", '
        '"tool_use_id": "toolu_roll", "evidence": "wrong reply"}], '
        '"defects": [{"summary": "bad reply", "repro": "/roll"}]}'
    )
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW
    # the report wears the review costume — the existing round machinery
    # (machine reviewer + CHANGES_REQUESTED) returns the defect to the dev
    ((pr_number, body, findings, event),) = scheduler.tester_api.posted
    assert (pr_number, findings, event) == (2, [], "REQUEST_CHANGES")
    assert "Machine test report" in body and "- **bad reply** — /roll" in body
    assert scheduler.tester_api.commented == []

    # next tick observes the review and queues the dev round
    api.issues[1] = _issue(labels=("in-progress",))
    api.reviews[2] = [Review(id=50, author=TESTER_LOGIN, state="CHANGES_REQUESTED", body=body)]
    scheduler.observe()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "review"


def test_preexisting_defects_file_auto_issues(rig, monkeypatch):
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="test")
    )
    api.prs[2] = _pr()
    fake_execute.next_outcome = _assess_outcome(
        '{"verdict": "Old bug found.", "scenarios": [], '
        '"defects": [{"summary": "log signal missing", "repro": "boot", "preexisting": true}], '
        '"issues": [{"title": "bot log lines never reach stdout", "body": "### Summary\\nlogging"}]}'
    )
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW
    # the tester authored it; the platform relayed it verbatim + mechanical provenance
    ((title, body, labels),) = scheduler.tester_api.filed
    assert title == "bot log lines never reach stdout" and labels == ["auto"]
    assert body.startswith("### Summary\nlogging")
    assert "Machine-filed (`auto` provenance)" in body and "`ready`" in body
    # pre-existing only → the report stays a comment, and it links the issue
    ((_, comment),) = scheduler.tester_api.commented
    assert "*(pre-existing)*" in comment and "#101" in comment
    assert scheduler.tester_api.posted == []


def test_auto_issue_dedup_skips_already_open_symptom(rig, monkeypatch):
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="test")
    )
    api.prs[2] = _pr()
    scheduler.tester_api.open_auto = [
        Issue(number=90, title="Bot log lines never reach stdout", body="", labels=["auto"])
    ]
    fake_execute.next_outcome = _assess_outcome(
        '{"verdict": "Same old bug.", "scenarios": [], '
        '"defects": [{"summary": "log signal missing", "repro": "boot", "preexisting": true}], '
        '"issues": [{"title": "bot log lines never reach stdout", "body": "### Summary\\nlogging"}]}'
    )
    scheduler.tick()
    assert scheduler.tester_api.filed == []  # matched case-insensitively — not re-filed
    ((_, comment),) = scheduler.tester_api.commented
    assert "filed as" not in comment.lower()


def test_unresolvable_citation_fails_like_a_malformed_report(rig, monkeypatch):
    """B6's done-when: a report whose citations don't resolve takes the same
    rail as JSON that doesn't parse — retry under the attempts cap, then the
    soft give-up, and nothing is ever posted from the unbacked report."""
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="test")
    )
    api.prs[2] = _pr()
    _tester_transcript(stores, ("toolu_real", "true"))  # the transcript exists…
    fake_execute.next_outcome = _assess_outcome(
        '{"verdict": "Clean.", "scenarios": [{"name": "boot", "result": "pass", '
        '"tool_use_id": "toolu_invented", "evidence": "looks fine"}]}'  # …the citation doesn't
    )
    scheduler.tick()  # attempt 1 → retry
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1
    assert scheduler.tester_api.commented == [] and scheduler.tester_api.posted == []
    scheduler.tick()  # attempt 2 = rails cap → give up on the test, not the task
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.attempts == 0
    assert scheduler.tester_api.commented == [] and scheduler.tester_api.posted == []


def test_test_failure_gives_up_softly_never_blocks(rig, monkeypatch):
    scheduler, api, stores, _, fake_execute = _tester_rig(rig, monkeypatch)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="test")
    )
    api.prs[2] = _pr()
    fake_execute.next_outcome = _outcome(exit_code=1)
    scheduler.tick()  # attempt 1 → retry
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1
    scheduler.tick()  # attempt 2 = rails cap → give up on the test, not the task
    task = stores.ledger.get_task(tid(1))
    assert task.status == TaskStatus.AWAITING_REVIEW and task.attempts == 0
    assert any("machine test could not run" in body for _, body in api.comments)
    assert ("remove", 1, "in-progress") not in api.label_ops  # task is NOT parked


def test_auto_issue_claims_with_tainted_framing(rig):
    # the tester-filed symptom (auto + human-added ready) enters the loop like
    # any task, but the work prompt flips to reproduce-first framing
    scheduler, api, stores, spawned, _ = rig
    auto_issue = Issue(number=13, title="log lines never reach stdout", body="symptom", labels=["ready", "auto"])
    api.ready_issues = [auto_issue]
    api.issues[13] = auto_issue
    scheduler.tick()
    task = stores.ledger.get_task(tid(13))
    assert task is not None and task.status in (TaskStatus.CLAIMED, TaskStatus.WORKING, TaskStatus.AWAITING_REVIEW)
    assert "tainted data" in spawned[0]["prompt"]  # auto provenance framing
    assert "reproduce" in spawned[0]["prompt"]


# --- E1: leases, compare-and-set, and what a peer's presence changes ---

PEER = "elsewhere:1"
FAR_AHEAD = "2099-01-01T00:00:00+00:00"
LONG_AGO = "2020-01-01T00:00:00+00:00"


def test_pick_next_leases_the_task_and_the_tick_releases_it(rig):
    scheduler, api, stores, spawned, _ = rig
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, next_burst_kind="work"))
    picked = scheduler.pick_next()
    assert picked.claimed_by == scheduler.replica and picked.claimed_until is not None
    assert stores.ledger.get_task(tid(1)).claimed_by == scheduler.replica  # the claim is the write

    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    scheduler.run_task_burst(picked)
    done = stores.ledger.get_task(tid(1))
    assert done.status == TaskStatus.AWAITING_REVIEW
    assert (done.claimed_by, done.claimed_until) == (None, None)  # released by the same statement
    assert callable(spawned[0]["heartbeat"])  # and the burst carried its renewal


def test_every_burst_kind_carries_the_heartbeat(rig):
    scheduler, api, stores, spawned, _ = rig
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, pr_number=2, next_burst_kind="assess"))
    api.prs[2] = _pr()
    scheduler.tick()
    assert spawned[-1]["kind"] == "assess" and callable(spawned[-1]["heartbeat"])


def test_the_heartbeat_renews_the_lease_and_reports_a_lost_one(rig):
    scheduler, _, stores, _, _ = rig
    scheduler.rails.lease_s = 0  # renew on every beat
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    task = scheduler.pick_next()
    before = task.claimed_until
    beat = scheduler._heartbeat(task)
    assert beat() is True
    assert stores.ledger.get_task(tid(1)).claimed_until >= before

    # a peer reclaimed the task (our lease lapsed, from its point of view)
    stolen = stores.ledger.get_task(tid(1))
    stolen.claimed_by = PEER
    stores.ledger.upsert_task(stolen)
    assert beat() is False  # the runner fences the burst on this answer

    # a store error is not a lost lease: keep the burst, retry next beat
    def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    scheduler.rails.lease_s = 90  # a real span, so the error is early in it
    beat = scheduler._heartbeat(stores.ledger.get_task(tid(1)))
    stores.ledger.renew_lease = boom
    assert beat() is True


def test_a_lease_the_holder_cannot_renew_fences_before_a_peer_may_absorb(rig, monkeypatch):
    """E2: the lease is the replica's only proof of life. A renewal that keeps
    failing is tolerated for two thirds of the span and fences on the beat after
    — with a third of the lease still to run, so the container is dead before
    the row can lawfully change hands. A renewal that lands resets the clock."""
    scheduler, _, stores, _, _ = rig
    scheduler.rails.lease_s = 90
    scheduler.rails.concurrency = 2  # two tasks in flight below
    clock = {"now": 1000.0}
    monkeypatch.setattr(sched_mod.time, "monotonic", lambda: clock["now"])

    def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    def ledger_down(down):
        if down:
            stores.ledger.renew_lease = boom
        else:
            stores.ledger.__dict__.pop("renew_lease", None)  # the class's own again

    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    beat = scheduler._heartbeat(scheduler.pick_next())
    assert beat() is True  # t=0: renewed for real
    ledger_down(True)
    clock["now"] += 30
    assert beat() is True  # t=30: one failure, 60 s of lease left — keep going
    clock["now"] += 30
    assert beat() is False  # t=60: two thirds unrenewed — fence, 30 s before any peer can move

    # the same span with a renewal landing in the middle: the clock restarts there
    ledger_down(False)
    stores.ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED))
    beat = scheduler._heartbeat(scheduler.pick_next())
    assert beat() is True  # t=0: renewed
    ledger_down(True)
    clock["now"] += 30
    assert beat() is True  # t=30: failed
    ledger_down(False)
    clock["now"] += 30
    assert beat() is True  # t=60: landed — the deadline moves out
    ledger_down(True)
    clock["now"] += 30
    assert beat() is True  # t=90: 30 s since the last landing — tolerated
    clock["now"] += 30
    assert beat() is False  # t=120: 60 s since — fenced


def test_a_task_a_peer_reclaimed_is_not_spawned(rig):
    """Between our claim and our spawn a peer may have taken the task (our
    lease lapsed during a slow clone sync). The claimed → working CAS names our
    lease, so we spawn nothing and write nothing."""
    scheduler, api, stores, spawned, _ = rig
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, next_burst_kind="work"))
    picked = scheduler.pick_next()
    peer_view = stores.ledger.get_task(tid(1))
    peer_view.claimed_by, peer_view.claimed_until = PEER, FAR_AHEAD
    stores.ledger.upsert_task(peer_view)

    api.issues[1] = _issue()
    scheduler.run_task_burst(picked)
    assert spawned == []
    after = stores.ledger.get_task(tid(1))
    assert (after.status, after.claimed_by) == (TaskStatus.CLAIMED, PEER)  # the peer's row, untouched


def test_a_lost_observation_race_writes_nothing_and_speaks_for_nobody(rig):
    """Two replicas observe the same merged PR; the second's CAS finds the task
    already `done` and does not repeat the label edit or the log line — the
    ledger moved first, the side effects belong to whoever moved it."""
    scheduler, api, stores, _, _ = rig
    _awaiting(stores)
    merged = _pr(state="closed", merged=True)

    def peer_wins(number):
        stores.ledger.transition_task(
            tid(1), {"status": TaskStatus.DONE, "next_burst_kind": None},
            expected_status=TaskStatus.AWAITING_REVIEW, expected_holder=None,
        )
        return merged

    api.get_pr = peer_wins
    scheduler.observe()
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.DONE
    assert api.label_ops == [] and api.comments == []


def test_reconcile_leaves_a_peers_live_lease_alone_and_absorbs_a_lapsed_one(rig):
    """A working task under a live lease is a peer's burst on another host —
    docker here cannot see it, and must not judge it. Once the lease lapses the
    peer is presumed dead and the survivor absorbs the task."""
    scheduler, _, stores, _, _ = rig
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.WORKING, next_burst_kind="work", claimed_by=PEER, claimed_until=FAR_AHEAD)
    )
    stores.ledger.record_burst(BurstRecord(burst_id="theirs", task_id=tid(1)))
    scheduler.reconcile_running()
    assert stores.ledger.get_burst("theirs").status == BurstStatus.RUNNING
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.WORKING

    lapsed = stores.ledger.get_task(tid(1))
    lapsed.claimed_until = LONG_AGO
    stores.ledger.upsert_task(lapsed)
    scheduler.reconcile_running()
    assert stores.ledger.get_burst("theirs").status == BurstStatus.KILLED
    absorbed = stores.ledger.get_task(tid(1))
    assert (absorbed.status, absorbed.attempts) == (TaskStatus.CLAIMED, 1)
    assert absorbed.claimed_by is None  # anyone's to claim now, this replica included


def test_reconcile_spares_a_lapsed_lease_whose_container_is_alive_here(rig, monkeypatch):
    """The lease lapsed but the container is running on this host: the work is
    alive, the heartbeat is what failed. Neither the burst nor the task moves."""
    scheduler, _, stores, _, _ = rig
    monkeypatch.setattr(scheduler.runner, "alive", lambda burst_id: burst_id == "mine")
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.WORKING, claimed_by=scheduler.replica, claimed_until=LONG_AGO)
    )
    stores.ledger.record_burst(BurstRecord(burst_id="mine", task_id=tid(1)))
    scheduler.reconcile_running()
    assert stores.ledger.get_burst("mine").status == BurstStatus.RUNNING
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.WORKING


def test_two_replicas_claim_disjoint_tasks(rig):
    scheduler, _, stores, _, _ = rig
    scheduler.rails.concurrency = 2  # the product allows two lanes
    other = Scheduler(stores, scheduler.api, scheduler.repo, OWNER, rails=scheduler.rails, replica=PEER, runner=scheduler.runner)
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, created_at="2026-01-01T00:00:00+00:00"))
    stores.ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED, created_at="2026-01-02T00:00:00+00:00"))
    mine, theirs = scheduler.pick_next(), other.pick_next()
    assert (mine.task_id, theirs.task_id) == (tid(1), tid(2))
    assert (mine.claimed_by, theirs.claimed_by) == (scheduler.replica, PEER)
    assert scheduler.pick_next() is None and other.pick_next() is None


def test_a_second_replica_stands_by_at_the_products_concurrency(rig):
    """E2: the row's concurrency (default 1) is fleet-wide. While one replica
    has the product's burst in flight the other picks nothing — a hot standby,
    not a second lane — and takes over the moment the lane is free."""
    scheduler, _, stores, _, _ = rig
    assert scheduler.rails.concurrency == 1
    other = Scheduler(stores, scheduler.api, scheduler.repo, OWNER, rails=scheduler.rails, replica=PEER, runner=scheduler.runner)
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, created_at="2026-01-01T00:00:00+00:00"))
    stores.ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED, created_at="2026-01-02T00:00:00+00:00"))
    mine = scheduler.pick_next()
    assert mine.task_id == tid(1) and other.pick_next() is None
    scheduler._move(mine, status=TaskStatus.AWAITING_REVIEW, **scheduler._released())
    assert other.pick_next().task_id == tid(2)


# --- E2: the observation turn — one observer per product per interval ---


def _registered(stores, **quotas):
    stores.ledger.register_repository(
        RepositoryRecord(repository=REPOSITORY, team="owners", **quotas), actor="test"
    )


def test_the_observation_turn_is_one_replicas_per_interval(rig):
    scheduler, api, stores, _, _ = rig
    _registered(stores)
    other = Scheduler(stores, api, scheduler.repo, OWNER, rails=scheduler.rails, replica=PEER, runner=scheduler.runner)
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()

    # a zero-length lease lapses at once: the next replica through takes the turn
    scheduler.rails.poll_interval_s = 0
    assert scheduler.observation_turn() is True
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.CLAIMED  # it observed
    scheduler.rails.poll_interval_s = 60
    assert other.observation_turn() is True
    assert stores.ledger.get_repository(REPOSITORY).claimed_by == PEER

    # inside the interval nobody's claim lands — not the holder's either
    api.ready_issues = [_issue(), _issue(2)]
    api.issues[2] = _issue(2)
    assert scheduler.observation_turn() is False
    assert other.observation_turn() is False
    assert stores.ledger.get_task(tid(2)) is None  # so nobody made the GitHub round trip
    assert stores.ledger.get_repository(REPOSITORY).claimed_by == PEER


def test_the_observation_turn_reconciles_too(rig):
    """Reconcile rides the same turn: an orphan is judged once per interval,
    by whoever observes — not by every replica that ticks."""
    scheduler, _, stores, _, _ = rig
    _registered(stores)
    stores.ledger.upsert_task(
        _task(1, status=TaskStatus.WORKING, next_burst_kind="work", claimed_by=PEER, claimed_until=LONG_AGO)
    )
    stores.ledger.record_burst(BurstRecord(burst_id="theirs", task_id=tid(1)))
    assert scheduler.observation_turn() is True
    assert stores.ledger.get_burst("theirs").status == BurstStatus.KILLED
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.CLAIMED


def test_the_single_product_tick_observes_without_a_row(rig):
    """`run --github` may serve an unregistered product (dev mode): its tick
    leases nothing but tasks and observes every time."""
    scheduler, api, stores, _, _ = rig
    assert stores.ledger.get_repository(REPOSITORY) is None
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    api.prs[2] = _pr()
    scheduler.tick()
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.AWAITING_REVIEW
    api.prs[2] = _pr(state="closed", merged=True)
    scheduler.tick()  # the very next tick observes again — no interval to wait out
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.DONE


# --- E3: the kill switch is a row — read per tick, per spawn, per heartbeat ---


def test_a_stop_landing_during_observation_spawns_nothing(rig):
    """Observation is GitHub round trips; the row is read again after them, so a
    `stop` that lands mid-tick is followed by no burst. The observe itself still
    completes — state advanced, nothing spawned."""
    scheduler, api, stores, spawned, _ = rig
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    original = api.list_ready_issues

    def ready_then_stop():
        stores.ledger.stop_org("op", "incident")
        return original()

    api.list_ready_issues = ready_then_stop
    scheduler.tick()
    assert stores.ledger.get_task(tid(1)).status == TaskStatus.CLAIMED  # observed
    assert spawned == []  # not spawned

    stores.ledger.resume_org("op")
    api.list_ready_issues = original
    api.ready_issues = []
    api.open_prs = [_pr()]
    api.prs[2] = _pr()
    scheduler.tick()  # the same process, the next tick — no restart
    assert len(spawned) == 1


def test_the_heartbeat_fences_the_burst_on_the_org_stop(rig):
    """How a `stop` reaches a burst on another host: the row, the heartbeat, the
    runner. Every beat reads the row — not every third of the lease — so the
    container is dead within HEARTBEAT_POLL_S of the verb, wherever it runs."""
    scheduler, _, stores, _, _ = rig
    scheduler.rails.lease_s = 90  # renewals are throttled; the stop read is not
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    task = scheduler.pick_next()
    beat = scheduler._heartbeat(task)
    assert beat() is True
    assert beat() is True  # inside the renewal throttle: still asked, still alive
    stores.ledger.stop_org("op", "incident")
    assert beat() is False  # the very next beat — the runner kills the container
    assert stores.ledger.get_task(tid(1)).claimed_by == scheduler.replica  # the lease is ours to release, later


def test_the_heartbeat_fences_the_burst_on_the_process_shutdown(rig):
    """SIGTERM sets `halt` (cli._serve): the same fence, no ledger write — a
    restarted replica is not a stopped org."""
    scheduler, _, stores, _, _ = rig
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    beat = scheduler._heartbeat(scheduler.pick_next())
    assert beat() is True
    scheduler.halt.set()
    assert beat() is False
    assert stores.ledger.org_stop() is None
    assert scheduler.stopped() is True  # and the tick stands down on the same flag


def test_a_fenced_burst_is_a_killed_attempt(rig):
    """The kill switch's transition (SCHEDULER.md): burst `killed`, the task
    rolled back to `claimed` with one more attempt — the reason says killed,
    not failed, so the trail reads as what happened."""
    scheduler, api, stores, spawned, fake_execute = rig
    scheduler.rails.max_attempts = 1  # so the reason surfaces as the parking comment
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    killed = _outcome(exit_code=137)
    killed.burst.status = BurstStatus.KILLED
    fake_execute.next_outcome = killed
    scheduler.tick()
    task = stores.ledger.get_task(tid(1))
    assert (task.status, task.attempts) == (TaskStatus.BLOCKED, 1)
    assert any("was killed" in body and "fenced" in body for _, body in api.comments)


# --- C4: a product's scheduler sees only its own rows ---

NEIGHBOR = "owner2/neighbor"


def _neighbor_task(stores, number=9, status=TaskStatus.CLAIMED, **kwargs):
    """A neighbor product's task in the same ledger — what the fleet makes normal."""
    task = TaskRecord(
        task_id=task_id_for(NEIGHBOR, number),
        repository=NEIGHBOR,
        issue_number=number,
        status=status,
        **kwargs,
    )
    stores.ledger.upsert_task(task)
    return task


def test_pick_next_never_takes_a_neighbors_task(rig):
    scheduler, _, stores, _, _ = rig
    _neighbor_task(stores)
    assert scheduler.pick_next() is None


def test_observation_never_parks_a_neighbors_task(rig):
    """Pre-C4, `observe` would ask *our* GitHub about the neighbor's issue
    number — same number, wrong repo. The blocked label on our issue 9 must not
    park the neighbor's task 9."""
    scheduler, api, stores, _, _ = rig
    foreign = _neighbor_task(stores, number=9)
    api.issues[9] = Issue(number=9, title="t", body="b", labels=["blocked"])
    scheduler.observe()
    assert stores.ledger.get_task(foreign.task_id).status == TaskStatus.CLAIMED


def test_reconcile_never_fails_a_neighbors_working_task(rig):
    scheduler, api, stores, _, _ = rig
    foreign = _neighbor_task(stores, status=TaskStatus.WORKING)
    scheduler.reconcile_running()
    assert stores.ledger.get_task(foreign.task_id).status == TaskStatus.WORKING
    assert api.comments == []  # and nobody commented on our repo about it


def test_daily_quota_counts_only_our_own_bursts(rig):
    """The neighbor burned bursts today; our product's quota is untouched."""
    scheduler, api, stores, spawned, _ = rig
    scheduler.rails.daily_burst_ceiling = 1
    foreign = _neighbor_task(stores, status=TaskStatus.DONE)
    stores.ledger.record_burst(
        BurstRecord(burst_id="theirs", task_id=foreign.task_id, started_at=utc_now())
    )
    stores.ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    picked = scheduler.pick_next()
    assert picked is not None and picked.task_id == tid(1)


def test_a_turn_is_one_trace_and_a_quiet_tick_is_none(rig, spans, monkeypatch):
    """A tick that only polls is not traced — a trace a minute of nothing — and
    the tick that works a task is one trace, rooted at the turn, with the burst
    started inside it."""
    from opentelemetry import trace

    scheduler, api, stores, spawned, fake_execute = rig
    scheduler.tick()
    assert spans.get_finished_spans() == ()

    current = []

    def inside(*args, **kwargs):
        current.append(trace.get_current_span().name)
        return fake_execute(*args, **kwargs)

    monkeypatch.setattr(sched_mod, "execute_burst", inside)
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    scheduler.tick()
    assert current == ["turn"]
    (turn,) = [span for span in spans.get_finished_spans() if span.name == "turn"]
    assert turn.parent is None
    assert turn.attributes["task.id"] == tid(1)
    assert turn.attributes["burst.kind"] == "work"
