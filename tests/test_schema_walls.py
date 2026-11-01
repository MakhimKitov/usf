"""The walls, provoked (A4, #15).

STORES.md's "Walls in the schema" table names the guarantees the database is
supposed to enforce. This file attacks each of them and requires an error. That
is not belt-and-braces: two of them — the `roles_pipeline_guard` trigger and
the append-only grants — are invisible to `atlas schema diff`, which reads
neither triggers nor privileges, so these tests are the *only* thing standing
behind them. A migration that dropped the trigger would pass CI's drift check.

The style throughout is: one statement, in its own transaction, expected to fail
with a specific error class. Raw SQL rather than the `Ledger` protocol, because a
wall's job is to stop writes the protocol would never make — the store layer is
careful, and the point of a wall is what happens when something isn't.

Two things worth knowing about the setup:

* `SET LOCAL ROLE platform_app` is how the grant walls are tested. It needs the
  migrating role to be a superuser or a member of platform_app, which is true of
  the dev rig and of CI (both make platform_admin the cluster's superuser) and is
  not assumed anywhere in the platform's own code.
* Postgres only. The single-node SQLite variant does not carry these walls, and
  deliberately: SQLite cannot add a constraint to an existing table, so a wall put
  in its bootstrap DDL would exist in ledgers created after the change and be
  missing from every older file, with nothing able to tell you which you hold.
  A6 (#17) retires that ledger as an origin. The races these walls catch need two
  writers, and the SQLite variant is one process by construction.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import migrate
from control_plane.models import BurstStatus, TaskStatus, Violation, task_id_for
from control_plane.store import LEDGER_DSN_ENV
from psycopg.conninfo import make_conninfo

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()

pytestmark = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset — no database to provoke")

PIPELINE_ROLES = ("dev", "reviewer", "tester", "monitor")

# Enough of a role row to insert one; the columns are all NOT NULL.
CUSTOM_ROLE = (
    "INSERT INTO roles (role, kind, prompt_preamble, tools, model, max_budget_usd, "
    "max_turns, mount_mode, identity, sink) VALUES "
    "(%s, 'custom', '', '[]', 'claude-sonnet-5', 1.0, 10, 'ro', %s, %s)"
)


@pytest.fixture(scope="module")
def walls_dsn():
    """One migrated database for this module, dropped afterwards."""
    name = f"schema_walls_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(DSN, dbname=name)
    migrate(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def db(walls_dsn):
    """An autocommit connection over an empty ledger, seeded roles intact.

    The two products the suite's tasks name are re-seeded raw: the registry wall
    (C1) makes a task row need them, and raw INSERTs — not the verb — keep the
    audit/events tables clean for the tests that provoke their grants.
    """
    with psycopg.connect(walls_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries, repositories, audit")
        conn.execute("UPDATE org_state SET stop_reason = NULL, stopped_by = NULL, stopped_at = NULL")  # a previous test's stop (E3)
        conn.execute("DELETE FROM roles WHERE kind = 'custom'")  # a previous test's leftovers
        conn.execute(  # …and a previous test's `role set` — the open fields back to seed (D5)
            "UPDATE roles SET prompt_preamble = '', model = 'claude-sonnet-5', max_budget_usd = 2.00, "
            "max_turns = 100, mount_mode = CASE WHEN role = 'dev' THEN 'rw' ELSE 'ro' END "
            "WHERE kind = 'pipeline'"
        )
        conn.execute(
            "INSERT INTO repositories (repository, team, state, review_enabled, review_provider, "
            "e2e_enabled, test_env_refs, concurrency, daily_bursts, usage_share) VALUES "
            "('owner/product', 'owner', 'active', false, '', false, '{}', 1, 20, 1.0), "
            "('owner/other', 'owner', 'active', false, '', false, '{}', 1, 20, 1.0)"
        )
        yield conn


def attempt(conn, sql, params=None, *, role=None):
    """One statement, in its own transaction, optionally as another role.

    The transaction is what keeps a refused statement from poisoning the next
    one, and `SET LOCAL ROLE` reverts with it.
    """
    with conn.transaction():
        if role is not None:
            conn.execute(f"SET LOCAL ROLE {role}")
        return conn.execute(sql, params)


REPOSITORY = "owner/product"


def a_task(conn, task_id=None, status=TaskStatus.OPEN, repository=REPOSITORY, issue_number=1):
    """A task row, keyed the way the platform keys them (A6): derived, not typed."""
    task_id = task_id or task_id_for(repository, issue_number)
    attempt(
        conn,
        "INSERT INTO tasks (task_id, repository, issue_number, status) VALUES (%s, %s, %s, %s)",
        (task_id, repository, issue_number, status),
    )
    return task_id


def a_running_burst(conn, burst_id, task_id=None, role="dev", kind="work"):
    task_id = task_id or task_id_for(REPOSITORY, 1)
    attempt(
        conn,
        "INSERT INTO bursts (burst_id, task_id, kind, role, status) VALUES (%s, %s, %s, %s, 'running')",
        (burst_id, task_id, kind, role),
    )
    return burst_id


def finish(conn, burst_id):
    attempt(
        conn,
        "UPDATE bursts SET status = 'succeeded', finished_at = now() WHERE burst_id = %s",
        (burst_id,),
    )


# ------------------------------------------------------------ one task per issue


def test_one_task_per_issue(db):
    """The wall A4's scope named and A6 could finally build, since it needs the
    column A6 adds. The derived id already makes a duplicate hard to represent;
    this is what makes it impossible — including for two replicas deriving the same
    id in the same instant."""
    a_task(db, task_id="owner/product#7", repository="owner/product", issue_number=7)

    with pytest.raises(psycopg.errors.UniqueViolation, match="tasks_one_per_issue"):
        attempt(
            db,
            "INSERT INTO tasks (task_id, repository, issue_number, status) "
            "VALUES ('owner/product#7-again', 'owner/product', 7, 'open')",
        )

    # …and the same issue number under a different product is a different task
    a_task(db, task_id="owner/other#7", repository="owner/other", issue_number=7)


def test_a_task_always_names_its_issue(db):
    """NOT NULL, arrived at without a table scan (0007 + 0008). A task whose issue
    is unknown has no derived id, so there is nothing for the wall above to be
    unique about."""
    with pytest.raises(psycopg.errors.NotNullViolation, match="issue_number"):
        attempt(
            db,
            "INSERT INTO tasks (task_id, repository, status) "
            "VALUES ('owner/product#8', 'owner/product', 'open')",
        )


def test_a_task_names_a_registered_product(db):
    """The registry wall (C1): `tasks.repository` is a foreign key into
    `repositories`, so a task for a product nobody registered is unrepresentable
    — the scheduler cannot invent tenants, and neither can a bug."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="tasks_repository_fk"):
        attempt(
            db,
            "INSERT INTO tasks (task_id, repository, issue_number, status) "
            "VALUES ('ghost/none#1', 'ghost/none', 1, 'open')",
        )


