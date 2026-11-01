"""One burst, end to end — the choreography the CLI and the scheduler share.

prepare volume → mint GitHub token → burst span → the runner's `run` (log tee) →
harvest → ledger. This is the proven M0/M1 manual-driver path, extracted so
the M2 scheduler runs exactly the same code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from control_plane.clone import pinned_origin
from control_plane.github_auth import (
    BURST_PERMISSIONS,
    CLONE_PERMISSIONS,
    AppCredentials,
    GitHubAuthError,
    bot_git_identity,
    mint_installation_token,
)
from control_plane.models import (
    BurstRecord,
    BurstStatus,
    MemoryEntry,
    SubscriptionStatus,
    TaskRecord,
    utc_now,
)
from control_plane.prompts import abandoned_attempts
from control_plane.roles import ALL_TOOLS, RoleError, compose_prompt, repo_read_only
from control_plane.roles import validate as validate_role
from control_plane.runner import DEFAULT_IMAGE, BurstSpec, Heartbeat, Runner, open_runner
from control_plane.store import Ledger, MemoryError_, SessionArchive, open_archive, open_ledger, session_key
from control_plane.subscriptions import (
    MACHINE_CREDENTIAL,
    Capacity,
    SubscriptionError,
    auth_failed,
    pick_capacity,
    rate_limited_until,
)
from control_plane.telemetry import burst_span
from control_plane.volume import HarvestOutcome, Snapshot, fold_artifacts

log = structlog.get_logger()


@dataclass(slots=True)
class PlatformStores:
    """The platform's data root and its two stores, opened together.

    Both are chosen by presence toggle (`LEDGER_DSN`, `ARCHIVE_URL`) and both are
    held behind their protocol, so nothing downstream of here can tell — or name —
    which backend it got.
    """

    data_dir: Path
    archive: SessionArchive
    ledger: Ledger

    @classmethod
    def open(cls, data_dir: Path) -> PlatformStores:
        data_dir = data_dir.expanduser().resolve()
        return cls(
            data_dir=data_dir,
            archive=open_archive(data_dir),
            ledger=open_ledger(data_dir),
        )


@dataclass(slots=True)
class BurstOutcome:
    burst: BurstRecord
    harvest: HarvestOutcome
    exit_code: int
    log_path: Path
    # When the subscription that paid for this burst is usable again (F3), if
    # what came back was a rate limit. Set here because this is where the result
    # and the payer are both in hand; the ledger already carries the backoff by
    # the time a caller reads it. None is every other ending, including success.
    rate_limited_until: str | None = None
    # The subscription's credential was refused (F4) — the environment's
    # failure, like a rate limit, and never the approach's (H5).
    credential_refused: bool = False

    @property
    def succeeded(self) -> bool:
        return outcome_succeeded(self.exit_code, self.harvest.result)

    @property
    def spent_its_allowance(self) -> bool:
        """The engine ran the attempt to the end of what it was given — its
        turns or its budget — and came back without the work (H5). The one
        failure the rail reads as the *approach's*: nothing stopped the burst
        from outside, and resuming the same transcript would hand the next
        burst the same exhausted path. Everything else that fails — a kill, a
        spent token, a refused credential, a stream that died with no result —
        is the environment's, and re-runs the same node."""
        if self.burst.status == BurstStatus.KILLED or self.rate_limited or self.credential_refused:
            return False
        return (self.harvest.result or {}).get("subtype") in ALLOWANCE_SPENT

    @property
    def rate_limited(self) -> bool:
        """The burst did not fail on its own account — the token it was given is
        spent. The attempt rail treats this as no attempt at all: nothing about
        the task was tried, and the next pick has one fewer eligible payer."""
        return self.rate_limited_until is not None


# How the engine says an attempt used up what it was given (the result's
# `subtype`, as the SDK spells it). The rail's whole test for an approach
# failure among bursts that failed: see `BurstOutcome.spent_its_allowance`.
ALLOWANCE_SPENT = frozenset({"error_max_turns", "error_max_budget_usd"})

# What the engine's ending reads as in a branch summary — a sentence a person
# and the next branch can both take in at once.
_ENDINGS = {
    "error_max_turns": "ran out of turns",
    "error_max_budget_usd": "ran out of budget",
    "error_during_execution": "stopped on an error during execution",
    "success": "finished",
}


