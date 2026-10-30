"""Domain vocabulary: tasks, bursts, violations.

A *task* is one GitHub issue worked to completion; a *burst* is one container
run against it (work / review round / test). The control plane's records mirror
ARCHITECTURE.md §6 — state per task = branch (GitHub) + session (object store)
+ issue/PR thread (GitHub); these rows are the queryable index over it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Role(StrEnum):
    DEV = "dev"
    TESTER = "tester"


class BurstKind(StrEnum):
    WORK = "work"
    REVIEW = "review"
    TEST = "test"


class BurstStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    KILLED = "killed"


class TaskStatus(StrEnum):
    """The scheduler's state machine (SCHEDULER.md)."""

    OPEN = "open"  # manual driver (M0/M1) — outside the scheduler's machine
    CLAIMED = "claimed"  # ours; a burst is queued (see next_burst_kind)
    WORKING = "working"  # the burst container is running
    AWAITING_REVIEW = "awaiting_review"  # PR open; humans/Copilot have the ball
    BLOCKED = "blocked"  # needs a human
    DONE = "done"  # PR merged; issue auto-closed


class ProductState(StrEnum):
    """A registered product is scheduled or it is not — nothing in between."""

    ACTIVE = "active"
    PAUSED = "paused"


class SubscriptionStatus(StrEnum):
    """A contributed subscription's standing (TO-BE §4 mechanism A). `active`
    is the only eligible one; `expired` is reached by time or by an auth-failed
    burst, `revoked` by the member's or an operator's word. Neither ending is a
    deletion — the row stays, because the bursts it paid for still name it."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def utc_iso(value: str) -> str:
    """An ISO timestamp respelled in UTC — the spelling every stored stamp has.

    Every timestamp the ledger holds was written by `utc_now`, so it carries the
    `+00:00` offset. A caller's timestamp need not: `2026-08-17T12:00:00+06:00` is
    an earlier instant than `2026-08-17T09:00:00+00:00` but a later string. The
    Postgres backend compares instants and doesn't care; the SQLite one compares
    text, so anything it compares goes through here first (gate A7 measured the
    difference: `bursts_started_since` answered 0 where Postgres answered 2).
    """
    parsed = datetime.fromisoformat(value)
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def task_id_for(repository: str, issue_number: int) -> str:
    """A task's id, derived and never invented: ``{owner}/{repo}#{issue_number}``.

    Two properties come from deriving it (TO-BE §2, STORES.md). It is stable across
    replicas — two of them looking at the same issue compute the same id without
    coordinating — and it joins the GitHub world for free, so a row and a URL name
    the same thing. `UNIQUE (repository, issue_number)` then makes a second task
    for one issue unrepresentable rather than unlikely.
    """
    return f"{repository}#{issue_number}"


@dataclass(slots=True)
class RepositoryRecord:
    """One registered product — the tenancy unit (TO-BE §2).

    Every policy and quota column holds a materialized value, never a
    NULL-that-means-inherit: `product register` fills them from platform
    defaults unless overridden, and after that the row is the entire truth
    about the product. A changed platform default never retro-propagates —
    changing a live product is `product set`, an audited operator action.
    """

    repository: str  # 'owner/name' — the primary key, same spelling tasks use
    team: str  # owning team: gate authority, where escalations go
    state: str = ProductState.ACTIVE
    review_enabled: bool = False
    review_provider: str = ""  # the selector, spelled as REVIEW_PROVIDERS spells it
    e2e_enabled: bool = False
    test_env_refs: dict = field(default_factory=dict)  # name → secret ref, never a value
    concurrency: int = 1
    daily_bursts: int = 20  # mirrors Rails.daily_burst_ceiling
    usage_share: float = 1.0
    last_burst_at: str | None = None  # least-recently-served scheduling (C4)
    claimed_by: str | None = None  # observation lease (E2)
    claimed_until: str | None = None
    registered_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class RoleRecord:
    """One role row — a declarative execution contract (TO-BE §4, D5).

    Ceilings in code, composition in data: the wall-bearing fields (`role`,
    `kind`, `identity`, `sink`, `tools`) of a pipeline row are frozen by the
    store; the open ones (`prompt_preamble`, `model`, `effort`,
    `max_budget_usd`, `max_turns`, `mount_mode`) are `role set`'s, audited.
    `execute_burst` reads the row at every spawn, so an edit governs the next
    burst.
    """

    role: str  # the primary key; pipeline identity is code keyed on this name
    kind: str  # 'pipeline' | 'custom'
    prompt_preamble: str = ""  # composed *after* the image's role preamble, never instead
    tools: list = field(default_factory=lambda: ["*"])  # ['*'] or ⊆ roles.TOOL_ALLOWLIST
    model: str = "claude-sonnet-5"
    effort: str | None = None  # dev_agent.env.EFFORT_LEVELS; None is the CLI's default for the model
    max_budget_usd: float = 2.0
    max_turns: int = 100
    mount_mode: str = "rw"  # 'rw' | 'ro' — a row can narrow the mount, never widen it
    identity: str = "none"  # 'none' | 'automation' — the pipeline Apps are not values
    sink: str = "issue_comment"  # 'issue_comment' | 'ledger_artifact' | 'event'
    trigger: dict | None = None  # schedule/event — machinery lands at T4
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


# The pool's rolling window (F2). A contributed subscription meters itself over a
# rolling window, so the platform's headroom question — "how much of this
# member's capacity is already spent?" — is asked over the same span: a
# `cap_tokens` is tokens per THIS long. Code, not a knob: the cap is the number
# an operator declares per member, and two numbers where one will do is how a
# pool gets two ways to mean the same thing. A member's longer-horizon ceiling
# is the member's business and reaches the platform as a rate-limited burst →
# `backoff_until` (F3), which is a fact rather than an estimate.
USAGE_WINDOW_H = 5.0

# How long before a contributed token dies the platform starts saying so (F4).
# A `claude setup-token` lasts a year, and renewing it is a person's errand —
# re-materialize at the same ref, then move the row's date — so the notice has to
# reach a human with time to run it, and keep reaching them: one
# `subscription_expiring` event per subscription per day through the final
# fortnight, until the date moves or the token dies.
EXPIRY_NOTICE_DAYS = 14


@dataclass(slots=True)
class SubscriptionRecord:
    """One member's contributed capacity — a *ref*, never a token (TO-BE §4).

    The pool's whole custody story is in `secret_ref`: the ledger holds a name
    (`env:NAME`, `file:PATH`, `helper:NAME`), the member materializes the value
    themselves, and it is resolved at spawn into a container that dies with it.
    Deleting the secret at its source is therefore a revocation the platform
    cannot override, and no ledger dump, log line or archive can carry a token
    the ledger never held.

    Usage is *derived*, never mirrored here: consumption is a rolling-window
    aggregate over `bursts` (§12's ledger-as-metrics rule), so the only pool
    state this row keeps beyond the declaration is `backoff_until` — set by CAS
    when a burst reports rate limiting, cleared by time.
    """

    subscription_id: str  # the member's handle, normalized — derived, never invented
    member: str  # who contributed it, as they are known to the team
    secret_ref: str  # env:NAME / file:PATH / helper:NAME — a name, never a value
    cap_tokens: int  # the member's ceiling, tokens per rolling window
    expires_at: str  # the OAuth token's own year, as the member declared it
    status: str = SubscriptionStatus.ACTIVE
    backoff_until: str | None = None  # rate-limited until — CAS'd by the attempt rail (F3)
    registered_at: str = field(default_factory=utc_now)


class MemoryStatus(StrEnum):
    """A memory entry's standing (TO-BE §9). A burst may only ever produce
    `proposed`: nothing a burst writes promotes itself into recall."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(slots=True)
class MemoryEntry:
    """One thing the platform learned, as a datum and not an instruction.

    `claim` is what is believed, `evidence` is why — both required, because an
    entry without evidence is an instruction wearing a datum's clothes, and §9's
    rule is that memory is injected as data an agent may *verify*. Provenance is
    the burst that proposed it, which is what makes an entry traceable and what
    the gardener and the console read.

    Scope decides who ever sees it: `product` entries name a repository and are
    filtered to that product's bursts (containment — product A's bursts never see
    product B's memory), `role` entries name a role, `platform` entries name
    neither and are operational.
    """

    scope: str  # 'product' | 'role' | 'platform'
    claim: str
    evidence: str
    repository: str | None = None  # product scope ⇔ set
    role: str | None = None  # role scope ⇔ set
    proposed_by_burst: str | None = None
    status: str = MemoryStatus.PROPOSED
    id: int | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class AuditRecord:
    """One operator mutation, as the ledger recorded it — the read shape of the
    `audit` table (append-only by grant; TO-BE §2, "The audit log starts here")."""

    id: int
    actor: str
    action: str  # 'product.*' | 'role.set' | 'subscription.*' | 'org.stop' | 'org.resume'
    target: str  # the product slug, the role name, or ORG_TARGET
    reason: str
    at: str


# The audit target of the org verbs (E3): the org has no name of its own in the
# ledger, so its rows carry this one — `audit_trail(ORG_TARGET)` is its history.
ORG_TARGET = "org"


@dataclass(slots=True)
class OrgStop:
    """The org STOP as the ledger holds it (E3): the one `org_state` row while
    its `stop_reason` is set. `Ledger.org_stop` returns None for a running org —
    there is no "running" record, because the row carries nothing then."""

    reason: str
    stopped_by: str
    stopped_at: str


@dataclass(slots=True)
class TaskRecord:
    """One GitHub issue worked to completion, keyed by its derived id."""

    task_id: str  # '{owner}/{repo}#{issue_number}' — see task_id_for
    repository: str  # 'owner/name', the registered product; never a clone path
    issue_number: int
    status: str = TaskStatus.OPEN
    session_id: str | None = None
    pr_number: int | None = None
    attempts: int = 0
    review_rounds: int = 0
    next_burst_kind: str | None = None  # work | review, while claimed/working
    last_review_id: int | None = None  # advanced only after a review burst succeeds
    last_burst_at: str | None = None
    # The lease (STORES.md, "Claim — a lease is data", E1): which replica holds
    # the task's next burst and until when. Stamped by `claim_next`, renewed by
    # the heartbeat while the container runs, cleared by the transition that
    # ends the burst. A lapsed lease is what a dead replica leaves behind —
    # visible data any peer may reclaim, never a held lock.
    claimed_by: str | None = None
    claimed_until: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


# What a state transition may write (E1). `task_id` is the key, `created_at` is
# history, and both backends check a transition's columns against this one tuple
# so a typo in the scheduler fails loudly instead of binding nothing.
TASK_TRANSITION_COLUMNS = (
    "status",
    "session_id",
    "pr_number",
    "attempts",
    "review_rounds",
    "next_burst_kind",
    "last_review_id",
    "last_burst_at",
    "claimed_by",
    "claimed_until",
)


@dataclass(slots=True)
class BurstRecord:
    """One container run — a row in the burst ledger.

    Also a node in the session tree (A5): ``resumed_from_burst_id`` names the
    parent this burst's volume was materialized from, and
    ``session_key``/``session_checksum`` name the snapshot this burst left behind.
    The ledger owns the tree; the archive is dumb storage that knows only keys.
    """

    burst_id: str
    task_id: str
    kind: str = BurstKind.WORK
    role: str = Role.DEV
    status: str = BurstStatus.RUNNING
    trace_id: str = ""
    # The burst span's own id. Both halves are here because a successor's span link
    # names a span, not a trace — see `telemetry.link_to_burst`. Empty when tracing
    # produced no span, which is a state and not a failure.
    span_id: str = ""
    resumed_from_burst_id: str | None = None  # the parent node — recorded at spawn
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    exit_code: int | None = None
    model: str | None = None  # requested at spawn, overwritten by the resolved model on harvest
    cost_usd: float | None = None  # advisory under subscription billing
    input_tokens: int | None = None
    output_tokens: int | None = None
    num_turns: int | None = None
    session_id: str | None = None
    session_key: str | None = None  # this burst's snapshot in the archive
    session_checksum: str | None = None  # its sha256; the ledger is the authority
    # Which contributed subscription paid for this burst (F2), stamped at spawn.
    # NULL is the honest value for every burst that ran on the machine's own
    # credential — the whole MVP, and any deployment with an empty pool.
    subscription_id: str | None = None
    # Set when the scheduler abandons this node (H5): the attempt ran and did not
    # work, so no burst resumes it — the next one branches from its parent. The
    # value is the branch summary that branch is shown; None is a live node.
    # Written only by `abandon_burst`, never by `finish_burst`: abandoning is the
    # rail's judgement about a finished burst, not something the burst learned.
    branch_summary: str | None = None


# What `finish_burst` writes — everything a burst learns by ending. Shared by both
# backends on purpose: the CAS is the one statement they both hand-write, and the
# list drifting between them would be invisible until a field silently stopped
# being persisted on one of the two.
BURST_FINALIZE_COLUMNS = (
    "status",
    "finished_at",
    "exit_code",
    "model",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "num_turns",
    "session_id",
    "session_key",
    "session_checksum",
)


@dataclass(slots=True)
class Violation:
    """A wall block or tripwire firing — the experiment's primary dataset (§9)."""

    task_id: str
    burst_id: str
    tool: str
    reason: str
    detail: str = ""
    at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Event:
    """One outbox row: what the platform tells the outside world happened.

    Read by cursor (`id` is the consumer's position) and never by subscription —
    a push may wake a consumer early, the poll is the truth (STORES.md). The
    payload carries only what routing an alert needs; the row it announces is
    there to be read by whoever wants the rest.
    """

    id: int
    kind: str
    payload: dict
    created_at: str


# Event kinds, as they appear in `events.kind`. The set grows with the stages
# that emit them (capacity_exhausted at F2, subscription_expiring at F4,
# task_blocked with the scheduler's own events) — this is the one A4 ships,
# because the violations feed is the alert signal the platform documents
# (TO-BE §12: any new violations row is a page — the three forms an adopter can
# consume it in are OBSERVABILITY.md, "The alert condition").
EVENT_VIOLATION_RECORDED = "violation_recorded"

# The registry's announcements (C1): one kind per operator verb, written in the
# same statement as the mutation and its audit row. The payload names the audit
# row (`audit_id`) so a consumer can read the who/why without a second contract.
EVENT_PRODUCT_REGISTERED = "product_registered"
EVENT_PRODUCT_PAUSED = "product_paused"
EVENT_PRODUCT_RESUMED = "product_resumed"
EVENT_PRODUCT_UPDATED = "product_updated"
EVENT_ROLE_UPDATED = "role_updated"  # `role set` (D5): payload names the role and the changed columns
# The subscription pool's verbs (F1): register, revoke, set — same discipline as
# the registry's, one kind per verb. The payload names the subscription and its
# audit row; it never names the ref, and could not name a value.
EVENT_SUBSCRIPTION_REGISTERED = "subscription_registered"
EVENT_SUBSCRIPTION_REVOKED = "subscription_revoked"
EVENT_SUBSCRIPTION_EXPIRED = "subscription_expired"
EVENT_SUBSCRIPTION_UPDATED = "subscription_updated"
# The pool had nothing to give (F2): every contributed subscription is spent,
# backing off, expired or revoked, so a task that was ready to run did not. The
# payload carries the repository and how many rows were looked at — adopters
# route it, because capacity is a people question (TO-BE §4), not a pager one.
EVENT_CAPACITY_EXHAUSTED = "capacity_exhausted"
# A contributed token is running out of year (F4) — one per subscription per day
# through the final `EXPIRY_NOTICE_DAYS`. Renewal is a person's errand, so this
# is addressed to a person: the payload names the member and the days left.
EVENT_SUBSCRIPTION_EXPIRING = "subscription_expiring"
EVENT_SUBSCRIPTION_RENEWED = "subscription_renewed"
# The org STOP (E3): `stop` and `resume`, same shape — the mutation, its audit row
# and this announcement in one statement. `org_stopped` also carries the reason,
# so the page it becomes needs no second read.
EVENT_ORG_STOPPED = "org_stopped"
EVENT_ORG_RESUMED = "org_resumed"
# A product woken by a webhook (E5): the receiver's forward landed, the row's
# observation lease was cleared, and the fleet will poll it now rather than at
# the interval. Payload: `repository`, `kind` (the GitHub event name) and the
# `delivery` id — the same id the receiver logged, so the two logs and this row
# line up, and `created_at` → the task's first burst is the ready-to-claim
# latency E6 measures. Webhooks are wake-ups; polls stay the truth (TO-BE §5).
EVENT_PRODUCT_WOKEN = "product_woken"

# The optional wake channel. A consumer may LISTEN on it to poll sooner; nothing
# is allowed to depend on a notification arriving.
EVENT_CHANNEL = "events"
# The replicas' own wake channel (E5): `wake_repository` notifies it with the
# repository as payload, and every replica's loop LISTENs, so a webhook that
# reached one replica's endpoint cuts every replica's sleep. Same rule as above
# — a lost notification costs latency (the next tick polls anyway), never truth.
WAKE_CHANNEL = "wakes"

# How long a wake settles before anyone looks (E6). GitHub delivers the webhook
# for a write before its own read side shows that write: the gate rehearsal saw
# a `ready` label listed 0.75 s after it landed, and the poll the wake fired at
# 0.6 s missed it — then held the observation lease for a whole interval, so the
# wake had made the claim *later*, not sooner. A wake therefore reserves the
# product's observation for these seconds (nobody polls a state that is still
# settling) and the woken loops sleep as long before their tick. Measured lag
# with margin, not a knob; a wake that lands inside a pending window joins it.
WAKE_SETTLE_S = 2.0