def test_the_clone_path_has_nowhere_to_go(db):
    """`repo` is gone, not renamed-and-kept: the platform's clone path is a fact
    about one machine and belongs in no row (A6). A migration that reintroduced it
    would make this pass, which is the point of asserting it."""
    columns = db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks'"
    ).fetchall()
    names = {row[0] for row in columns}
    assert "repo" not in names
    assert {"repository", "issue_number"} <= names
    assert not {"transcript_key", "transcript_checksum"} & names  # A5's snapshot moved to bursts


# ------------------------------------------------- one running burst per task


def test_only_one_burst_runs_per_task(db):
    """The wall that makes a doubled burst unrepresentable rather than unlikely."""
    a_task(db)
    a_running_burst(db, "b1")

    with pytest.raises(psycopg.errors.UniqueViolation, match="bursts_one_running_per_task"):
        a_running_burst(db, "b2")


def test_the_handoff_still_works(db):
    """The index is partial, so history piles up freely — it is only *running*
    rows that are unique. A task's next burst starts as soon as the last one has
    finished, which is the whole scheduler loop.

    (Observed while building this: a single data-modifying CTE that finalizes one
    burst and inserts the next trips the index unless the INSERT consumes the
    UPDATE's RETURNING. Postgres does not order unreferenced CTEs, so the two race
    against the same index. The store layer's two statements are unaffected, and
    anything combining them later must carry the data dependency.)"""
    a_task(db)
    a_running_burst(db, "b1")
    finish(db, "b1")
    a_running_burst(db, "b2")
    finish(db, "b2")
    a_running_burst(db, "b3")

    rows = db.execute("SELECT count(*) FROM bursts WHERE task_id = 'owner/product#1'").fetchone()
    assert rows[0] == 3


