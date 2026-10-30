"""What is specific to one backend, and nothing that isn't.

The behavior both backends of each store must share lives in the contract suites —
`test_ledger_contract.py` and `test_archive_contract.py` — each running one suite
against every implementation. What stays here is the single-node ledger's
bootstrap, which has no counterpart: Postgres gets its schema from numbered
migrations, and this one writes its own on open.
"""

import sqlite3

import pytest
from control_plane.models import BurstRecord, TaskRecord
from control_plane.store import LedgerShapeError, SqliteLedger

MVP_TASKS = (
    "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, repo TEXT NOT NULL, status TEXT NOT NULL, "
    "session_id TEXT, transcript_key TEXT, transcript_checksum TEXT, pr_number INTEGER, "
    "attempts INTEGER NOT NULL DEFAULT 0, review_rounds INTEGER NOT NULL DEFAULT 0, "
    "next_burst_kind TEXT, last_review_id INTEGER, last_burst_at TEXT, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
)

# A file in the shape A6 left behind: current task ids, no `span_id` yet.
PRE_B1_SCHEMA = """
CREATE TABLE tasks (task_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
    issue_number INTEGER NOT NULL, status TEXT NOT NULL, session_id TEXT,
    pr_number INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
    review_rounds INTEGER NOT NULL DEFAULT 0, next_burst_kind TEXT,
    last_review_id INTEGER, last_burst_at TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE bursts (burst_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
    role TEXT NOT NULL, status TEXT NOT NULL, trace_id TEXT NOT NULL DEFAULT '',
    resumed_from_burst_id TEXT, started_at TEXT NOT NULL, finished_at TEXT,
    exit_code INTEGER, model TEXT, cost_usd REAL, input_tokens INTEGER,
    output_tokens INTEGER, num_turns INTEGER, session_id TEXT, session_key TEXT,
    session_checksum TEXT);
"""


def test_a_fresh_ledger_is_ready_on_open(tmp_path):
    """No runner, no migration: the file is created in the current shape."""
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    ledger.upsert_task(TaskRecord(task_id="owner/product#7", repository="owner/product", issue_number=7))
    ledger.record_burst(BurstRecord(burst_id="b1", task_id="owner/product#7"))

    loaded = ledger.get_task("owner/product#7")
    assert (loaded.repository, loaded.issue_number) == ("owner/product", 7)
    assert ledger.get_burst("b1").session_key is None  # nothing archived yet


def test_a_column_added_after_the_file_was_made_gets_added_to_it(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` does nothing to a table that exists, so a file
    created before B1 has no `span_id` and the next insert would fail on it. The
    additive list is for exactly this: a column with a default, on a file whose task
    ids are already current — the case A6's refusal was never about."""
    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(PRE_B1_SCHEMA)
    conn.execute(
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, trace_id, started_at) "
        "VALUES ('b0', 'owner/product#7', 'work', 'dev', 'succeeded', 'ab', 'then')"
    )
    conn.commit()
    conn.close()

    ledger = SqliteLedger(db)
    ledger.upsert_task(TaskRecord(task_id="owner/product#7", repository="owner/product", issue_number=7))
    ledger.record_burst(BurstRecord(burst_id="b1", task_id="owner/product#7", span_id="c" * 16))

    assert ledger.get_burst("b1").span_id == "c" * 16
    # the burst that predates the column keeps its trace and reads back unspanned,
    # which is what `telemetry.link_to_burst` refuses to make a link out of
    assert (ledger.get_burst("b0").trace_id, ledger.get_burst("b0").span_id) == ("ab", "")
    # 0021 (H5): the old burst is a live node, and it can still be abandoned
    assert ledger.get_burst("b0").branch_summary is None
    assert ledger.abandon_burst("b0", "abandoned after the column arrived")
    assert ledger.latest_archived_burst("owner/product#7") is None


def test_a_roles_table_from_before_effort_opens_and_takes_one(tmp_path):
    """0022 (#119): a file made while the row had no `effort` gains the column on
    open. Every seeded row reads back unset — the CLI's default, which is what
    those bursts ran at — and an operator's earlier edit survives the repair."""
    db = tmp_path / "ledger.sqlite3"
    SqliteLedger(db).close()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE roles SET model = 'claude-opus-5' WHERE role = 'dev'")
    conn.execute("ALTER TABLE roles DROP COLUMN effort")  # back to the shape 0014 knew
    conn.commit()
    conn.close()

    ledger = SqliteLedger(db)
    assert {row.effort for row in ledger.list_roles()} == {None}
    assert ledger.get_role("dev").model == "claude-opus-5"
    ledger.update_role("dev", {"effort": "high"}, "op")
    assert ledger.get_role("dev").effort == "high"


def test_a_pre_a6_ledger_refuses_to_open_and_says_what_to_run(tmp_path):
    """A6 retires this ledger as an origin (STORES.md). Its rows are keyed by a
    bare issue number, which no longer identifies a task, and no `ALTER TABLE` can
    fix that — deriving an id needs a repository slug the file has never held. So
    the class refuses rather than opening something half-shaped, and names the
    one-shot that can move the rows."""
    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(MVP_TASKS)
    conn.execute(
        "INSERT INTO tasks (task_id, repo, status, created_at, updated_at) "
        "VALUES ('7', '/Users/x/clones/product', 'done', 'then', 'then')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerShapeError, match="control-plane backfill"):
        SqliteLedger(db)

    # and the file is untouched — the refusal is not a repair attempt
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT repo FROM tasks").fetchone()[0] == "/Users/x/clones/product"
    conn.close()
