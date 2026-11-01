"""Roles as rows (TO-BE §4, D5): ceilings in code, composition in data.

The done-when, at the engine seam: editing an open field of a role row changes
the *next* burst `execute_burst` spawns; editing a wall field is refused — by
name in the store, and by the schema's trigger beneath it.
"""

import sqlite3

import pytest
from control_plane import roles
from control_plane.bursts import PlatformStores, execute_burst
from control_plane.models import RoleRecord, TaskRecord
from control_plane.roles import RoleError
from control_plane.runner import HostPhases, burst_env
from control_plane.store import SqliteLedger
from dev_agent import burst
from dev_agent import env as agentenv
from dev_agent.env import BurstEnv


class RecordingRunner(HostPhases):
    """Stands in for docker: keeps the spec the engine built, exits 0. The bytes
    phases are the real ones (G1) — the volume is what a role's mount governs."""

    def __init__(self, archive=None):
        self.specs = []
        self.archive = archive

    def run(self, spec, log_path=None, heartbeat=None):
        self.specs.append(spec)
        return 0


@pytest.fixture
def engine(tmp_path):
    stores = PlatformStores.open(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = RecordingRunner(archive=stores.archive)
    task = TaskRecord(task_id="owner/product#7", repository="owner/product", issue_number=7)

    def spawn(role="dev", prompt="fix the bug", **overrides):
        execute_burst(stores, task, repo, prompt, role=role, runner=runner, **overrides)
        return runner.specs[-1]

    return stores, spawn


# --- ceilings (code) ---


def test_the_field_split_is_the_contract():
    # what `role set` may touch vs. what is frozen — disjoint, and together the
    # whole row minus key-adjacent bookkeeping (trigger is T4's)
    assert set(roles.ROLE_SET_COLUMNS) == {
        "prompt_preamble", "model", "effort", "max_budget_usd", "max_turns", "mount_mode"
    }
    assert set(roles.ROLE_WALL_COLUMNS) == {"role", "kind", "identity", "sink", "tools"}
    assert not set(roles.ROLE_SET_COLUMNS) & set(roles.ROLE_WALL_COLUMNS)
    assert roles.PIPELINE_ROLES == ("dev", "reviewer", "tester", "monitor")
    assert roles.READ_ONLY_ROLES == {"reviewer", "tester", "monitor"}


def test_check_settable_refuses_walls_by_name():
    with pytest.raises(RoleError, match="frozen: identity, tools"):
        roles.check_settable({"tools": ["Bash"], "identity": "automation", "model": "x"})
    with pytest.raises(RoleError, match="not settable: trigger"):
        roles.check_settable({"trigger": {"schedule": "hourly"}})
    with pytest.raises(RoleError, match="nothing to set"):
        roles.check_settable({})


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"model": "  "}, "must name a model"),
        ({"max_budget_usd": 0}, "must be positive"),
        ({"max_turns": -1}, "must be positive"),
        ({"mount_mode": "rwx"}, "one of rw, ro"),
        ({"effort": "ludicrous"}, "effort is one of low, medium, high, xhigh, max"),
    ],
)
def test_check_settable_shapes_each_value(changes, why):
    with pytest.raises(RoleError, match=why):
        roles.check_settable(changes)


def test_unset_is_an_effort_value():
    # None hands the choice back to the CLI's default for the model — `role set
    # --effort default` — so it passes the gate the levels pass
    roles.check_settable({"effort": None})
    for level in agentenv.EFFORT_LEVELS:
        roles.check_settable({"effort": level})


def test_a_row_with_an_unknown_effort_refuses_to_shape_a_burst():
    # what stands behind a row edited past the verb: the levels have no CHECK
    # in the schema (an open field), so the load is where they are held
    with pytest.raises(RoleError, match="effort 'turbo'"):
        roles.validate(RoleRecord(role="dev", kind="pipeline", effort="turbo"))
    assert roles.validate(RoleRecord(role="dev", kind="pipeline", effort="max")).effort == "max"


def test_tools_must_stay_inside_the_code_allowlist():
    ok = RoleRecord(role="lens", kind="custom", tools=["Read", "Grep"])
    assert roles.validate(ok) is ok
    assert roles.validate(RoleRecord(role="dev", kind="pipeline")).tools == ["*"]
    with pytest.raises(RoleError, match="outside the code allowlist: Shell, mcp__browser"):
        roles.validate(RoleRecord(role="lens", kind="custom", tools=["Read", "Shell", "mcp__browser"]))
    with pytest.raises(RoleError, match="nothing at all"):
        roles.validate(RoleRecord(role="lens", kind="custom", tools=[]))