# ------------------------------------------------------ finalize exactly once


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("finished without a finish time", "UPDATE bursts SET status = 'succeeded' WHERE burst_id = 'b1'"),
        ("running with one", "UPDATE bursts SET finished_at = now() WHERE burst_id = 'b1'"),
    ],
)
def test_a_burst_is_never_half_finished(db, label, sql):
    """The CAS in the store layer makes the second finalize a no-op; this is what
    makes it structural. No statement can leave a row in a state that says both
    "still running" and "over" — the pair moves together or not at all."""
    a_task(db)
    a_running_burst(db, "b1")

    with pytest.raises(psycopg.errors.CheckViolation, match="bursts_running_finished_check"):
        attempt(db, sql)


def test_an_abandoned_node_says_why(db):
    """H5 (0021): the abandon mark *is* the branch summary, so an empty one — a
    branch that forgets why it exists — is not a value the column takes."""
    a_task(db)
    a_running_burst(db, "b1")
    finish(db, "b1")

    with pytest.raises(psycopg.errors.CheckViolation, match="bursts_branch_summary_check"):
        attempt(db, "UPDATE bursts SET branch_summary = '' WHERE burst_id = 'b1'")


def test_a_finished_burst_cannot_be_reopened(db):
    a_task(db)
    a_running_burst(db, "b1")
    finish(db, "b1")

    with pytest.raises(psycopg.errors.CheckViolation, match="bursts_running_finished_check"):
        attempt(db, "UPDATE bursts SET finished_at = NULL WHERE burst_id = 'b1'")


# --------------------------------------------- enumerations and the code's own


@pytest.mark.parametrize("status", list(TaskStatus))
def test_every_task_status_the_code_writes_is_allowed(db, status):
    """The CHECK and the StrEnum have to agree; a status the scheduler sets and
    the database rejects would be a wall in the wrong place."""
    a_task(db, task_id=f"t-{status}", status=status)


@pytest.mark.parametrize("status", list(BurstStatus))
def test_every_burst_status_the_code_writes_is_allowed(db, status):
    a_task(db)
    ended = None if status == BurstStatus.RUNNING else datetime(2026, 8, 14, tzinfo=UTC)
    attempt(
        db,
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, finished_at) "
        "VALUES (%s, 'owner/product#1', 'work', 'dev', %s, %s)",
        (f"b-{status}", status, ended),
    )


def test_a_status_nobody_implements_is_refused(db):
    with pytest.raises(psycopg.errors.CheckViolation, match="tasks_status_check"):
        a_task(db, status="vibing")

    # …and with a finish time, since a nonsense status with none trips the
    # running ⟺ finished_at CHECK first: the two interlock, and Postgres reports
    # whichever it reaches. This provokes the status CHECK alone.
    a_task(db)
    with pytest.raises(psycopg.errors.CheckViolation, match="bursts_status_check"):
        attempt(
            db,
            "INSERT INTO bursts (burst_id, task_id, kind, role, status, finished_at) "
            "VALUES ('b1', 'owner/product#1', 'work', 'dev', 'vibing', now())",
        )


# ------------------------------------------------------- the subscription pool


def a_subscription(conn, secret_ref="env:MAKSIM_CLAUDE_TOKEN", cap=12_000_000, member="maksim"):
    return attempt(
        conn,
        "INSERT INTO subscriptions (subscription_id, member, secret_ref, cap_tokens, "
        "expires_at, status) VALUES (%s, %s, %s, %s, now() + interval '1 year', 'active')",
        (member, member, secret_ref, cap),
    )


def test_the_pool_column_holds_a_ref_and_cannot_hold_a_token(db):
    """F1's claim, in the schema (0017): `secret_ref` is one of the resolver's
    three schemes followed by a name. A verb refuses a value first; this is what
    a console, a psql session or a bug meets instead. What no column can hold,
    no dump can leak."""
    for scheme in ("env:MAKSIM_CLAUDE_TOKEN", "file:/run/secrets/maksim", "helper:maksim"):
        a_subscription(db, secret_ref=scheme, member=scheme[:4])
    for not_a_ref in ("sk-ant-oat01-A-REAL-LOOKING-TOKEN", "MAKSIM_CLAUDE_TOKEN", "vault:maksim", "env:", ""):
        with pytest.raises(psycopg.errors.CheckViolation, match="subscriptions_secret_ref_check"):
            a_subscription(db, secret_ref=not_a_ref)


def test_a_cap_is_capacity(db):
    for cap in (0, -1):
        with pytest.raises(psycopg.errors.CheckViolation, match="subscriptions_cap_tokens_check"):
            a_subscription(db, cap=cap)


