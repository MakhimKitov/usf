-- The ledger's schema as desired state — the executable copy of STORES.md's
-- "Schema — canonical DDL", and the input Atlas diffs the migration directory
-- against (see atlas.hcl, and ARCHITECTURE.md §12 for why).
--
-- HOW THIS FILE IS USED. Nothing applies it. It is the *statement* of what the
-- schema should be; `packages/control-plane/src/control_plane/migrations/` is the
-- ordered path that gets a database there, and `control-plane migrate` is what
-- walks it. To change the schema: edit this file, ask Atlas for the difference,
-- and save the answer as the next numbered migration:
--
--   atlas schema diff --env local --var dev_url="docker://postgres/18/dev"
--
-- CI then asserts the two agree — a migrated database diffed against this file
-- must come back empty. That check is the entire point: before it existed, "the
-- DDL is authoritative" was a claim about a markdown block that nothing verified.
--
-- WHAT THIS FILE CANNOT SAY. Atlas's community version does not read triggers,
-- functions, privileges, or table data, so four of the walls are deliberately
-- absent here and are verified by behavior instead: the `roles_pipeline_guard`
-- trigger, the append-only grants on violations/audit/events, the rest of
-- `platform_app`'s grants, and the seeded pipeline role rows. A4 (#15) provokes
-- each of them in `tests/test_schema_walls.py`, which is a better proof than a
-- schema comparison anyway. Views are the fifth, and louder than the others: Atlas
-- does not merely ignore a `CREATE VIEW`, it refuses the file that contains one
-- ("views are available to logged-in users only"), so B3's reporting views live in
-- their migration and are tested by querying them (see the end of this file). Do not read this file as the whole truth about the
-- database — read it as the whole truth about its tables, columns, indexes and
-- constraints.
--
-- One more blind spot, and a sharp one: a constraint added to a live table lands
-- `NOT VALID` and is validated in a later migration (0002 → 0003). Atlas calls a
-- database with that second step missing *synced* — it compares the constraints
-- but not pg_constraint.convalidated, so a wall enforced only for new rows looks
-- identical to one that holds for all of them. The declarations below are the
-- validated form; test_no_constraint_is_left_unvalidated is what makes the
-- database match them.
--
-- Shape note: still converging on STORES.md's target. Landed: org tables and
-- walls (A4), the session tree (A5), composite task identity (A6), the
-- tasks→repositories foreign key with the registry that can create a row (C1),
-- the task lease columns with the claim statements that use them (E1), the
-- `org_state` STOP row (E3), the subscription pool's walls with the verbs that
-- write it (F1) and `bursts.subscription_id` with the pick that stamps it (F2). Outstanding: `bursts.pinned` with the golden-replay
-- harness that needs it at G4 (#45). It is an addition to a *live* table, which
-- is why it waits for the stage that needs it rather than landing speculatively
-- here — `bursts.subscription_id` arrived that way at F2 (#39), in 0018/0019.

-- repositories — one row per registered product; implements TO-BE §2's
-- product model (product:repository is 1:1 by DR). Defaults are materialized,
-- not inherited: `product register` (C1) fills every policy/quota column from
-- platform config unless overridden; the row is the entire truth, no
-- NULL-means-inherit, and a changed platform default never retro-propagates.
-- Rows are created by the registry verbs only — each mutation writes its audit
-- row and outbox event in the same statement.
CREATE TABLE repositories (
    repository      text PRIMARY KEY,          -- 'owner/name'
    team            text NOT NULL,
    state           text NOT NULL CONSTRAINT repositories_state_check CHECK (state IN ('active', 'paused')),
    review_enabled  boolean NOT NULL,
    review_provider text NOT NULL,
    e2e_enabled     boolean NOT NULL,
    test_env_refs   jsonb NOT NULL,            -- name → secret ref, never a value
    concurrency     integer NOT NULL,
    daily_bursts    integer NOT NULL,
    usage_share     numeric NOT NULL,
    last_burst_at   timestamptz,               -- least-recently-served scheduling
    claimed_by      text,                      -- observation lease (E2)
    claimed_until   timestamptz,
    registered_at   timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- roles — ceilings in code, composition in data (TO-BE §4). The four pipeline
-- rows are seeded by migration and their wall-bearing fields are frozen by the
-- roles_pipeline_guard trigger. `identity` never accepts a pipeline App:
-- pipeline identity is engine code keyed on the seeded role name, which is why
-- every seeded row carries 'none'.
CREATE TABLE roles (
    role            text PRIMARY KEY,
    kind            text NOT NULL CONSTRAINT roles_kind_check CHECK (kind IN ('pipeline', 'custom')),
    prompt_preamble text NOT NULL,
    tools           jsonb NOT NULL,            -- must be ⊆ the code allowlist, checked at load
    model           text NOT NULL,
    effort          text,                      -- NULL: the CLI's default for the model; levels checked in code
    max_budget_usd  numeric NOT NULL,
    max_turns       integer NOT NULL,
    mount_mode      text NOT NULL,
    identity        text NOT NULL CONSTRAINT roles_identity_check CHECK (identity IN ('none', 'automation')),
    sink            text NOT NULL CONSTRAINT roles_sink_check CHECK (sink IN ('issue_comment', 'ledger_artifact', 'event')),
    trigger         jsonb,                     -- schedule/event; machinery lands at T4
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- subscriptions — the ref-only pool (TO-BE §4, F1). Usage is derived from bursts;
-- backoff_until is the pool's only mutable state, set by CAS on rate limiting,
-- cleared by time. The `secret_ref` CHECK is the pool's central claim made
-- structural: the column holds one of the resolver's three schemes followed by a
-- name, so a token value cannot become a row — and what no column holds, no
-- dump, log line or archive can leak. Rows are created by the pool verbs only,
-- each mutation writing its audit row and outbox event in the same statement.
CREATE TABLE subscriptions (
    subscription_id text PRIMARY KEY,          -- the member's handle, derived from it
    member          text NOT NULL,
    secret_ref      text NOT NULL CONSTRAINT subscriptions_secret_ref_check CHECK (secret_ref ~ '^(env|file|helper):.'),
    cap_tokens      bigint NOT NULL CONSTRAINT subscriptions_cap_tokens_check CHECK (cap_tokens > 0),
    expires_at      timestamptz NOT NULL,
    status          text NOT NULL CONSTRAINT subscriptions_status_check CHECK (status IN ('active', 'expired', 'revoked')),
    backoff_until   timestamptz,
    registered_at   timestamptz NOT NULL DEFAULT now()
);

-- tasks — one GitHub issue worked to completion. The id is derived, never
-- invented: '{owner}/{repo}#{issue_number}', so it is stable across replicas and
-- joins the GitHub world for free. `repository` holds the product slug; the
-- platform's clone path is a runtime argument and belongs in no row.
--
-- WALL: a task names a registered product (C1). The foreign key waited for the
-- verb that can create a `repositories` row — before `product register` existed,
-- requiring one would have made every task uninsertable.
CREATE TABLE tasks (
    task_id         text PRIMARY KEY,          -- '{owner}/{repo}#{issue_number}'
    repository      text NOT NULL CONSTRAINT tasks_repository_fk REFERENCES repositories (repository),
    issue_number    integer NOT NULL,
    status          text NOT NULL CONSTRAINT tasks_status_check CHECK (status IN
                      ('open', 'claimed', 'working', 'awaiting_review', 'blocked', 'done')),
    session_id      text,
    pr_number       integer,
    attempts        integer NOT NULL DEFAULT 0,
    review_rounds   integer NOT NULL DEFAULT 0,
    next_burst_kind text,
    last_review_id  bigint,
    last_burst_at   timestamptz,
    claimed_by      text,                      -- the lease pattern (E1): who holds the next burst, until when
    claimed_until   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- tasks_by_status(): WHERE status IN (…) ORDER BY created_at, task_id. The index
-- covers the filter and the leading sort key; the tie-break (A7 — an order that is
-- the query's, not the plan's) is a sort over the few rows a status match returns,
-- which is not worth a wider index.
CREATE INDEX tasks_status_created ON tasks (status, created_at);

-- WALL: one task per issue. The derived task_id already makes a duplicate hard to
-- represent; this makes it impossible, including for two replicas that compute the
-- same id at the same moment.
CREATE UNIQUE INDEX tasks_one_per_issue ON tasks (repository, issue_number);

-- bursts — history-preserving, not append-only: a row transitions while running
-- and is finalized exactly once (the CAS in the store layer, with the
-- running ⟺ finished_at CHECK behind it). `kind` is deliberately unconstrained:
-- it is scheduler vocabulary, and new kinds must not need a migration.
CREATE TABLE bursts (
    burst_id              text PRIMARY KEY,
    task_id               text NOT NULL CONSTRAINT bursts_task_fk REFERENCES tasks (task_id),
    kind                  text NOT NULL,
    role                  text NOT NULL CONSTRAINT bursts_role_fk REFERENCES roles (role),
    status                text NOT NULL CONSTRAINT bursts_status_check CHECK (status IN
                            ('running', 'succeeded', 'failed', 'killed')),
    -- the burst's trace and the id of the root span in it. Both, because a
    -- successor's span link names a span and not a trace (B1).
    trace_id              text NOT NULL DEFAULT '',
    span_id               text NOT NULL DEFAULT '',
    -- the session tree (A5): the parent node this burst resumed from, and the
    -- snapshot it left behind. Self-referencing, so lineage is a walk in SQL.
    resumed_from_burst_id text CONSTRAINT bursts_resumed_from_fk REFERENCES bursts (burst_id),
    session_id            text,
    session_key           text,   -- archive key of this burst's snapshot
    session_checksum      text,   -- sha256; the ledger is the checksum authority
    -- which contributed subscription paid for this burst (F2), stamped at the
    -- pick. NULL is the honest value for a burst that ran on the machine's own
    -- credential — the whole MVP, and any deployment with an empty pool.
    subscription_id       text CONSTRAINT bursts_subscription_fk REFERENCES subscriptions (subscription_id),
    -- set when the scheduler abandons this node (H5): the attempt ran and did
    -- not work, so no burst resumes it. The value is the branch summary the next
    -- branch is shown; NULL is a live node.
    branch_summary        text CONSTRAINT bursts_branch_summary_check CHECK (branch_summary <> ''),
    started_at            timestamptz NOT NULL DEFAULT now(),
    finished_at           timestamptz,
    exit_code             integer,
    model                 text,
    cost_usd              numeric,
    input_tokens          bigint,
    output_tokens         bigint,
    num_turns             integer,
    CONSTRAINT bursts_running_finished_check CHECK ((status = 'running') = (finished_at IS NULL))
);

-- bursts_started_since(): the rolling-window count behind the daily rail.
CREATE INDEX bursts_started_at ON bursts (started_at);

-- the pool's headroom question (F2): a subscription's bursts since a timestamp.
-- Partial — every burst that ran on the machine's own credential is NULL here,
-- and none of them is ever an answer.
CREATE INDEX bursts_subscription_started_idx ON bursts (subscription_id, started_at)
    WHERE subscription_id IS NOT NULL;

-- latest_archived_burst(): the leaf of a task's session tree, materialized into
-- the next burst's volume. Partial on exactly the query's predicate — archived,
-- and the dev's. The role filter is the B5 wall in miniature: a tester burst
-- archives its volume as *evidence*, and must never become what the next dev
-- burst resumes. Reviewer bursts archive nothing and never enter. An abandoned
-- node (H5) is not a leaf either: the next burst branches from its parent.
CREATE INDEX bursts_session_leaf ON bursts (task_id, started_at)
    WHERE session_key IS NOT NULL AND role = 'dev' AND branch_summary IS NULL;

-- WALL: sessions are single-writer — at most one running burst per task, as a
-- constraint, not a scheduler promise. Any claiming bug becomes a constraint
-- violation, never a doubled burst. Serves running_bursts() too, which is why
-- that query has no index of its own — it sorts its handful of rows by
-- (started_at, burst_id) so both backends answer in the same order (A7).
CREATE UNIQUE INDEX bursts_one_running_per_task ON bursts (task_id)
    WHERE status = 'running';

-- violations — wall blocks and tripwire firings; the experiment's primary
-- dataset. Append-only by grant. No foreign keys on purpose: a violation is
-- evidence and outlives whatever it was recorded against.
CREATE TABLE violations (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id  text NOT NULL,
    burst_id text NOT NULL,
    tool     text NOT NULL,
    reason   text NOT NULL,
    detail   text NOT NULL DEFAULT '',
    at       timestamptz NOT NULL DEFAULT now()
);

-- violations(task_id) — the experiment's primary dataset, always read by task
CREATE INDEX violations_task ON violations (task_id);

-- audit — every operator mutation (actor, action, target, reason), starting
-- with `product register` (TO-BE §2). Append-only by grant.
CREATE TABLE audit (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor  text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    reason text NOT NULL DEFAULT '',
    at     timestamptz NOT NULL DEFAULT now()
);

-- events — the outbox (STORES.md). id is the consumer's replay cursor; rows are
-- written in the same statement as the mutation they announce. Append-only by
-- grant; pruning is platform_admin's.
CREATE TABLE events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- memory_entries — a datum, not an instruction (TO-BE §9): auto-class content
-- under the provenance gate, scope-filtered at prompt assembly.
CREATE TABLE memory_entries (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope             text NOT NULL CONSTRAINT memory_entries_scope_check CHECK (scope IN ('product', 'platform', 'role')),
    repository        text CONSTRAINT memory_entries_repository_fk REFERENCES repositories (repository),
    role              text CONSTRAINT memory_entries_role_fk REFERENCES roles (role),
    claim             text NOT NULL,
    evidence          text NOT NULL,
    proposed_by_burst text CONSTRAINT memory_entries_burst_fk REFERENCES bursts (burst_id),  -- provenance
    status            text NOT NULL CONSTRAINT memory_entries_status_check CHECK (status IN ('proposed', 'active', 'retired')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memory_entries_product_scope_check CHECK ((scope = 'product') = (repository IS NOT NULL)),
    CONSTRAINT memory_entries_role_scope_check CHECK ((scope = 'role') = (role IS NOT NULL))
);

-- org_state — exactly one row. STOP moves from the filesystem into the ledger
-- (TO-BE §5, E3): non-null stop_reason means no ticks, no spawns, from any
-- replica, and every live burst's heartbeat fences on it. The boolean key that
-- must be true is what makes the row single; the shape CHECK makes a stop carry
-- who and when or not exist. Seeded by migration (0016), never inserted or
-- deleted again — platform_app holds SELECT and UPDATE on it and nothing else.
CREATE TABLE org_state (
    id          boolean PRIMARY KEY DEFAULT true CONSTRAINT org_state_singleton_check CHECK (id),
    stop_reason text,
    stopped_by  text,
    stopped_at  timestamptz,
    CONSTRAINT org_state_stop_shape_check
        CHECK ((stop_reason IS NULL) = (stopped_by IS NULL) AND (stop_reason IS NULL) = (stopped_at IS NULL))
);


-- ============================================================ reporting (B3)
--
-- The schema is declared here; its four views are not, and cannot be. Atlas's
-- community version refuses them outright — `views are available to logged-in
-- users only` — so a `CREATE VIEW` in this file breaks the drift check rather
-- than feeding it. They join the blind spots above: stated in
-- `migrations/0010_reporting-views.sql`, which is where their definitions and the
-- reasoning behind each metric live, and verified by behavior in
-- `tests/test_reporting_views.py` — seeded rows in, known numbers out, which is a
-- better proof of a metric than a comparison of its text anyway.
--
-- The schema itself has to be here: Atlas does read schemas and their comments, so
-- without these two statements a migrated database looks like drift and CI's check
-- proposes `DROP SCHEMA reporting CASCADE`.
CREATE SCHEMA reporting;

COMMENT ON SCHEMA reporting IS
    'Read-only views over the ledger: the ARCHITECTURE §9 metric families (B3).';