def test_the_mount_floor_narrows_and_never_widens():
    # a row can take the dev to read-only; it cannot give a reader the pen
    assert roles.repo_read_only(RoleRecord(role="dev", kind="pipeline", mount_mode="rw")) is False
    assert roles.repo_read_only(RoleRecord(role="dev", kind="pipeline", mount_mode="ro")) is True
    assert roles.repo_read_only(RoleRecord(role="reviewer", kind="pipeline", mount_mode="rw")) is True
    assert roles.repo_read_only(RoleRecord(role="tester", kind="pipeline", mount_mode="rw")) is True


def test_the_preamble_composes_ahead_of_the_task_never_instead():
    row = RoleRecord(role="dev", kind="pipeline", prompt_preamble="  Prefer pytest.  ")
    assert roles.compose_prompt(row, "fix #7") == "Prefer pytest.\n\nfix #7"
    assert roles.compose_prompt(RoleRecord(role="dev", kind="pipeline"), "fix #7") == "fix #7"


def test_retired_env_knobs_are_named_with_their_verb():
    stale = roles.retired_settings({"SCHEDULER_BURST_BUDGET_USD": "0.5", "TESTER_MODEL": " ", "E2E": "on"})
    assert stale == [
        "SCHEDULER_BURST_BUDGET_USD no longer governs anything — the role row does: `role set <role> --budget`"
    ]
    assert roles.retired_settings({}) == []


# --- the done-when, at the engine seam ---


def test_the_seeded_row_is_what_a_burst_gets(engine):
    stores, spawn = engine
    spec = spawn()
    assert (spec.model, spec.max_budget_usd, spec.max_turns) == ("claude-sonnet-5", 2.0, 100)
    assert spec.effort is None  # seeded unset: the CLI's default, as before the column
    assert spec.prompt == "fix the bug" and spec.repo_read_only is False
    reviewer = spawn(role="reviewer")
    assert reviewer.repo_read_only is True


def test_editing_an_open_field_changes_the_next_burst(engine):
    stores, spawn = engine
    stores.ledger.update_role(
        "dev",
        {"model": "claude-opus-5", "max_budget_usd": 0.75, "max_turns": 12, "prompt_preamble": "Be terse."},
        actor="op",
        reason="cheaper dev rounds",
    )
    spec = spawn()
    assert (spec.model, spec.max_budget_usd, spec.max_turns) == ("claude-opus-5", 0.75, 12)
    assert spec.prompt == "Be terse.\n\nfix the bug"
    # the row is read at every spawn — a second edit governs the burst after it
    stores.ledger.update_role("dev", {"mount_mode": "ro", "prompt_preamble": ""}, actor="op")
    spec = spawn()
    assert spec.repo_read_only is True and spec.prompt == "fix the bug"
    # every edit left its trail
    assert [a.action for a in stores.ledger.audit_trail("dev")] == ["role.set", "role.set"]
    assert stores.ledger.audit_trail("dev")[0].reason == "cheaper dev rounds"


def options_for(spec):
    """The SDK options the box would build from what `burst_env` hands it —
    the whole path an effort takes from the row to the engine."""
    return burst._to_options(BurstEnv.from_environ(burst_env(spec)))


def test_role_set_effort_reaches_the_next_bursts_options(engine):
    """#119's done-when: `role set dev --effort xhigh` makes the next dev burst's
    options carry it; `--effort default` (NULL) takes it out again."""
    stores, spawn = engine
    assert options_for(spawn()).effort is None
    assert agentenv.EFFORT not in burst_env(spawn())

    stores.ledger.update_role("dev", {"effort": "xhigh"}, actor="op", reason="think harder")
    spec = spawn()
    assert burst_env(spec)[agentenv.EFFORT] == "xhigh"
    assert options_for(spec).effort == "xhigh"
    assert options_for(spawn(role="reviewer")).effort is None  # the dev's row, not every row's

    stores.ledger.update_role("dev", {"effort": None}, actor="op")
    assert options_for(spawn()).effort is None
    assert [a.action for a in stores.ledger.audit_trail("dev")] == ["role.set", "role.set"]