def test_a_subscription_ends_in_a_status_the_pool_speaks(db):
    a_subscription(db)
    for status in ("expired", "revoked", "active"):
        attempt(db, "UPDATE subscriptions SET status = %s WHERE subscription_id = 'maksim'", (status,))
    with pytest.raises(psycopg.errors.CheckViolation, match="subscriptions_status_check"):
        attempt(db, "UPDATE subscriptions SET status = 'lapsed' WHERE subscription_id = 'maksim'")


def test_burst_kind_is_deliberately_open(db):
    """`kind` is scheduler vocabulary — 'assess' arrived without a migration and
    the next one should too. The absence of a CHECK here is a decision."""
    a_task(db)
    a_running_burst(db, "b1", kind="archaeology")


def test_a_burst_points_at_a_real_task_and_a_real_role(db):
    a_task(db)

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="bursts_task_fk"):
        a_running_burst(db, "b1", task_id="no-such-task")

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="bursts_role_fk"):
        a_running_burst(db, "b2", role="ghost")


def test_a_burst_descends_from_a_burst_that_exists(db):
    """The session tree's edge is a foreign key (A5): a parent that isn't a burst
    is not a parent, so lineage can be walked without checking whether each hop
    is real."""
    a_task(db)

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="bursts_resumed_from_fk"):
        attempt(
            db,
            "INSERT INTO bursts (burst_id, task_id, kind, role, status, resumed_from_burst_id) "
            "VALUES ('b1', 'owner/product#1', 'work', 'dev', 'running', 'no-such-burst')",
        )

    a_running_burst(db, "b1")
    finish(db, "b1")
    attempt(
        db,
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, resumed_from_burst_id) "
        "VALUES ('b2', 'owner/product#1', 'work', 'dev', 'running', 'b1')",
    )
    assert db.execute("SELECT resumed_from_burst_id FROM bursts WHERE burst_id='b2'").fetchone() == (
        "b1",
    )


def test_a_role_with_history_cannot_be_deleted(db):
    """Not the trigger this time — the plain foreign key. Even a custom role stops
    being disposable once a burst ran as it, because deleting it would leave
    history describing a role nobody can look up."""
    attempt(db, CUSTOM_ROLE, ("lens", "none", "issue_comment"))
    a_task(db)
    a_running_burst(db, "b1", role="lens")

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="bursts_role_fk"):
        attempt(db, "DELETE FROM roles WHERE role = 'lens'")


# ----------------------------------------------- the pipeline roles are frozen


def test_the_pipeline_roles_are_seeded(db):
    rows = db.execute(
        "SELECT role, kind, identity, sink, tools FROM roles WHERE kind = 'pipeline' ORDER BY role"
    ).fetchall()
    assert [row[0] for row in rows] == sorted(PIPELINE_ROLES)
    # identity 'none' for all four is the wall, not an omission: a pipeline role's
    # credential is engine code keyed on its name, and the column can only name
    # the shared automation App.
    assert {row[2] for row in rows} == {"none"}
    # tools '["*"]' — no narrowing, since no per-role allowlist exists in code yet
    assert all(row[4] == ["*"] for row in rows)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identity", "'automation'"),  # the funnel slot gets the wrong credential
        ("kind", "'custom'"),  # …or stops being a funnel slot at all
        ("sink", "'ledger_artifact'"),  # …or reports somewhere else
        ("tools", "'[\"Bash\"]'"),  # …or gains a tool the role never had
        ("role", "'dev2'"),  # renaming is the same escape: identity keys on the name
    ],
)
def test_a_pipeline_row_cannot_have_its_wall_fields_edited(db, field, value):
    with pytest.raises(psycopg.errors.RestrictViolation, match="frozen"):
        attempt(db, f"UPDATE roles SET {field} = {value} WHERE role = 'dev'")


def test_a_pipeline_row_cannot_be_deleted(db):
    """A row that can be dropped and re-inserted is not frozen."""
    with pytest.raises(psycopg.errors.RestrictViolation, match="cannot be deleted"):
        attempt(db, "DELETE FROM roles WHERE role = 'reviewer'")


def test_the_open_fields_stay_open(db):
    """The other half of roles-as-rows, and D5's (#31) whole premise: composition
    is data. Preamble, model, budget, turns and mount are editable on a pipeline
    row — only the wall-bearing fields are not."""
    attempt(
        db,
        "UPDATE roles SET prompt_preamble = 'Be terse.', model = 'claude-opus-5', "
        "max_budget_usd = 9.50, max_turns = 42, mount_mode = 'rw' WHERE role = 'reviewer'",
    )
    row = db.execute(
        "SELECT prompt_preamble, model, max_budget_usd, max_turns FROM roles WHERE role = 'reviewer'"
    ).fetchone()
    assert row == ("Be terse.", "claude-opus-5", 9.50, 42)