def branch_summary(burst: BurstRecord, result: dict | None, why: str) -> str:
    """The short summary of an abandoned attempt (H5), built from what the
    platform already holds — the burst's row, the engine's ending, and the
    rail's reason — never from the transcript, which the platform does not
    read. Structural on purpose: it says *that* and *how* an attempt ended, not
    what it tried; what it tried stays in its archived snapshot, one key away."""
    result = result or {}
    ending = _ENDINGS.get(result.get("subtype") or "", "ended without a result from the engine")
    grew = f"burst {burst.resumed_from_burst_id}" if burst.resumed_from_burst_id else "the start of the task"
    spent = []
    if burst.num_turns is not None:
        spent.append(f"{burst.num_turns} turns")
    if burst.cost_usd is not None:
        spent.append(f"${float(burst.cost_usd):.2f}")
    detail = f" ({', '.join(spent)})" if spent else ""
    return (
        f"Attempt {burst.burst_id} ({burst.kind}, grown from {grew}) {ending}{detail} "
        f"and was abandoned: {why}."
    )


def abandon_attempt(ledger: Ledger, task: TaskRecord, outcome: BurstOutcome, why: str) -> bool:
    """Retire an attempt's node from the session tree and remember that it
    happened (H5): the next burst branches from the node this one grew from,
    and is shown this summary as data (`abandoned_attempts`) rather than the
    transcript that led here.

    The summary is also proposed to the product's memory, with the burst that
    made it as provenance — proposed, like everything a burst leads to (H4),
    so the gardener decides whether a failed attempt taught anything durable.
    A refused proposal is a log line; the abandon is what the rail needed.
    False when the node was already abandoned (a peer, or a retry of this
    judgement) — nothing is proposed twice."""
    burst = outcome.burst
    summary = branch_summary(burst, outcome.harvest.result, why)
    if not ledger.abandon_burst(burst.burst_id, summary):
        return False
    burst.branch_summary = summary
    log.warning(
        "attempt abandoned — the next burst branches",
        task=task.task_id, burst=burst.burst_id, branch_from=burst.resumed_from_burst_id,
    )
    try:
        ledger.propose_memory(
            MemoryEntry(
                scope="product",
                claim=summary,
                evidence=(
                    f"burst {burst.burst_id}: status {burst.status}, exit {burst.exit_code}, "
                    f"engine ending {(outcome.harvest.result or {}).get('subtype')!r}; "
                    f"snapshot {burst.session_key or 'not archived'}"
                ),
                repository=task.repository,
                proposed_by_burst=burst.burst_id,
            )
        )
    except MemoryError_ as exc:
        log.warning("the branch summary was not proposed", burst=burst.burst_id, reason=str(exc))
    return True


def outcome_succeeded(exit_code: int, result: dict | None) -> bool:
    """`BurstOutcome.succeeded`, before there is an outcome to ask — the harvest
    knows both halves and the answer must not be spelled twice."""
    return exit_code == 0 and not (result or {}).get("is_error")


def codex_auth_dir() -> Path | None:
    codex = Path.home() / ".codex"
    return codex if (codex / "auth.json").is_file() else None


def mint_burst_token(task: TaskRecord, role: str) -> tuple[str, str, str] | None:
    """The burst token tier (TO-BE §4, D2): minted fresh per spawn, downscoped
    below the App's grant — pinned to the task's repository and to the role's
    permission subset, so a burst for product A holds a token product B rejects
    by construction. The broad cached tier stays control-plane-only.

    Returns ``(token, git_name, git_email)``, or None when the App isn't
    configured (the burst runs without GitHub — dev mode).
    """
    creds = AppCredentials.from_env()
    if creds is None:
        return None
    if role not in BURST_PERMISSIONS:
        # reviewer/tester bursts hold no GitHub credential at all — a request
        # for one is a broken code path, not a policy to satisfy
        raise GitHubAuthError(f"role {role!r} holds no GitHub credential inside a burst (TO-BE §4)")
    minted = mint_installation_token(
        creds,
        repositories=[task.repository.split("/", 1)[1]],
        permissions=BURST_PERMISSIONS[role],
    )
    git_name, git_email = bot_git_identity(minted, creds.app_id)
    log.info(
        "github token minted",
        pushes_as=git_name,
        expires=minted.expires_at,
        scoped_to=task.repository,
        permissions=sorted(minted.permissions),
    )
    return minted.token, git_name, git_email