def test_explicit_burst_arguments_win_for_that_burst_only(engine):
    # the hand-run `burst` verb: a flag is this one burst's, the row stays
    stores, spawn = engine
    spec = spawn(model="claude-haiku-4-5", max_budget_usd=0.1, max_turns=3)
    assert (spec.model, spec.max_budget_usd, spec.max_turns) == ("claude-haiku-4-5", 0.1, 3)
    assert stores.ledger.get_role("dev").model == "claude-sonnet-5"
    assert spawn().model == "claude-sonnet-5"


def test_a_widened_reviewer_row_is_still_read_only(engine):
    # the DB lets the open field move (test_schema_walls pins that); the code
    # floor is what the burst gets
    stores, spawn = engine
    stores.ledger.update_role("reviewer", {"mount_mode": "rw"}, actor="op")
    assert spawn(role="reviewer").repo_read_only is True


def test_editing_a_wall_field_is_refused_by_the_store(engine):
    stores, spawn = engine
    for wall in ({"tools": ["Bash"]}, {"identity": "automation"}, {"sink": "event"}, {"kind": "custom"}):
        with pytest.raises(RoleError, match="frozen"):
            stores.ledger.update_role("dev", wall, actor="op")
    assert stores.ledger.audit_trail("dev") == []  # a refusal leaves no trail
    assert stores.ledger.get_role("dev").tools == ["*"]


def test_the_sqlite_trigger_stands_behind_the_store_check(tmp_path):
    """A bypass of check_settable meets the schema: the SQLite twin of
    roles_pipeline_guard refuses the wall fields and deletion as an
    IntegrityError — the class a constraint gives, on both backends."""
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    conn = ledger._conn
    for statement in (
        "UPDATE roles SET identity = 'automation' WHERE role = 'dev'",
        "UPDATE roles SET tools = '[\"Bash\"]' WHERE role = 'dev'",
        "UPDATE roles SET kind = 'custom' WHERE role = 'dev'",
        "UPDATE roles SET role = 'dev2' WHERE role = 'dev'",
        "DELETE FROM roles WHERE role = 'reviewer'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="seeded pipeline role"):
            conn.execute(statement)
    # the open fields stay open at the SQL level too
    conn.execute("UPDATE roles SET model = 'claude-opus-5', mount_mode = 'rw' WHERE role = 'reviewer'")
    assert ledger.get_role("reviewer").model == "claude-opus-5"
    # a custom row is an ordinary row: the trigger fires on OLD.kind = 'pipeline' alone
    conn.execute(
        "INSERT INTO roles (role, kind, prompt_preamble, tools, model, max_budget_usd, max_turns, "
        "mount_mode, identity, sink, created_at, updated_at) VALUES "
        "('lens', 'custom', '', '[\"Read\"]', 'm', 1.0, 5, 'ro', 'none', 'issue_comment', 't', 't')"
    )
    conn.execute("UPDATE roles SET identity = 'automation' WHERE role = 'lens'")
    conn.execute("DELETE FROM roles WHERE role = 'lens'")


def test_a_narrowed_row_cannot_run_until_the_container_narrows(tmp_path):
    """Tool narrowing reaches the container with custom-role execution (T4).
    Until then a row that narrows is refused at spawn, loudly — running it wide
    would be a silent widening of a declaration the operator believes holds."""
    stores = PlatformStores.open(tmp_path / "data")
    stores.ledger._conn.execute(
        "INSERT INTO roles (role, kind, prompt_preamble, tools, model, max_budget_usd, max_turns, "
        "mount_mode, identity, sink, created_at, updated_at) VALUES "
        "('lens', 'custom', '', '[\"Read\", \"Grep\"]', 'claude-sonnet-5', 1.0, 5, 'ro', 'none', "
        "'issue_comment', 't', 't')"
    )
    task = TaskRecord(task_id="owner/product#1", repository="owner/product", issue_number=1)
    with pytest.raises(RoleError, match=r"narrows tools .*\(T4\)"):
        execute_burst(stores, task, tmp_path, "look", role="lens", runner=RecordingRunner())
    with pytest.raises(RoleError, match="no row"):
        execute_burst(stores, task, tmp_path, "look", role="ghost", runner=RecordingRunner())


def test_reopening_a_file_keeps_operator_edits(tmp_path):
    # the seed is INSERT OR IGNORE: a second open never resets a `role set`
    path = tmp_path / "ledger.sqlite3"
    first = SqliteLedger(path)
    first.update_role("tester", {"max_turns": 7}, actor="op")
    first.close()
    assert SqliteLedger(path).get_role("tester").max_turns == 7