def test_a_custom_role_is_an_ordinary_row(db):
    """The trigger fires on `OLD.kind = 'pipeline'` and nothing else: custom roles
    — §10's review lenses among them — are created, edited and dropped freely."""
    attempt(db, CUSTOM_ROLE, ("lens", "none", "issue_comment"))
    attempt(db, "UPDATE roles SET identity = 'automation', tools = '[\"Read\"]' WHERE role = 'lens'")
    attempt(db, "DELETE FROM roles WHERE role = 'lens'")
    assert db.execute("SELECT count(*) FROM roles WHERE role = 'lens'").fetchone()[0] == 0


@pytest.mark.parametrize("identity", ["dev-app", "reviewer", "tester-app", ""])
def test_identity_never_names_a_pipeline_app(db, identity):
    """Ceilings in code: the column takes 'none' or the shared automation App.
    A pipeline App is not a value it accepts, from any row, pipeline or custom."""
    with pytest.raises(psycopg.errors.CheckViolation, match="roles_identity_check"):
        attempt(db, CUSTOM_ROLE, ("lens", identity, "issue_comment"))


def test_sinks_come_from_the_platform_implemented_set(db):
    with pytest.raises(psycopg.errors.CheckViolation, match="roles_sink_check"):
        attempt(db, CUSTOM_ROLE, ("lens", "none", "slack"))


# --------------------------------------------------------------- memory scope


@pytest.mark.parametrize(
    ("scope", "columns", "values", "constraint"),
    [
        ("product", "", "", "memory_entries_product_scope_check"),
        ("role", "", "", "memory_entries_role_scope_check"),
        ("platform", ", role", ", 'dev'", "memory_entries_role_scope_check"),
        ("role", ", repository", ", NULL", "memory_entries_role_scope_check"),
    ],
)
def test_a_memory_entry_names_what_its_scope_requires(db, scope, columns, values, constraint):
    """Both CHECKs are biconditionals, so they catch the pointer missing *and*
    the pointer nobody asked for — a 'platform' claim scoped to a role is as
    wrong as a 'role' claim scoped to nothing."""
    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        attempt(
            db,
            f"INSERT INTO memory_entries (scope, claim, evidence, status{columns}) "
            f"VALUES (%s, 'c', 'e', 'proposed'{values})",
            (scope,),
        )


def test_a_well_scoped_memory_entry_is_accepted(db):
    attempt(
        db,
        "INSERT INTO memory_entries (scope, role, claim, evidence, status) "
        "VALUES ('role', 'dev', 'c', 'e', 'proposed')",
    )
    attempt(
        db,
        "INSERT INTO memory_entries (scope, claim, evidence, status) "
        "VALUES ('platform', 'c', 'e', 'proposed')",
    )


# ------------------------------------------------------- append-only by grant


@pytest.mark.parametrize("table", ["violations", "audit", "events"])
@pytest.mark.parametrize("verb", ["UPDATE {} SET at = now()", "DELETE FROM {}"])
def test_the_evidence_tables_are_append_only(db, table, verb):
    """A grant, not a promise. The runtime that records evidence cannot revise it,
    and the outbox it writes cannot be un-written. Invisible to the drift check:
    Atlas reads no privileges, so this test is the whole guard."""
    statement = verb.format(table)
    if table == "events" and "at = now()" in statement:
        statement = "UPDATE events SET created_at = now()"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        attempt(db, statement, role="platform_app")


@pytest.mark.parametrize(
    "table", ["tasks", "bursts", "repositories", "roles", "org_state", "subscriptions"]
)
def test_the_runtime_cannot_delete_its_own_state(db, table):
    """No DELETE grant anywhere: retention is platform_admin's, on a horizon —
    unregistering a product is not a verb (C1 granted INSERT/UPDATE only), and
    neither is dropping a role (D5 granted UPDATE only) or the org row (E3
    granted UPDATE only — the kill switch can be flipped, never removed). A
    revoked subscription stays for the same reason (F1, 0017): the bursts it
    paid for still name it."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        attempt(db, f"DELETE FROM {table}", role="platform_app")


@pytest.mark.parametrize(
    ("what", "statement"),
    [
        (
            "a trust slot",
            "INSERT INTO roles (role, kind, prompt_preamble, tools, model, max_budget_usd, "
            "max_turns, mount_mode, identity, sink) VALUES "
            "('shadow', 'pipeline', '', '[]', 'claude-sonnet-5', 1.0, 10, 'rw', 'none', 'event')",
        ),
        (
            "a second org row",
            "INSERT INTO org_state (id) VALUES (false)",
        ),
    ],
)
def test_the_runtime_cannot_register_what_operators_register(db, what, statement):
    """Role composition is an operator verb, and each verb grows the app role a
    grant in the stage that ships it — until then the runtime reads and nothing
    more. Products left this list at C1 (registry verbs) and the subscription
    pool at F1 (0017, see test_the_pool_verbs_run_under_the_runtime_grants);
    roles gained UPDATE at D5 (`role set`) and *only* UPDATE: a new row is a
    trust slot or a custom role, and neither is the runtime's to create until T4
    can run one."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        attempt(db, statement, role="platform_app")