def mint_clone_token(task: TaskRecord) -> str | None:
    """`contents:read` on the task's repository, for an init container alone.

    Only a runner whose phases run beside the burst asks for this (`pod_side`,
    G3): a pod inherits no mount, so something in it has to fetch the working
    tree, and that something is not the agent. The scope is the smallest one
    that can clone, and the container holding it is gone before the agent's
    first turn — which is how a reviewer or tester burst gets a tree while still
    holding no GitHub credential of its own (TO-BE §6).

    None when the App isn't configured: a public repository clones without one,
    and a private one fails in the pod, loudly, with the repository named.
    """
    creds = AppCredentials.from_env()
    if creds is None:
        return None
    minted = mint_installation_token(
        creds,
        repositories=[task.repository.split("/", 1)[1]],
        permissions=CLONE_PERMISSIONS,
    )
    log.info("clone token minted", scoped_to=task.repository, expires=minted.expires_at)
    return minted.token


def execute_burst(
    stores: PlatformStores,
    task: TaskRecord,
    repo: Path,
    prompt: str,
    *,
    kind: str = "work",
    role: str = "dev",
    mode: str = "run",
    image: str = DEFAULT_IMAGE,
    model: str | None = None,
    max_budget_usd: float | None = None,
    max_turns: int | None = None,
    fresh: bool = False,
    burst_id: str | None = None,
    with_github: bool = False,
    mount_codex: bool = False,
    extra_env: dict[str, str] | None = None,
    egress: tuple[str, ...] = (),
    runner: Runner | None = None,
    heartbeat: Heartbeat | None = None,
    capacity: Capacity | None = None,
) -> BurstOutcome:
    """Run one burst container against the task and fold the results back in.

    The role's row (D5) is read here, at spawn, and is the source of the
    burst's model, effort, budget, turns, preamble and mount: ``model`` /
    ``max_budget_usd`` / ``max_turns`` given explicitly (the hand-run ``burst``
    verb) win for this one burst; None means the row. An edit to an open field
    therefore governs the *next* burst, with no restart.

    ``with_github`` mints the dev App token into the box, downscoped to the
    task's repository + the role's permission subset (`mint_burst_token`, D2);
    reviewer bursts get no GitHub credential at all — REVIEWERS.md.
    ``mount_codex`` mounts the operator's codex auth (slice 3, reviewer only).
    ``extra_env`` injects sandbox credentials — the declared-ref intersection
    resolved at spawn (D4), tester bursts only (TESTER.md). ``heartbeat`` (E1)
    is the scheduler's lease renewal, asked by the runner while the container
    runs; the hand-run verb holds no lease and passes none. A burst the
    heartbeat fenced — a False answer, on which the runner kills the container
    — is recorded ``killed`` rather than ``failed`` (E3): the exit code alone
    cannot tell a kill from a crash, so the answer is remembered here.
    ``runner`` is where the burst runs (E4): the deployment's (`open_runner`)
    unless the caller brings its own — the scheduler's one per process, the
    suite's fakes in docker's seat. ``capacity`` is who pays (F2): the
    scheduler picks before it admits the burst, so an exhausted pool idles
    instead of failing an attempt; a caller that passes none — the hand-run
    verb — picks here and lets `CapacityExhausted` reach it. The id is stamped
    on the burst row; the token goes to the box and nowhere else.

    Task writes here are the E1 shapes: the row is created if the ledger has
    never seen it (never overwritten if it has), and the dev burst's session id
    lands by compare-and-set against the (status, holder) the task carried on
    entry — a task a peer has since moved is left as the peer left it.

    May raise GitHubAuthError (before the container starts) — callers decide
    whether that aborts (CLI) or waits for the next tick (scheduler).
    """
    runner = runner if runner is not None else open_runner(stores.archive)
    if capacity is None:
        capacity = pick_capacity(stores.ledger) if mode == "run" else MACHINE_CREDENTIAL
    profile = stores.ledger.get_role(role)
    if profile is None:
        raise RoleError(f"role {role!r} has no row in the ledger — the pipeline roles are seeded (TO-BE §4)")
    validate_role(profile)
    if profile.tools != [ALL_TOOLS]:
        # a narrowed row is a real declaration; running it wide would be a silent
        # widening. The container-side narrowing lands with custom-role
        # execution (T4) — until then a narrowed row cannot run at all.
        raise RoleError(
            f"role {role!r} narrows tools to {profile.tools} — tool narrowing reaches the "
            "container with custom-role execution (T4); no row that runs today may narrow"
        )
    model = model or profile.model
    effort = profile.effort
    max_budget_usd = profile.max_budget_usd if max_budget_usd is None else max_budget_usd
    max_turns = profile.max_turns if max_turns is None else max_turns
    prompt = compose_prompt(profile, prompt)
    codex_auth = codex_auth_dir() if mount_codex else None
    if mode == "run":
        log.info("burst starting", task=task.task_id, kind=kind, role=role, model=model, effort=effort)

    github_token = git_name = git_email = None
    if mode == "run" and with_github:
        scoped = mint_burst_token(task, role)
        if scoped is not None:
            github_token, git_name, git_email = scoped

    # Where this burst's snapshot goes — decided here, before the spec, because a
    # runner whose phases run beside the burst has to mint the PUT for that key at
    # spawn. dev: the session tree's next node. tester: evidence — the transcript is
    # the record of what the sandbox *did*, and it must outlive the machine that ran
    # it (B5). Leafhood stays the dev's either way: the leaf query filters on role,
    # so a tester's snapshot can never be resumed. Reviewers read and never execute,
    # so there is nothing to prove — not archived.
    burst_id = burst_id or uuid.uuid4().hex[:12]
    # The node this burst descends from: the leaf of the task's session tree, or
    # nothing. fresh = amnesiac (the reviewer), which is exactly "no parent" — so
    # the volume cannot be seeded with the dev's transcripts and the resume is off,
    # from one decision rather than two flags.
    parent = None if fresh else stores.ledger.latest_archived_burst(task.task_id)
    volume = stores.data_dir / "volumes" / burst_id  # where this host keeps it; the runner fills it
    resume = None if fresh else task.session_id
    # Attempts already abandoned at this node (H5): this burst is a new branch.
    # It resumes the node's own session — not the task's latest, which is the
    # abandoned tail — and forks it, so the tail is neither in the volume nor in
    # the session id; what it gets instead is each attempt's summary, as data.
    abandoned = [] if fresh else stores.ledger.abandoned_branches(
        task.task_id, parent.burst_id if parent is not None else None
    )
    if abandoned:
        resume = parent.session_id if parent is not None else None
        prompt = f"{prompt}\n\n{abandoned_attempts([b.branch_summary or '' for b in abandoned])}"

    burst = BurstRecord(
        burst_id=burst_id,
        task_id=task.task_id,
        kind=kind,
        role=role,
        model=model,
        # recorded at spawn, before the container starts: the edge is a fact about
        # what this burst was given, not about what it managed to produce
        resumed_from_burst_id=parent.burst_id if parent is not None else None,
        # who pays (F2) — the id, never the token; NULL is this machine's own
        subscription_id=capacity.subscription_id,
    )
    # The task row goes in before the burst row that points at it. A4's
    # bursts.task_id foreign key is what makes the order load-bearing, but the
    # order was always the honest one: the CLI can be handed a task the ledger has
    # never seen, and a burst is evidence about a task, so the task exists first.
    # Insert-if-absent (E1): a task the scheduler holds is already there, leased
    # and `working`, and a whole-row write over it would be the blind overwrite
    # the CAS discipline retires.
    stores.ledger.create_task(task)
    entered = (task.status, task.claimed_by)  # what the session write compares against
    with burst_span(task.task_id, burst_id, role, kind, model=model, parent=parent) as (
        span,
        traceparent,
    ):
        context = span.get_span_context()
        # Both halves, because a successor's span link needs both (B1). The lookup
        # TO-BE §12 describes turns out to cost nothing: the parent row is already
        # in hand — it's what the volume was materialized from.
        burst.trace_id = f"{context.trace_id:032x}"
        burst.span_id = f"{context.span_id:016x}"
        stores.ledger.record_burst(burst)
        spec = BurstSpec(
            task_id=task.task_id,
            burst_id=burst_id,
            volume_dir=volume,
            repo_mount=repo,
            prompt=prompt,
            role=role,
            kind=kind,
            command=mode,
            image=image,
            resume_session_id=resume,
            fork_session=bool(abandoned) and resume is not None,
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
            model=model,
            effort=effort,
            traceparent=traceparent,
            github_token=github_token,
            git_user_name=git_name,
            git_user_email=git_email,
            codex_auth_dir=codex_auth,
            repo_read_only=repo_read_only(profile),  # code floor ∨ the row; never widened
            oauth_token=capacity.token,  # the picked subscription's, or None for this machine's
            archive_as=session_key(task.task_id, burst_id) if role in ("dev", "tester") else None,
            # a pod inherits no mount, so the burst is told what to fetch and
            # given a credential scoped to fetching it — and neither is asked for
            # on a substrate that shares a filesystem with the control plane
            clone=pinned_origin(repo) if runner.pod_side else None,
            # minted for a `check` burst too: the mode decides what the *agent*
            # holds, and the init container is not the agent — a boot smoke still
            # needs the working tree it is smoking against
            clone_token=mint_clone_token(task) if runner.pod_side else None,
            extra_env=dict(extra_env or {}),
            egress=tuple(egress),
        )
        log_path = stores.data_dir / "logs" / f"{burst_id}.log"
        fenced = False

        def beat() -> bool:
            # the heartbeat's False is the runner's cue to kill the container; it
            # is remembered here because the exit code that comes back is the
            # container's (137, or whatever the kill left) and cannot say why
            nonlocal fenced
            alive = heartbeat()  # type: ignore[misc] — only bound when a heartbeat was given
            fenced = fenced or not alive
            return alive

        # phase one (G1): the volume, filled from the parent node. The runner owns
        # it because *where* those bytes live is the runner's — a directory here,
        # an init container's emptyDir on a cluster.
        runner.materialize(spec, Snapshot.of(parent))
        exit_code = runner.run(spec, log_path=log_path, heartbeat=beat if heartbeat is not None else None)
        burst.exit_code = exit_code
        if fenced:
            # killed, not failed (E3): the lease was lost, the org stopped or the
            # replica is shutting down — the work was stopped, it did not break
            burst.status = BurstStatus.KILLED
        else:
            burst.status = BurstStatus.SUCCEEDED if exit_code == 0 else BurstStatus.FAILED
        burst.finished_at = utc_now()
        span.set_attribute("burst.exit_code", exit_code)

        # phase three: what the burst left comes back as bytes, and only here —
        # in the control plane, with the credentials for it — does it become rows
        artifacts = runner.collect(spec)
        outcome = fold_artifacts(artifacts, burst, stores.ledger)
        if burst.model:
            span.set_attribute("burst.model", burst.model)  # resolved beats requested
        stores.ledger.finish_burst(burst)
        if role == "dev" and burst.session_id and burst.session_id != task.session_id:
            # the task's session is the *dev's* memory — a reviewer burst's
            # session must never become what the next dev round resumes.
            # The snapshot itself is on the burst row now (A5); the id lands by
            # CAS (E1): if the task has moved since this burst was spawned — a
            # peer reclaimed a lapsed lease — its state is the peer's to write.
            status, holder = entered
            written = stores.ledger.transition_task(
                task.task_id, {"session_id": burst.session_id},
                expected_status=status, expected_holder=holder,
            )
            if written is None:
                log.warning(
                    "task moved under this burst — session id not recorded",
                    task=task.task_id, burst=burst_id,
                )
            else:
                task.session_id = written.session_id

        # Inside the span on purpose: this is the line a reader starts from, and
        # outside it the trace, burst and task ids are no longer bound (B1). The
        # burst is already finalized in the ledger by here — the span is still open
        # only because the log line is part of the burst, not part of what follows.
        # a spent token is not a failed task (F3): bench the subscription by CAS
        # — forward only — and tell the caller, whose rail must not charge the
        # task for a limit it did not cause
        backoff = None
        refused = False
        if capacity.subscription_id is not None and not outcome_succeeded(exit_code, outcome.result):
            backoff = rate_limited_until(outcome.result)
            if backoff is not None:
                moved = stores.ledger.back_off_subscription(capacity.subscription_id, backoff)
                log.warning(
                    "the subscription that paid for this burst is rate limited — benched",
                    subscription=capacity.subscription_id, until=backoff, moved=moved,
                )
            elif auth_failed(outcome.result):
                refused = True
                # the credential itself was refused: the year ran out, or the
                # member revoked it at the provider. The row says `expired` from
                # now on — by CAS, and with an audit row, because the platform
                # changed a member's standing on its own authority (F4)
                try:
                    stores.ledger.set_subscription_status(
                        capacity.subscription_id, SubscriptionStatus.EXPIRED, "control-plane",
                        reason=f"burst {burst_id} could not authenticate with this subscription's token",
                    )
                    log.warning(
                        "a contributed token no longer authenticates — marked expired",
                        subscription=capacity.subscription_id,
                    )
                except SubscriptionError as exc:  # already expired or revoked: nothing to say
                    log.info("the subscription was already ended", subscription=capacity.subscription_id, detail=str(exc))
        log.info(
            "burst finished",
            exit=exit_code,
            status=str(burst.status),
            model=burst.model,
            session=burst.session_id,
            violations=outcome.violation_count,
            resumed_from=burst.resumed_from_burst_id,
            snapshot=outcome.session_key,
            log=str(log_path),
        )
    return BurstOutcome(
        burst=burst, harvest=outcome, exit_code=exit_code, log_path=log_path,
        rate_limited_until=backoff, credential_refused=refused,
    )
