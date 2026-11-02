"""The M2 reconciler (SCHEDULER.md): observe GitHub, advance tasks, run bursts.

Task state lives on ``TaskRecord.status``; labels are reflective only. Serial
by design — one burst globally. Each tick re-derives every decision from
GitHub + the ledger, so a killed or crashed scheduler resumes cleanly.

Every ledger write is one statement and no transaction outlives a call (E1,
TO-BE §5): a task is *claimed* by a lease the claim statement stamps
(`claim_next`), *moved* by compare-and-set on the (status, holder) this
replica read (`_move`), and *kept* by a heartbeat that renews the lease while
the container runs. A replica that dies leaves a lapsed lease — data any peer
reconciles — never a held lock, and a peer's live lease is proof of life from
any host, so two replicas over one ledger cannot double a burst. Observation is
leased the same way (E2): the fleet's tick takes each product's observation
turn through its registry row, so N replicas poll a product once per interval,
and the product's declared concurrency is counted at admission, so N replicas
run as many bursts of it as its row allows — one, by default, the rest standing
by. `tests/test_replicas.py` runs two processes over one Postgres and kills one
mid-burst; the survivor absorbs the work after the lease lapses, exactly once.
The kill switch is a row too (E3): the org's STOP is `org_state`, read at the
top of every tick, before every spawn and on every heartbeat, so a `stop` from
any host stands the whole fleet down within a tick and fences every live burst
within a heartbeat — the runner kills it, wherever it runs. A process's own
shutdown (SIGTERM) is the same fence with a local flag, and no ledger write.
Where a burst runs is a seam (E4): the scheduler holds one `Runner` for the
process — `run` for every spawn, `alive` for the reconciler's proof-of-life
question — and never spells ``docker`` itself. Webhooks are wake-ups (E5): a
delivery the receiver verified becomes `wake_repository` — the product's
observation lease cleared, so the next look polls it now — and every
replica's loop sleeps on a `wake` flag the ledger's channel sets; polls stay
the only truth, so a wake can never cost more than one extra poll.

A ``Scheduler`` is one product's engine — its ledger reads are scoped to its
repository, so two products in one ledger cannot see each other's tasks. The
``Fleet`` (C4) multiplexes N of them through the same serial loop: every tick
observes every active product whose observation lease this replica lands, then
at most one burst runs — the least recently served product goes first.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import structlog

from control_plane.bursts import PlatformStores, abandon_attempt, execute_burst
from control_plane.clone import CloneError
from control_plane.github_api import COPILOT_REVIEWER, COPILOT_TOKEN_ENV, GitHubApi, GitHubApiError, Issue, Review
from control_plane.github_auth import GitHubAuthError
from control_plane.models import (
    EVENT_CAPACITY_EXHAUSTED,
    BurstStatus,
    ProductState,
    RepositoryRecord,
    TaskRecord,
    TaskStatus,
    task_id_for,
    utc_now,
)
from control_plane.prompts import assess_prompt, review_prompt, test_prompt, work_prompt
from control_plane.review import (
    FindingsError,
    ReviewPolicy,
    ReviewPolicyError,
    format_review_body,
    parse_findings,
)
from control_plane.runner import DEFAULT_IMAGE, Runner, open_runner
from control_plane.store import ArchiveError, RegistryError
from control_plane.subscriptions import (
    Capacity,
    CapacityExhausted,
    announce_expiring,
    pick_capacity,
)
from secret_refs import SecretResolver
from control_plane.testing import (
    MANIFEST_FILENAME,
    ManifestError,
    ReportError,
    E2EPolicy,
    E2EPolicyError,
    format_test_report,
    parse_manifest,
    parse_report,
    resolve_citations,
    resolve_test_env,
)
from control_plane.telemetry import turn_span

log = structlog.get_logger()

WORK = "work"
REVIEW = "review"
ASSESS = "assess"  # a reviewer-role burst judging the open PR (REVIEWERS.md)
TEST = "test"  # a tester-role burst running the product in its sandbox (TESTER.md)
MACHINE_STAGES = (ASSESS, TEST)  # additive stages: their failures never block the task

AUTO_PROVENANCE_LABEL = "auto"
AUTO_ISSUES_PER_REPORT = 3  # machine-filed work must converge, not flood


@dataclass(slots=True)
class Rails:
    """Hard limits on unattended behavior. Defaults per SCHEDULER.md."""

    max_attempts: int = 3
    max_review_rounds: int = 3
    daily_burst_ceiling: int = 20
    failure_cooldown_s: int = 300
    poll_interval_s: int = 60
    # how long a replica's claim on a task holds without a heartbeat (E1) —
    # the longest a dead replica's work stays owned before a peer reconciles it.
    # Renewed every third of it while the container runs.
    lease_s: int = 90
    # bursts of the product in flight at once, fleet-wide (E2) — the registry
    # row's `concurrency`, which the fleet copies in here per product the way it
    # does `daily_bursts`. No env knob: `--github` runs one product from one
    # process, and 1 is that loop.
    concurrency: int = 1
    # per-burst budget and turns left this class at D5: they are the role row's
    # (`max_budget_usd`, `max_turns`), read by execute_burst at every spawn

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Rails:
        env = dict(os.environ) if environ is None else environ
        defaults = cls()

        def _get(name: str, cast, fallback):
            raw = env.get(name)
            return cast(raw) if raw else fallback

        return cls(
            max_attempts=_get("SCHEDULER_MAX_ATTEMPTS", int, defaults.max_attempts),
            max_review_rounds=_get("SCHEDULER_MAX_REVIEW_ROUNDS", int, defaults.max_review_rounds),
            daily_burst_ceiling=_get("SCHEDULER_DAILY_BURST_CEILING", int, defaults.daily_burst_ceiling),
            failure_cooldown_s=_get("SCHEDULER_FAILURE_COOLDOWN_S", int, defaults.failure_cooldown_s),
            poll_interval_s=_get("SCHEDULER_POLL_INTERVAL_S", int, defaults.poll_interval_s),
            lease_s=_get("SCHEDULER_LEASE_S", int, defaults.lease_s),
        )


def default_replica() -> str:
    """Who this process is in `tasks.claimed_by`: host and pid. Unique per live
    process with nothing to configure, and readable in the row — a lapsed
    lease says which host to look at."""
    return f"{socket.gethostname()}:{os.getpid()}"


def standing_down(ledger, halt: threading.Event) -> bool:
    """Whether a loop acts at all, asked at the top of every tick and again
    before every spawn (E3). Two reasons, one answer: this process is shutting
    down — `halt`, set by `_serve` on SIGTERM/SIGINT, the process's own and
    written nowhere — or the org is stopped, the `org_state` row, one for the
    whole ledger. A `stop` from any replica on any host therefore lands on
    every loop within a tick; the row has no expiry, so it holds until an
    operator lifts it, and the loop keeps ticking meanwhile — reading the row,
    doing nothing — so `resume` needs no restart anywhere."""
    if halt.is_set():
        return True
    stop = ledger.org_stop()
    if stop is None:
        return False
    log.info("org STOP in force — standing down", reason=stop.reason, by=stop.stopped_by, since=stop.stopped_at)
    return True


class Scheduler:
    def __init__(
        self,
        stores: PlatformStores,
        api: GitHubApi,
        repo: Path,
        owner: str,
        rails: Rails | None = None,
        review: ReviewPolicy | None = None,
        testing: E2EPolicy | None = None,
        reviewer_api: GitHubApi | None = None,
        reviewer_login: str | None = None,
        tester_api: GitHubApi | None = None,
        tester_login: str | None = None,
        secrets: SecretResolver | None = None,
        image: str = DEFAULT_IMAGE,
        sync_clone: Callable[[TaskRecord], Path] | None = None,
        sync_clone_ref: Callable[[TaskRecord, str], Path] | None = None,
        replica: str | None = None,
        halt: threading.Event | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.stores = stores
        self.api = api
        self.repo = repo  # the platform-owned clone (issue #4), never the operator's; the fallback without sync_clone
        self.owner = owner  # the human whose changes-requested reviews trigger rounds
        self.rails = rails or Rails()
        self.review = review or ReviewPolicy()  # ships off (REVIEWERS.md)
        self.testing = testing or E2EPolicy()  # ships off (TESTER.md)
        self.reviewer_api = reviewer_api  # posts the claude provider's findings (second App)
        self.reviewer_login = reviewer_login  # that App's bot login, learned at startup
        self.tester_api = tester_api  # posts test reports + auto issues (the tester App, D3)
        self.tester_login = tester_login
        self.secrets = secrets or SecretResolver()  # resolves sandbox refs at spawn (D4)
        self.machine_reviewers = set(self.review.machine_reviewers())
        if self.review.claude and reviewer_login:
            self.machine_reviewers.add(reviewer_login)
        if self.testing.enabled and tester_login:
            # the tester's REQUEST_CHANGES (pr-caused defects) must return the
            # task to the developer through the same round machinery
            self.machine_reviewers.add(tester_login)
        self.image = image
        # model, budget and turns are the role row's (D5) — execute_burst reads
        # them at spawn; the scheduler no longer carries a model at all
        # re-syncs the task's own tree of the platform clone (H3); called before every burst
        self.sync_clone = sync_clone
        self.sync_clone_ref = sync_clone_ref  # same, to a PR head sha (assess and test bursts)
        self.replica = replica or default_replica()  # the name on this replica's leases (E1)
        # the process's shutdown flag (E3): `_serve` sets it on SIGTERM/SIGINT,
        # the tick reads it, the heartbeat fences on it. A fleet shares one across
        # every scheduler it builds
        self.halt = halt if halt is not None else threading.Event()
        # the loop's wake flag (E5): `_serve` sleeps on it; a webhook's wake — this
        # process's endpoint or a peer's, through the ledger's channel — sets it
        self.wake = threading.Event()
        # where this replica's bursts run (E4): the one runner the process has,
        # asked to `run` every burst and, by the reconciler, whether one is `alive`.
        # The archive goes with it because the bytes phases are the runner's (G1) —
        # a Job runner mints its pod's URLs from it (G3) and refuses without one
        self.runner = runner if runner is not None else open_runner(self.stores.archive)
        self.current_burst_id: str | None = None  # the one live burst, if any

    # --- the tick ---

    def stopped(self) -> bool:
        """`standing_down` for this loop: the process's flag or the org's row."""
        return standing_down(self.stores.ledger, self.halt)

    def tick(self) -> None:
        """The single-product loop (`run --github`): one process, one product,
        possibly no registry row (dev mode) — so it observes every tick and
        leases nothing but its tasks. The fleet's tick is the multi-replica one:
        it takes `observation_turn` per product instead."""
        if self.stopped():
            return
        announce_expiring(self.stores.ledger)  # the pool's housekeeping (F4), as the fleet's tick does
        self.reconcile_running()
        self.observe()
        if self.stopped():  # `stop` may have landed during observation
            return
        task = self.pick_next()
        if task is not None:
            self.run_task_burst(task)

    def observation_turn(self) -> bool:
        """Reconcile and observe the product, if this replica's claim on its
        observation lease lands (E2). The lease runs one poll interval, so a
        fleet of N observes a product once per interval — one set of GitHub
        calls, one reconcile — and a peer's live lease means this replica skips
        the product this tick. A replica that dies holding it is waited out, at
        most one interval; the observe itself is idempotent by construction
        (every state change it makes is a CAS), so the lease is a rate, not a
        correctness wall. False: a peer has this turn — the normal state of every
        replica but one, so nothing is logged; the row's `claimed_by` says whose."""
        if not self.stores.ledger.claim_observation(
            self.api.repo, self.replica, self.rails.poll_interval_s
        ):
            return False
        self.reconcile_running()
        self.observe()
        return True

    # --- the ledger writes (E1) ---

    def _move(self, task: TaskRecord, **changes) -> bool:
        """The one way this class moves a task: compare-and-set against the
        (status, holder) the record was read with, writing only `changes`. The
        record is refreshed from the row as written. False means the row had
        moved — a peer got there first — and nothing was written; callers stop
        acting on a task that is no longer theirs to act on."""
        written = self.stores.ledger.transition_task(
            task.task_id, changes, expected_status=task.status, expected_holder=task.claimed_by
        )
        if written is None:
            log.warning(
                "task moved under us — a peer got there first, nothing written",
                task=task.task_id, expected=task.status, wanted=changes.get("status", task.status),
            )
            return False
        for name, value in asdict(written).items():
            setattr(task, name, value)
        return True

    @staticmethod
    def _released() -> dict:
        """The lease columns cleared — every transition that ends a burst
        carries them, so a task never sits `claimed` under a stale lease."""
        return {"claimed_by": None, "claimed_until": None}

    def _heartbeat(self, task: TaskRecord) -> Callable[[], bool]:
        """The lease renewal the runner asks while the container runs (E1). Asked
        every few seconds; renews on the first beat — the claim's stamp is as old
        as the clone sync took — and every third of the lease after. False — the
        lease is lost, a peer took the task — makes the runner fence the burst.

        A ledger error is not a lost lease: the burst keeps going and the next
        beat retries, because killing live work on a database blip is the worse
        mistake — up to a point (E2). The lease is this replica's only proof of
        life to its peers, and once it lapses a peer may absorb the task; so a
        renewal that has not landed for two thirds of the lease fences the burst
        while a third still runs, and the container is dead before anyone may
        legally start its successor. Waiting for the full span would fence
        *after* a peer could have spawned — the doubled burst the lease exists
        to prevent.

        The same answer is the kill switch (E3): every beat — not every third
        of the lease — reads the org's STOP row and the process's own shutdown
        flag, and False on either makes the runner kill the container, wherever
        it runs. That is how a `stop` issued on one host reaches a burst on
        another: through the ledger, to the heartbeat, to the runner holding
        it, within `HEARTBEAT_POLL_S`."""
        every = self.rails.lease_s / 3
        last = float("-inf")
        renewed_at = time.monotonic()  # the claim's own stamp, as of this replica's clock

        def beat() -> bool:
            nonlocal last, renewed_at
            if self.halt.is_set():
                log.warning("replica shutting down — fencing the burst", task=task.task_id)
                return False
            now = time.monotonic()
            try:
                stop = self.stores.ledger.org_stop()
                if stop is not None:
                    log.warning("org STOP — fencing the burst", task=task.task_id,
                                reason=stop.reason, by=stop.stopped_by)
                    return False
                if now - last < every:
                    return True
                last = now
                renewed = self.stores.ledger.renew_lease(task.task_id, self.replica, self.rails.lease_s)
            except Exception as exc:  # noqa: BLE001 — any store error; see the docstring
                if now - renewed_at >= self.rails.lease_s - every:
                    log.error("lease unrenewable for most of its span — fencing before a peer can absorb",
                              task=task.task_id, error=str(exc))
                    return False
                log.warning("ledger unreachable from the heartbeat — keeping the burst, retrying next beat",
                            task=task.task_id, error=str(exc))
                return True
            if renewed:
                renewed_at = now
            else:
                log.error("lease lost — a peer holds the task now", task=task.task_id)
            return renewed

        return beat

    # --- crash recovery ---

    def reconcile_running(self) -> None:
        """A running burst or a working task that no live lease covers is a
        replica that died mid-burst (E1). A live lease is a peer's proof of life
        from any host and is never touched; a burst this replica's runner can
        still see running is proof of life too — the lease lapsed, the work did
        not (a heartbeat that could not reach the ledger) — and its task is left
        alone."""
        repo = self.api.repo
        alive: set[str] = set()
        for burst in self.stores.ledger.running_bursts(repository=repo, orphaned=True):
            if self.runner.alive(burst.burst_id):
                alive.add(burst.task_id)
                continue
            burst.status = BurstStatus.KILLED
            burst.finished_at = utc_now()
            self.stores.ledger.finish_burst(burst)
            log.warning("burst died without a result — marked killed", burst=burst.burst_id)
        # a WORKING task nobody holds is the same crash, seen from the task side
        for task in self.stores.ledger.tasks_by_status(
            TaskStatus.WORKING, repository=repo, orphaned=True
        ):
            if task.task_id in alive:
                continue
            self._register_failure(task, "the scheduler crashed or was killed mid-burst")

    # --- observation: GitHub facts → state transitions ---

    def observe(self) -> None:
        self._claim_ready_issues()
        self._reconcile_active_issues()
        self._advance_awaiting_review()

    def _claim_ready_issues(self) -> None:
        for issue in self.api.list_ready_issues():
            # by the derived id, the same one _new_task would compute: a lookup that
            # spells the key differently from the write finds nothing and re-claims
            # an issue the platform already holds
            task = self.stores.ledger.get_task(task_id_for(self.api.repo, issue.number))
            if task is not None and task.status in (
                TaskStatus.CLAIMED,
                TaskStatus.WORKING,
                TaskStatus.AWAITING_REVIEW,
            ):
                continue  # already ours; the label is drift
            kind = WORK
            if task is not None and task.pr_number:  # re-readied with a PR still open → resume the review path
                pr = self.api.get_pr(task.pr_number)
                if pr.state == "open" and not pr.merged:
                    kind = REVIEW
            if task is None:
                task = self._new_task(issue.number)
                task.status = TaskStatus.CLAIMED
                task.next_burst_kind = kind
                if not self.stores.ledger.create_task(task):
                    continue  # a peer claimed it between our lookup and our insert
            elif not self._move(
                task,
                status=TaskStatus.CLAIMED,
                attempts=0,  # a human re-`ready` is a fresh grant
                review_rounds=0,
                next_burst_kind=kind,
                **self._released(),
            ):
                continue
            self.api.remove_label(issue.number, "ready")
            self.api.add_labels(issue.number, ["in-progress"])
            log.info(
                "issue claimed",
                issue=issue.number,
                provenance=issue.provenance,
                burst=task.next_burst_kind,
            )

    def _new_task(self, issue_number: int) -> TaskRecord:
        """A task row for an issue we just claimed.

        The id is derived from the repository slug and the issue number, never
        invented (A6): `self.api.repo` is 'owner/name', so two replicas claiming
        the same issue compute the same id and the one-task-per-issue index turns
        the race into a constraint violation rather than two tasks. The clone path
        deliberately does not appear — it is a fact about this machine, passed to
        each burst as an argument.
        """
        return TaskRecord(
            task_id=task_id_for(self.api.repo, issue_number),
            repository=self.api.repo,
            issue_number=issue_number,
        )

    def _reconcile_active_issues(self) -> None:
        """One read of each active task's issue, two facts it can carry: the
        agent set `blocked` (contract rule 9) — stop scheduling the task; or the
        issue is closed — the task is finished (#93), whoever closed it.

        The closed rule is for `claimed` tasks only. An `awaiting_review` task
        has an open PR, and the PR is what ends it — a merge closes the issue
        through `Closes #<n>`, and "pr merged" is the truer reason to record."""
        for task in self.stores.ledger.tasks_by_status(
            TaskStatus.CLAIMED, TaskStatus.AWAITING_REVIEW, repository=self.api.repo
        ):
            issue = self.api.get_issue(task.issue_number)
            if task.status == TaskStatus.CLAIMED and self._finish_if_issue_closed(task, issue):
                continue
            if "blocked" in issue.labels and self._move(
                task, status=TaskStatus.BLOCKED, next_burst_kind=None, **self._released()
            ):
                log.info("issue carries `blocked` — parked for a human", issue=task.task_id)

    def _finish_if_issue_closed(self, task: TaskRecord, issue: Issue | None = None) -> bool:
        """A closed issue ends its task (#93).

        Some work needs no PR — the symptom was already fixed, the report is a
        duplicate — and the burst says so by closing the issue with its
        evidence, which the product's contract allows; a human can close it
        mid-flight for the same reasons. Scoring that as "no PR, retry" spends
        the remaining attempts re-reading a closed issue: the gate rehearsal
        (#37) ran three bursts on discord-bot#16 for a task the first one
        finished. True means the issue is closed, whether or not this replica
        wrote the transition — either way the caller registers no failure."""
        issue = self.api.get_issue(task.issue_number) if issue is None else issue
        if issue.state != "closed":
            return False
        if self._move(task, status=TaskStatus.DONE, next_burst_kind=None, **self._released()):
            self.api.remove_label(task.issue_number, "in-progress")
            log.info("issue closed — task done", task=task.task_id, issue=task.issue_number)
        return True

    def _advance_awaiting_review(self) -> None:
        for task in self.stores.ledger.tasks_by_status(
            TaskStatus.AWAITING_REVIEW, repository=self.api.repo
        ):
            if not task.pr_number:
                continue
            number = task.issue_number
            pr = self.api.get_pr(task.pr_number)
            if pr.merged:
                if self._move(task, status=TaskStatus.DONE, next_burst_kind=None):
                    self.api.remove_label(number, "in-progress")
                    log.info("pr merged — task done", task=task.task_id, pr=task.pr_number)
                continue
            if pr.state == "closed":
                if self._move(task, status=TaskStatus.BLOCKED, next_burst_kind=None):
                    self.api.comment(number, "⛔ control plane: PR closed without merge — parking this task for a human.")
                    self.api.remove_label(number, "in-progress")
                    log.warning("pr closed unmerged — task blocked", task=task.task_id, pr=task.pr_number)
                continue
            pending, owner_triggered = self._actionable_reviews(task)
            if not pending:
                continue
            if task.review_rounds >= self.rails.max_review_rounds:
                if owner_triggered:
                    if self._move(task, status=TaskStatus.BLOCKED, next_burst_kind=None):
                        self.api.comment(
                            number,
                            f"⛔ control plane: parking this task after {task.review_rounds} review rounds — "
                            "a human can re-add `ready` to grant more.",
                        )
                        self.api.remove_label(number, "in-progress")
                # machine-review rounds cap softly: stop chasing, the human gate decides
                elif self._move(task, last_review_id=max(review.id for review in pending)):
                    self.api.comment(
                        number,
                        f"ℹ️ control plane: review-round cap ({task.review_rounds}) reached — "
                        "remaining reviewer findings are left for the owner's review.",
                    )
                continue
            if self._move(
                task, status=TaskStatus.CLAIMED, next_burst_kind=REVIEW, review_rounds=task.review_rounds + 1
            ):
                trigger = "changes requested" if owner_triggered else "reviewer findings"
                log.info("review round queued", pr=task.pr_number, trigger=trigger, round=task.review_rounds)

    def _actionable_reviews(self, task: TaskRecord) -> tuple[list[Review], bool]:
        """(reviews to address, owner_triggered) above the processed cursor.

        The cursor (last_review_id) advances only after a review burst
        succeeds, so failed rounds re-surface the same reviews. Two triggers:
        the owner's CHANGES_REQUESTED, and a registered machine reviewer's
        CHANGES_REQUESTED *or* review carrying inline findings — the reviewer
        App requests changes exactly when it has findings, but its body-only
        422 fallback loses the inline comments (state survives), and Copilot
        never requests changes (COMMENTED by design), so neither signal alone
        covers both. Approvals and overview-only reviews trigger nothing, and
        reviewers outside the policy's registry are ignored entirely.
        """
        reviews = self.api.list_reviews(task.pr_number)
        last = task.last_review_id or 0
        unseen = [review for review in reviews if review.id > last]
        owner = [
            review
            for review in unseen
            if review.author == self.owner and review.state == "CHANGES_REQUESTED"
        ]
        machine = [review for review in unseen if review.author in self.machine_reviewers]
        if machine:
            with_findings = {
                comment.review_id for comment in self.api.list_review_comments(task.pr_number)
            }
            machine = [
                review
                for review in machine
                if review.state == "CHANGES_REQUESTED" or review.id in with_findings
            ]
        return owner + machine, bool(owner)

    # --- spawning ---

    def pick_next(self) -> TaskRecord | None:
        """Admission (TO-BE §5): the rails and the claim, as one ledger call —
        the oldest eligible `claimed` task comes back *leased to this replica*
        by the statement that picked it, or nothing does. The rails are the
        statement's WHERE (cooldown) and its fence (the daily ceiling, the
        product's concurrency); the day boundary is the one thing computed
        here, because it is a calendar fact, not a comparison."""
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self.stores.ledger.claim_next(
            self.api.repo,
            self.replica,
            ceiling=self.rails.daily_burst_ceiling,
            since=day_start,
            lease_s=self.rails.lease_s,
            cooldown_s=self.rails.failure_cooldown_s,
            concurrency=self.rails.concurrency,
        )

    def _capacity(self, task: TaskRecord) -> Capacity | None:
        """Who pays for this burst (F2) — or None, and the task stays where it is.

        The pool is asked once per burst, before anything is synced, minted or
        spawned. Nothing eligible is not a failed attempt: nothing was tried, so
        the task keeps its attempts and its `claimed` status and only loses the
        lease — the next replica to pass, or this one after the next window,
        picks it up unchanged. The `capacity_exhausted` event is how an adopter
        hears about it, because capacity is a people question (TO-BE §4): the
        answer is another member opting in, not a retry."""
        try:
            return pick_capacity(self.stores.ledger)
        except CapacityExhausted as exc:
            log.warning("the pool has nothing to give — idling", task=task.task_id, detail=str(exc))
            self.stores.ledger.record_event(
                EVENT_CAPACITY_EXHAUSTED,
                {"repository": self.api.repo, "task": task.task_id, "detail": str(exc)},
            )
            self._move(task, **self._released())  # unleased, still claimed, attempts untouched
            return None

    def run_task_burst(self, task: TaskRecord, capacity: Capacity | None = None) -> None:
        """One turn for the task: at most one burst, and what the platform does
        around it. The turn is the trace (§9), so a turn that ends before any
        burst — a clone that would not sync, a review that vanished — is still
        one a reader can find."""
        with turn_span(task.task_id, task.next_burst_kind or WORK, self.api.repo):
            self._run_turn(task, capacity)

    def _run_turn(self, task: TaskRecord, capacity: Capacity | None) -> None:
        number = task.issue_number
        kind = task.next_burst_kind or WORK
        # the fleet picks once for the whole tick (H1) and hands it down; the
        # single-product loop has no fleet, so it asks here
        if capacity is None:
            capacity = self._capacity(task)
            if capacity is None:
                return
        if kind == ASSESS:
            self._run_assess_burst(task, capacity)
            return
        if kind == TEST:
            self._run_test_burst(task, capacity)
            return
        repo = self.repo
        if self.sync_clone is not None:
            try:
                repo = self.sync_clone(task)
            except CloneError as exc:
                self._register_failure(task, f"platform clone sync failed: {exc}")
                return
        pending: list[Review] = []
        owner_triggered = False
        head_before = ""
        if kind == WORK:
            prompt = work_prompt(self.api.get_issue(number))
        else:
            pending, owner_triggered = self._actionable_reviews(task)
            if not pending:  # review dismissed/vanished between ticks
                self._move(task, status=TaskStatus.AWAITING_REVIEW, **self._released())
                return
            prompt = review_prompt(task.pr_number, pending, self.api.list_review_comments(task.pr_number))
            head_before = self.api.get_pr(task.pr_number).head_sha

        # claimed → working, guarded by our lease: a peer that reclaimed the task
        # while we synced the clone owns it now, and we spawn nothing
        if not self._move(task, status=TaskStatus.WORKING, last_burst_at=utc_now()):
            return
        self.current_burst_id = uuid.uuid4().hex[:12]
        try:
            outcome = execute_burst(
                self.stores,
                task,
                repo,
                prompt,
                kind=kind,
                with_github=True,  # dev bursts push branches and open PRs
                image=self.image,
                burst_id=self.current_burst_id,
                heartbeat=self._heartbeat(task),
                runner=self.runner,
                capacity=capacity,
            )
        except (GitHubAuthError, GitHubApiError) as exc:
            log.error("burst spawn failed", error=str(exc))
            self._register_failure(task, f"burst could not start: {exc}")
            return
        except ArchiveError as exc:
            # the session tree let go of a node: the snapshot is gone, or its
            # bytes disagree with the checksum the ledger recorded (#87). One
            # missing object used to take the replica down — and then its peer,
            # which resolves the same leaf — so this is an attempt, not a crash
            log.error("session snapshot unusable", task=task.task_id, error=str(exc))
            self._register_failure(task, f"session snapshot unusable: {exc}")
            return
        except RegistryError as exc:
            # the ledger refused the task on the way in — the product left the
            # registry between this tick's read and the spawn (#90). Same rule as
            # every other refusal here: the attempt fails, the replica lives
            log.error("the ledger refused the task", task=task.task_id, error=str(exc))
            self._register_failure(task, str(exc))
            return
        finally:
            self.current_burst_id = None

        if not outcome.succeeded:
            if not self._spent_token(task, outcome):
                if outcome.spent_its_allowance:
                    # retry ≠ branch (H5): the approach ran to the end of its
                    # turns or budget, so the next attempt grows from the node
                    # this one grew from, and does not carry its tail
                    abandon_attempt(
                        self.stores.ledger, task, outcome,
                        "it spent what it was given without delivering",
                    )
                self._register_failure(task, self._burst_ended_badly(outcome))
            return

        queue_assess = False
        changes: dict = {}
        if kind == WORK:
            pr = self.api.open_pr_with_branch_prefix(f"dev/{number}-")
            if pr is None:
                # the work burst's contract is a PR; anything less is a failed
                # attempt — unless the burst closed the issue instead, which
                # finishes the task (#93) rather than failing an attempt of it
                if self._finish_if_issue_closed(task):
                    return
                # the engine said it finished and there is nothing to show for
                # it — the approach's failure, not the environment's (H5)
                abandon_attempt(self.stores.ledger, task, outcome, "it finished without opening a pull request")
                self._register_failure(task, f"burst {outcome.burst.burst_id} succeeded but opened no PR")
                return
            task.pr_number = pr.number  # the re-review below reads it; the CAS writes it
            changes["pr_number"] = pr.number
            log.info("pr opened", task=task.task_id, pr=pr.number)
            # fire every active provider (REVIEWERS.md); the owner sees the PR exists
            self._request_re_review(task, [*self._active_request_providers(), self.owner])
            queue_assess = self.review.claude
        else:
            changes["last_review_id"] = max(review.id for review in pending)
            pushed = self.api.get_pr(task.pr_number).head_sha != head_before
            if owner_triggered:
                self._request_re_review(task, [*self._active_request_providers(), self.owner])
            elif pushed:
                # autonomous reviewer round: fresh diff → re-review; no owner ping
                self._request_re_review(task, self._active_request_providers())
            # reply-only round: threads answered, same diff — re-reviewing would loop
            queue_assess = self.review.claude and pushed
        if queue_assess:
            changes.update(status=TaskStatus.CLAIMED, next_burst_kind=ASSESS)
            log.info("machine review queued", pr=task.pr_number)
        else:
            changes.update(status=TaskStatus.AWAITING_REVIEW, next_burst_kind=None)
        self._move(task, **changes, **self._released())

    @staticmethod
    def _burst_ended_badly(outcome, kind: str = WORK) -> str:
        """The attempt's reason: a burst the runner fenced — the lease lost, the
        org stopped, the replica shutting down — is `killed` (E3), and reads so
        in the trail; anything else that came back non-zero failed on its own."""
        what = "burst" if kind == WORK else f"{kind} burst"
        if outcome.burst.status == BurstStatus.KILLED:
            return f"{what} {outcome.burst.burst_id} was killed — fenced by its heartbeat (see its log)"
        return f"{what} {outcome.burst.burst_id} failed (see its log)"

    def _run_assess_burst(self, task: TaskRecord, capacity: Capacity) -> None:
        """A reviewer-role burst judging the open PR (REVIEWERS.md): fresh
        session, no GitHub credential; findings return via the harvest and the
        control plane posts them as the reviewer App."""
        pr = self.api.get_pr(task.pr_number)
        if pr.merged or pr.state != "open":
            # resolved while queued — nothing to judge
            self._move(task, status=TaskStatus.AWAITING_REVIEW, next_burst_kind=None, **self._released())
            return
        repo = self.repo
        if self.sync_clone_ref is not None:
            try:
                repo = self.sync_clone_ref(task, pr.head_sha)
            except CloneError as exc:
                self._register_failure(task, f"platform clone sync failed: {exc}")
                return

        if not self._move(task, status=TaskStatus.WORKING, last_burst_at=utc_now()):
            return
        self.current_burst_id = uuid.uuid4().hex[:12]
        try:
            outcome = execute_burst(
                self.stores,
                task,
                repo,
                assess_prompt(task.pr_number, pr.head_sha),
                kind=ASSESS,
                role="reviewer",
                fresh=True,  # deliberately amnesiac — no shared context with the dev
                image=self.image,
                burst_id=self.current_burst_id,
                heartbeat=self._heartbeat(task),
                runner=self.runner,
                capacity=capacity,
            )
        except (GitHubAuthError, GitHubApiError) as exc:
            log.error("burst spawn failed", error=str(exc))
            self._register_failure(task, f"burst could not start: {exc}")
            return
        except ArchiveError as exc:
            # the session tree let go of a node: the snapshot is gone, or its
            # bytes disagree with the checksum the ledger recorded (#87). One
            # missing object used to take the replica down — and then its peer,
            # which resolves the same leaf — so this is an attempt, not a crash
            log.error("session snapshot unusable", task=task.task_id, error=str(exc))
            self._register_failure(task, f"session snapshot unusable: {exc}")
            return
        except RegistryError as exc:
            # the ledger refused the task on the way in — the product left the
            # registry between this tick's read and the spawn (#90). Same rule as
            # every other refusal here: the attempt fails, the replica lives
            log.error("the ledger refused the task", task=task.task_id, error=str(exc))
            self._register_failure(task, str(exc))
            return
        finally:
            self.current_burst_id = None

        if not outcome.succeeded:
            if not self._spent_token(task, outcome):
                self._register_failure(task, self._burst_ended_badly(outcome, ASSESS))
            return
        try:
            parsed = parse_findings((outcome.harvest.result or {}).get("final_text") or "")
        except FindingsError as exc:
            self._register_failure(task, f"reviewer output unusable: {exc}")
            return
        model = (outcome.harvest.result or {}).get("model")  # resolved, not requested
        self._post_assessment(task, parsed, head_sha=pr.head_sha, model=model)
        # the funnel (TESTER.md): only a clean assess queues the sandbox —
        # no boot for a diff that findings are about to change
        if not parsed.findings and self._test_queueable(task, repo):
            changes = {"status": TaskStatus.CLAIMED, "next_burst_kind": TEST}
            log.info("machine test queued", pr=task.pr_number)
        else:
            changes = {"status": TaskStatus.AWAITING_REVIEW, "next_burst_kind": None}
        # attempts=0: machine-stage retries must not poison later dev rounds
        self._move(task, **changes, attempts=0, **self._released())

    def _test_queueable(self, task: TaskRecord, repo: Path) -> bool:
        """Whether the tester can plug in (TESTER.md): policy on, the product
        ships a parseable TESTING.md, and every declared credential exists
        platform-side. Anything short is a logged or commented skip, never a
        failure — tests are additive."""
        if not self.testing.enabled:
            return False
        manifest_path = repo / MANIFEST_FILENAME
        if not manifest_path.is_file():
            log.info("product not testable — skipping", missing=MANIFEST_FILENAME)
            return False
        try:
            manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
        except ManifestError as exc:
            self._skip_test(task, str(exc))
            return False
        _, missing = resolve_test_env(manifest, self.testing.refs_for(manifest), self.secrets)
        if missing:
            self._skip_test(
                task, f"sandbox credentials not provisioned platform-side: {', '.join(missing)}"
            )
            return False
        return True

    def _skip_test(self, task: TaskRecord, reason: str) -> None:
        log.info("machine test skipped", task=task.task_id, reason=reason)
        try:
            self.api.comment(
                task.issue_number,
                f"ℹ️ control plane: machine test could not run ({reason}).",
            )
        except GitHubApiError as exc:
            log.warning("skip comment failed — advisory, continuing", error=str(exc))

    def _run_test_burst(self, task: TaskRecord, capacity: Capacity) -> None:
        """A tester-role burst (TESTER.md): boots the product per its own
        TESTING.md, sandbox credentials injected as the declared-ref
        intersection (resolved at spawn, D4); the report returns via the
        harvest and posts as a PR comment."""
        pr = self.api.get_pr(task.pr_number)
        if pr.merged or pr.state != "open":
            # resolved while queued — nothing to test
            self._move(task, status=TaskStatus.AWAITING_REVIEW, next_burst_kind=None, **self._released())
            return
        repo = self.repo
        if self.sync_clone_ref is not None:
            try:
                repo = self.sync_clone_ref(task, pr.head_sha)
            except CloneError as exc:
                self._register_failure(task, f"platform clone sync failed: {exc}")
                return
        # re-read at spawn time — the synced head owns the manifest's truth
        try:
            manifest = parse_manifest((repo / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        except (OSError, ManifestError) as exc:
            self._register_failure(task, f"test manifest unusable: {exc}")
            return
        sandbox_env, missing = resolve_test_env(
            manifest, self.testing.refs_for(manifest), self.secrets
        )
        if missing:
            self._register_failure(task, f"sandbox credentials missing: {', '.join(missing)}")
            return

        if not self._move(task, status=TaskStatus.WORKING, last_burst_at=utc_now()):
            return
        self.current_burst_id = uuid.uuid4().hex[:12]
        try:
            outcome = execute_burst(
                self.stores,
                task,
                repo,
                test_prompt(task.pr_number, pr.head_sha, manifest.prose),
                kind=TEST,
                role="tester",
                fresh=True,  # no shared context with the dev or the reviewer
                image=self.image,
                burst_id=self.current_burst_id,
                extra_env=sandbox_env,  # the resolved intersection — tester bursts only
                egress=manifest.egress,  # its sandbox hosts, declared beside the refs (G6)
                heartbeat=self._heartbeat(task),
                runner=self.runner,
                capacity=capacity,
            )
        except (GitHubAuthError, GitHubApiError) as exc:
            log.error("burst spawn failed", error=str(exc))
            self._register_failure(task, f"burst could not start: {exc}")
            return
        except ArchiveError as exc:
            # the session tree let go of a node: the snapshot is gone, or its
            # bytes disagree with the checksum the ledger recorded (#87). One
            # missing object used to take the replica down — and then its peer,
            # which resolves the same leaf — so this is an attempt, not a crash
            log.error("session snapshot unusable", task=task.task_id, error=str(exc))
            self._register_failure(task, f"session snapshot unusable: {exc}")
            return
        except RegistryError as exc:
            # the ledger refused the task on the way in — the product left the
            # registry between this tick's read and the spawn (#90). Same rule as
            # every other refusal here: the attempt fails, the replica lives
            log.error("the ledger refused the task", task=task.task_id, error=str(exc))
            self._register_failure(task, str(exc))
            return
        finally:
            self.current_burst_id = None

        if not outcome.succeeded:
            if not self._spent_token(task, outcome):
                self._register_failure(task, self._burst_ended_badly(outcome, TEST))
            return
        try:
            report = parse_report((outcome.harvest.result or {}).get("final_text") or "")
            # B6: every scenario's tool_use_id must resolve against the burst's own
            # transcript — the volume just harvested, byte-identical to the archived
            # snapshot (B5). CitationError is a ReportError on purpose: an unbacked
            # claim fails exactly like JSON that didn't parse — same retry rail,
            # same "machine test could not run" at the cap, nothing new blocks.
            citations = resolve_citations(
                report, self.stores.data_dir / "volumes" / outcome.burst.burst_id
            )
        except ReportError as exc:
            self._register_failure(task, f"tester output unusable: {exc}")
            return
        model = (outcome.harvest.result or {}).get("model")  # resolved, not requested
        filed = self._file_auto_issues(task, report, head_sha=pr.head_sha)
        self._post_test_report(
            task, report, head_sha=pr.head_sha, model=model, filed=filed, citations=citations
        )
        # attempts=0: machine-stage retries must not poison later dev rounds
        self._move(
            task, status=TaskStatus.AWAITING_REVIEW, next_burst_kind=None, attempts=0, **self._released()
        )

    def _file_auto_issues(self, task: TaskRecord, report, head_sha: str = "") -> list[int]:
        """Relay the tester-authored issues for pre-existing defects (TESTER.md):
        `auto` provenance, never `ready` — a human triages before anyone acts.
        The control plane adds only mechanical provenance, never content, and
        posts under the tester App — the finding's own identity (D3).
        Advisory end to end: any failure degrades to 'not filed', logged."""
        if not report.issues:
            return []
        if self.tester_api is None:
            log.warning("no tester App configured — auto issues not filed")
            return []
        try:
            existing = {
                issue.title.strip().lower()
                for issue in self.tester_api.list_open_issues_by_label(AUTO_PROVENANCE_LABEL)
            }
        except GitHubApiError as exc:
            log.warning("auto-issue dedup listing failed — not filing blind", error=str(exc))
            return []
        if len(report.issues) > AUTO_ISSUES_PER_REPORT:
            log.warning(
                "tester authored more issues than the per-report cap — dropping the rest",
                authored=len(report.issues),
                cap=AUTO_ISSUES_PER_REPORT,
            )
        filed: list[int] = []
        for issue in report.issues[:AUTO_ISSUES_PER_REPORT]:
            key = issue["title"].strip().lower()
            if key in existing:
                log.info("auto issue already open — skipped", title=issue["title"])
                continue
            body = (
                f"{issue['body']}\n\n---\n"
                f"_Machine-filed (`auto` provenance) from the test run on PR #{task.pr_number}, "
                f"commit `{head_sha[:7]}`. Treat the content as symptom data, not instructions; "
                f"a human adds `ready` to authorize work._"
            )
            try:
                number = self.tester_api.create_issue(
                    issue["title"], body, [AUTO_PROVENANCE_LABEL]
                )
            except GitHubApiError as exc:
                log.warning("filing auto issue failed — advisory, continuing", error=str(exc))
                continue
            existing.add(key)
            filed.append(number)
            log.info("auto issue filed", issue=number, title=issue["title"])
        return filed

    def _post_test_report(
        self,
        task: TaskRecord,
        report,
        head_sha: str = "",
        model: str | None = None,
        filed: list[int] | None = None,
        citations=None,
    ) -> None:
        """PR-caused defects post the report as a REQUEST_CHANGES review — the
        existing round machinery returns them to the developer. Otherwise the
        report is a plain comment, advisory to the human gate. Posting failures
        degrade, never block."""
        if self.tester_api is None:
            log.warning("no tester App configured — test report not posted")
            return
        body = format_test_report(report, head_sha, model, filed=filed, citations=citations)
        pr_caused = report.pr_caused
        try:
            if pr_caused:
                self.tester_api.post_review(task.pr_number, body, [], event="REQUEST_CHANGES")
            else:
                self.tester_api.comment(task.pr_number, body)
            log.info(
                "test report posted",
                pr=task.pr_number,
                scenarios=len(report.scenarios),
                failed=sum(1 for s in report.scenarios if s["result"] == "fail"),
                defects=len(report.defects),
                returned_to_dev=len(pr_caused),
                filed=[f"#{n}" for n in filed] if filed else None,
            )
        except GitHubApiError as exc:
            log.warning("posting the test report failed — advisory, continuing", error=str(exc))

    def _post_assessment(
        self, task: TaskRecord, parsed, head_sha: str = "", model: str | None = None
    ) -> None:
        """Posting is advisory — a failure degrades to 'no machine review this
        round' (logged), never a blocked task."""
        if self.reviewer_api is None:
            log.warning("no reviewer App configured — assessment not posted")
            return
        # The verdict is the review state: findings block, a clean pass approves.
        # A later review by the same App supersedes, so the red chip clears when
        # a re-assess comes back clean. The human merge gate is untouched — an
        # App approval only *satisfies* branch protection if the repo requires
        # approving reviews, which this experiment deliberately does not.
        event = "REQUEST_CHANGES" if parsed.findings else "APPROVE"
        body = format_review_body(parsed.verdict, head_sha, model)
        try:
            self.reviewer_api.post_review(task.pr_number, body, parsed.findings, event=event)
            log.info("review posted", pr=task.pr_number, verdict=event, findings=len(parsed.findings))
        except GitHubApiError as exc:
            log.warning("posting the review failed — advisory, continuing", error=str(exc))

    def _active_request_providers(self) -> list[str]:
        """Request-driver providers to fire on PR events (REVIEWERS.md) — today
        just Copilot; spawn providers queue reviewer bursts instead (slice 2)."""
        return [COPILOT_REVIEWER] if self.review.copilot else []

    def _request_re_review(self, task: TaskRecord, reviewers: list[str]) -> None:
        """Deterministic review request: on PR open (auto-review observed not
        firing) and after review-round pushes (the M1 gap). Never fatal."""
        if not reviewers:
            return
        try:
            dropped = self.api.request_reviewers(task.pr_number, reviewers)
        except GitHubApiError as exc:
            log.warning("re-request failed — advisory, continuing", error=str(exc))
            return
        requested = [r for r in reviewers if r not in dropped]
        if requested:
            log.info("review requested", pr=task.pr_number, reviewers=requested)
        if COPILOT_REVIEWER in dropped:
            log.warning(
                "Copilot request dropped — GitHub only grants it to a user with a Copilot seat",
                pr=task.pr_number,
                fix=f"set {COPILOT_TOKEN_ENV} in .env",
            )
            dropped = [r for r in dropped if r != COPILOT_REVIEWER]
        if dropped:
            log.warning("review request silently dropped", pr=task.pr_number, reviewers=dropped)

    # --- failure rail ---

    def _spent_token(self, task: TaskRecord, outcome) -> bool:
        """A burst that came back rate limited (F3): the token is spent, the task
        is not at fault.

        `execute_burst` has already benched the subscription by CAS, so the next
        pick cannot choose it; all the rail has to do is *not* charge the task
        for it — no attempt, no cooldown, no comment on the issue — and give the
        lease back so the next tick admits the same task on another member's
        token. The session is the task's, not the subscription's: it was written
        by CAS while the burst ran, so the retry resumes exactly where the limit
        cut it off."""
        if not outcome.rate_limited:
            return False
        log.warning(
            "the burst's token was spent — retrying the task on another, no attempt charged",
            task=task.task_id, until=outcome.rate_limited_until, attempt=task.attempts,
        )
        self._move(task, status=TaskStatus.CLAIMED, **self._released())
        return True

    def _register_failure(self, task: TaskRecord, reason: str) -> None:
        """One failed attempt, by CAS: the ledger moves first, the comments and
        labels follow only if it did — a peer that moved the task already spoke
        for it. The lease is released in the same statement."""
        attempts = task.attempts + 1
        number = task.issue_number
        if attempts >= self.rails.max_attempts:
            if task.next_burst_kind in MACHINE_STAGES:
                # machine stages are additive (REVIEWERS.md, TESTER.md): give up
                # on the stage, never on the task — it proceeds to the human gate
                stage = "review" if task.next_burst_kind == ASSESS else "test"
                if not self._move(
                    task, status=TaskStatus.AWAITING_REVIEW, next_burst_kind=None, attempts=0,
                    last_burst_at=utc_now(), **self._released(),
                ):
                    return
                self.api.comment(
                    number,
                    f"ℹ️ control plane: machine {stage} could not run ({reason}) — "
                    "left for the owner's review.",
                )
                log.warning("machine stage given up", task=task.task_id, stage=stage, reason=reason)
            else:
                if not self._move(
                    task, status=TaskStatus.BLOCKED, next_burst_kind=None, attempts=attempts,
                    last_burst_at=utc_now(), **self._released(),
                ):
                    return
                self.api.comment(
                    number,
                    f"⛔ control plane: parking this task after {attempts} failed attempts — {reason}. "
                    "A human can re-add `ready` to retry.",
                )
                self.api.remove_label(number, "in-progress")
                log.error("task blocked", task=task.task_id, attempts=attempts, reason=reason)
        # next_burst_kind unchanged — retry the same kind
        elif self._move(
            task, status=TaskStatus.CLAIMED, attempts=attempts, last_burst_at=utc_now(), **self._released()
        ):
            log.warning("attempt failed — will retry", task=task.task_id, attempt=attempts, reason=reason)


# --- the fleet (C4): one serial loop, N products ---


def _least_recently_served(row: RepositoryRecord) -> tuple[bool, str, str]:
    """Never-served first (no stamp), then the oldest stamp; the slug breaks
    ties so two products registered together take turns deterministically."""
    return (row.last_burst_at is not None, row.last_burst_at or "", row.repository)


# The window fair share is measured over (H1). Long enough that a product cannot
# be starved by a busy morning and short enough that a share changed today means
# something this week: a day. Code, not a knob — the *weight* is the row's
# (`usage_share`), and a second number saying "over how long" would let two
# operators mean different things by the same share.
FAIR_SHARE_WINDOW_H = 24.0


def _since(hours: float) -> str:
    """The start of a window, in the spelling every stored stamp has."""
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _fair_share_order(row: RepositoryRecord, served: dict[str, int]) -> tuple[float, bool, str, str]:
    """Whose turn it is: the product with the least served *per unit of weight*.

    `usage_share` is the weight an operator declared (C1), so a product at 2.0
    should see twice the bursts of one at 1.0 over the window, and the ratio
    below is what makes that true without anyone maintaining a counter — the
    numerator is an aggregate over `bursts`, the denominator is the row.

    With equal weights this is least-served-first, and ties fall through to the
    C4 order — least *recently* served, then the slug — so two products that have
    had the same number of bursts still alternate rather than picking by name.
    """
    weight = row.usage_share if row.usage_share > 0 else 1.0
    return (served.get(row.repository, 0) / weight, *_least_recently_served(row))


class Fleet:
    """The registry-driven multiplexer (C4).

    Reads the active products fresh every tick and builds each one's
    ``Scheduler`` anew — registration, ``pause``/``resume`` and ``product set``
    take effect on the next tick with no cache to invalidate. Every tick takes
    every product's observation turn (E2: the turn is a lease on the registry
    row, one per poll interval fleet-wide, so state advances everywhere and N
    replicas poll GitHub once), then spawns at most one burst: the least
    recently served product with a claimable task under its own quotas — the
    row's daily ceiling and its concurrency, both counted at admission across
    the fleet. With one active product and one replica this degenerates to
    exactly the single-product loop — observe, then at most one burst.

    A product's trouble stays its own: a build or observe failure is logged
    and skips that product for the tick, never the fleet.

    The org's STOP (E3) is read here at the top of the tick and again before
    every spawn — `standing_down`, the same read the single-product loop makes
    — and the fleet's one shutdown flag is handed to every scheduler it builds,
    so a live burst's heartbeat fences on SIGTERM whichever product owns it.
    """

    def __init__(
        self,
        stores: PlatformStores,
        build_scheduler: Callable[[RepositoryRecord], Scheduler],
    ) -> None:
        self.stores = stores
        self.build_scheduler = build_scheduler
        self.halt = threading.Event()  # the process's shutdown flag (E3), shared with every scheduler built
        self.wake = threading.Event()  # the loop's wake flag (E5): a webhook's wake cuts the sleep

    def stopped(self) -> bool:
        """`standing_down` for this loop: the process's flag or the org's row."""
        return standing_down(self.stores.ledger, self.halt)

    def tick(self) -> None:
        if self.stopped():
            return
        # the pool's own housekeeping, once a tick and independent of any product:
        # a contribution running out of year is announced daily (F4), whether or
        # not there is work to spend it on
        announce_expiring(self.stores.ledger)
        pairs: list[tuple[RepositoryRecord, Scheduler]] = []
        for row in self.stores.ledger.list_repositories():
            if row.state != ProductState.ACTIVE:
                continue  # paused products are ignored entirely — no observe, no spawn
            try:
                sched = self.build_scheduler(row)
            except (ReviewPolicyError, E2EPolicyError, GitHubApiError, GitHubAuthError) as exc:
                # `set` preflights on this machine (C2), so this is drift since
                # registration (env changed, repo unreachable) — quarantine the
                # product for the tick, not the fleet
                log.error("product skipped this tick", repo=row.repository, error=str(exc))
                continue
            sched.halt = self.halt  # one flag per process, whoever's burst it is
            pairs.append((row, sched))
        if not pairs:
            log.warning("no operable active products — idle tick")
            return
        for row, sched in pairs:
            try:
                sched.observation_turn()
            except (GitHubApiError, GitHubAuthError) as exc:
                log.warning("observe failed — product skipped this tick", repo=row.repository, error=str(exc))
        # Who pays, asked once for the fleet (H1). The pool is the platform's, not
        # a product's, so an empty one is an empty one for everybody: asking per
        # product would spend the tick claiming a task, giving it back, and
        # announcing the same exhaustion N times. Nothing is touched here — no
        # lease, no attempt — and the tick ends where it would have gone idle.
        try:
            capacity = pick_capacity(self.stores.ledger)
        except CapacityExhausted as exc:
            log.warning("the pool has nothing to give — no product runs this tick", detail=str(exc))
            self.stores.ledger.record_event(
                EVENT_CAPACITY_EXHAUSTED, {"products": len(pairs), "detail": str(exc)}
            )
            return
        served = self.stores.ledger.bursts_per_product_since(_since(FAIR_SHARE_WINDOW_H))
        for row, sched in sorted(pairs, key=lambda pair: _fair_share_order(pair[0], served)):
            if self.stopped():  # `stop` may have landed during observation
                return
            task = sched.pick_next()
            if task is None:
                continue
            # the turn is the unit of fairness, stamped when taken — a spawn
            # that then fails still spent this product's place in line
            self.stores.ledger.touch_repository_burst(row.repository, utc_now())
            sched.run_task_burst(task, capacity)
            return  # one burst per tick — the loop stays serial