def test_the_registry_verbs_run_under_the_runtime_grants(db, walls_dsn):
    """The C1 grant, exercised by the real methods as the real role: each verb is
    one CTE writing repositories + audit + events and returning through
    pg_notify, so like the outbox statement it is checked per table *and* per
    returned column — the statement most likely to work as platform_admin in a
    test and fail as platform_app in production."""
    from control_plane.models import RepositoryRecord
    from control_plane.postgres import PostgresLedger

    ledger = PostgresLedger(walls_dsn)
    try:
        ledger._conn.execute("SET ROLE platform_app")  # for the rest of the session
        ledger.register_repository(
            RepositoryRecord(repository="newly/registered", team="bots"), actor="op"
        )
        ledger.set_repository_state("newly/registered", "paused", "op", "rehearsal")
        ledger.update_repository("newly/registered", {"concurrency": 2}, "op")

        assert [a.action for a in ledger.audit_trail("newly/registered")] == [
            "product.register", "product.pause", "product.set",
        ]
        assert ledger.get_repository("newly/registered").concurrency == 2
    finally:
        ledger.close()


def test_the_pool_verbs_run_under_the_runtime_grants(db, walls_dsn):
    """The F1 grant (0017), exercised by the real methods as the real role: each
    verb is one CTE writing subscriptions + audit + events through pg_notify. And
    the wall beneath the grant: a value where a ref belongs, sent past the
    grammar through the same connection, meets the column's CHECK — INSERT is
    granted, storing a token is not."""
    from control_plane.postgres import PostgresLedger
    from control_plane.subscriptions import SubscriptionError, declare

    ledger = PostgresLedger(walls_dsn)
    try:
        ledger._conn.execute("SET ROLE platform_app")  # for the rest of the session
        ledger.register_subscription(declare("ada", "env:ADA_CLAUDE_TOKEN", 5_000_000, "2027-09-01"), "op")
        ledger.update_subscription("ada", {"cap_tokens": 6_000_000}, "op", "bump")
        ledger.set_subscription_status("ada", "revoked", "op", "rotating the token")

        assert [a.action for a in ledger.audit_trail("ada")] == [
            "subscription.register", "subscription.set", "subscription.revoke",
        ]
        assert ledger.get_subscription("ada").cap_tokens == 6_000_000

        token = "sk-ant-oat01-PAST-THE-GRAMMAR"
        row = declare("bob", "env:BOB", 1_000, "2027-09-01")
        row.secret_ref = token
        with pytest.raises(SubscriptionError) as refused:
            ledger.register_subscription(row, "op")
        assert token not in str(refused.value)  # the row is in the DETAIL; it stays there
        assert ledger.get_subscription("bob") is None
    finally:
        ledger.close()


def test_the_role_verb_runs_under_the_runtime_grants(db, walls_dsn):
    """The D5 grant (0014), exercised by the real method as the real role: one
    CTE writing roles + audit + events through pg_notify. And the wall beneath
    the grant: a wall field through the same connection meets the trigger, not
    a privilege error — UPDATE is granted, *becoming* something else is not."""
    from control_plane.postgres import PostgresLedger
    from control_plane.roles import RoleError

    ledger = PostgresLedger(walls_dsn)
    try:
        ledger._conn.execute("SET ROLE platform_app")  # for the rest of the session
        ledger.update_role("reviewer", {"model": "claude-opus-5", "max_turns": 30}, "op", "cheaper")
        assert [a.action for a in ledger.audit_trail("reviewer")] == ["role.set"]
        assert ledger.get_role("reviewer").model == "claude-opus-5"
        # the store refuses by name first…
        with pytest.raises(RoleError, match="frozen"):
            ledger.update_role("reviewer", {"identity": "automation"}, "op")
        # …and the trigger stands behind it for a statement that skips the store
        with pytest.raises(psycopg.errors.RestrictViolation, match="frozen"):
            ledger._conn.execute("UPDATE roles SET identity = 'automation' WHERE role = 'reviewer'")
    finally:
        ledger.close()


def test_the_lease_statements_run_under_the_runtime_grants(db, walls_dsn):
    """E1's statements as the role replicas connect as: FOR UPDATE SKIP LOCKED
    needs UPDATE on the table (0002 grants it), the advisory functions need
    nothing, RETURNING * needs SELECT on every column — including the two 0015
    added. E2's observation lease is an UPDATE on repositories (0012 grants it).
    The real methods, not copies of their SQL."""
    from control_plane.models import TaskRecord
    from control_plane.postgres import PostgresLedger

    ledger = PostgresLedger(walls_dsn)
    try:
        ledger._conn.execute("SET ROLE platform_app")  # for the rest of the session
        task = TaskRecord(task_id=task_id_for(REPOSITORY, 1), repository=REPOSITORY, issue_number=1,
                          status=TaskStatus.CLAIMED)
        assert ledger.create_task(task) is True
        leased = ledger.claim_next(REPOSITORY, "replica-a", ceiling=20, since="2020-01-01T00:00:00+00:00",
                                   lease_s=90, cooldown_s=0, concurrency=1)
        assert leased.claimed_by == "replica-a"
        assert ledger.claim_observation(REPOSITORY, "replica-a", 60) is True
        assert ledger.renew_lease(leased.task_id, "replica-a", 90) is True
        moved = ledger.transition_task(
            leased.task_id, {"status": TaskStatus.WORKING}, expected_status=TaskStatus.CLAIMED,
            expected_holder="replica-a",
        )
        assert moved.status == TaskStatus.WORKING
        assert ledger.tasks_by_status(TaskStatus.WORKING, repository=REPOSITORY, orphaned=True) == []
        assert ledger.running_bursts(repository=REPOSITORY, orphaned=True) == []
    finally:
        ledger.close()


def test_the_memory_proposal_runs_under_the_runtime_grants(db, walls_dsn):
    """The H4 write (0023), exercised by the real method as the real role: a
    burst's proposal and an abandoned attempt's summary are both one INSERT
    into memory_entries. 0002 left the table SELECT-only and H4 shipped the
    write without the grant, so the method passed as platform_admin here and
    was refused as platform_app on the node — where the refusal took the
    replica down. And the wall beneath the grant: promotion is the
    gardener's, so the runtime cannot UPDATE what it proposed."""
    from control_plane.models import MemoryEntry
    from control_plane.postgres import PostgresLedger

    a_task(db)
    a_running_burst(db, "b1")
    ledger = PostgresLedger(walls_dsn)
    try:
        ledger._conn.execute("SET ROLE platform_app")  # for the rest of the session
        stored = ledger.propose_memory(
            MemoryEntry(scope="product", claim="c", evidence="e", repository=REPOSITORY, proposed_by_burst="b1")
        )
        assert stored.id is not None
        assert [(e.proposed_by_burst, e.status) for e in ledger.memory_entries()] == [("b1", "proposed")]
    finally:
        ledger.close()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        attempt(db, "UPDATE memory_entries SET status = 'active'", role="platform_app")


def test_the_runtime_can_do_its_job(db):
    """The other direction, and the one a too-tight grant would break: everything
    the scheduler actually does, done as the role replicas connect as."""
    attempt(
        db,
        "INSERT INTO tasks (task_id, repository, issue_number, status) "
        "VALUES ('owner/product#1', 'owner/product', 1, 'open')",
        role="platform_app",
    )
    attempt(db, "UPDATE tasks SET status = 'working' WHERE task_id = 'owner/product#1'", role="platform_app")
    attempt(
        db,
        "INSERT INTO bursts (burst_id, task_id, kind, role, status) VALUES ('b1','owner/product#1','work','dev','running')",
        role="platform_app",
    )
    attempt(
        db,
        "UPDATE bursts SET status='succeeded', finished_at=now() WHERE burst_id='b1'",
        role="platform_app",
    )
    attempt(
        db,
        "INSERT INTO violations (task_id, burst_id, tool, reason) VALUES ('owner/product#1','b1','Bash','WALL')",
        role="platform_app",
    )
    attempt(db, "INSERT INTO audit (actor, action, target) VALUES ('op','stop','owner/product#1')", role="platform_app")
    # C1: the registry verbs write repositories under the runtime grants
    attempt(
        db,
        "INSERT INTO repositories (repository, team, state, review_enabled, review_provider, "
        "e2e_enabled, test_env_refs, concurrency, daily_bursts, usage_share) "
        "VALUES ('owner/second', 'bots', 'active', false, '', false, '{}', 1, 20, 1.0)",
        role="platform_app",
    )
    attempt(db, "UPDATE repositories SET team = 'platform' WHERE repository = 'owner/second'", role="platform_app")
    # D5: `role set` writes the open fields of roles under the runtime grants
    attempt(db, "UPDATE roles SET max_budget_usd = 1.50 WHERE role = 'dev'", role="platform_app")
    for readable in ("roles", "repositories", "subscriptions", "memory_entries", "events"):
        attempt(db, f"SELECT count(*) FROM {readable}", role="platform_app")


def test_the_outbox_statement_runs_under_the_runtime_grants(db, walls_dsn):
    """`record_violation` is one data-modifying CTE writing two tables and
    returning through pg_notify. Grants are checked per table *and* per returned
    column, so this is the statement most likely to work as platform_admin in a
    test and fail as platform_app in production. Run the real method, as the real
    role — not a copy of its SQL."""
    from control_plane.postgres import PostgresLedger

    ledger = PostgresLedger(walls_dsn)
    try:
        ledger._conn.execute("SET ROLE platform_app")  # for the rest of the session
        db.execute(
            "INSERT INTO tasks (task_id, repository, issue_number, status) "
            "VALUES ('owner/product#1', 'owner/product', 1, 'open')"
        )
        ledger.record_violation(Violation(task_id="t1", burst_id="b1", tool="Bash", reason="WALL"))

        assert [v.tool for v in ledger.violations("t1")] == ["Bash"]
        events = ledger.events()
        assert [e.kind for e in events] == ["violation_recorded"]
        assert events[0].payload["task_id"] == "t1"
    finally:
        ledger.close()


# ------------------------------------------------------------ the org STOP row


@pytest.mark.parametrize(
    ("label", "sql", "error"),
    [
        ("a second row by default", "INSERT INTO org_state DEFAULT VALUES", psycopg.errors.UniqueViolation),
        ("a second row by key", "INSERT INTO org_state (id) VALUES (false)", psycopg.errors.CheckViolation),
        ("a stop without who", "UPDATE org_state SET stop_reason = 'x', stopped_at = now()", psycopg.errors.CheckViolation),
        ("a stop without when", "UPDATE org_state SET stop_reason = 'x', stopped_by = 'op'", psycopg.errors.CheckViolation),
        ("a who without a stop", "UPDATE org_state SET stopped_by = 'op'", psycopg.errors.CheckViolation),
    ],
)
def test_the_org_row_is_single_and_a_stop_is_whole(db, label, sql, error):
    """E3, structurally: the org has one STOP row — a boolean key that must be
    true admits no second — and a stop carries who and when or is not a state
    the row can hold. As platform_admin, so the grants are not what refuses."""
    assert db.execute("SELECT count(*) FROM org_state").fetchone()[0] == 1  # seeded by 0016
    with pytest.raises(error):
        attempt(db, sql)


def test_the_org_verbs_run_under_the_runtime_grants(db, walls_dsn):
    """The E3 grant (0016): SELECT for every tick, spawn and heartbeat, UPDATE
    for the two verbs — each a CTE writing org_state + audit + events through
    pg_notify, checked per table and per returned column by running the real
    methods as the real role."""
    from control_plane.postgres import PostgresLedger

    ledger = PostgresLedger(walls_dsn)
    try:
        ledger._conn.execute("SET ROLE platform_app")  # for the rest of the session
        assert ledger.org_stop() is None
        ledger.stop_org("op", "rehearsal")
        assert ledger.org_stop().reason == "rehearsal"
        ledger.resume_org("op")
        assert ledger.org_stop() is None
        assert [a.action for a in ledger.audit_trail("org")] == ["org.stop", "org.resume"]
    finally:
        ledger.close()


# ------------------------------------------------------------- the two-step


def test_no_constraint_is_left_unvalidated(db):
    """The guard on 0002's other half. A constraint added NOT VALID and never
    validated is enforced for new rows only, and every check we have is blind to
    it: squawk reads the SQL and sees a correct two-step, and `atlas schema diff`
    calls the database synced either way — measured, not assumed. This is what
    notices that 0003 (or its successor) was forgotten."""
    unvalidated = db.execute(
        "SELECT conrelid::regclass::text, conname FROM pg_constraint "
        "WHERE NOT convalidated ORDER BY conname"
    ).fetchall()
    assert unvalidated == []
