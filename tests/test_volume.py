"""Volume choreography: materialize from the parent node, collect into a new one.

Since A5 this is a tree walk. `materialize_volume` descends from the leaf the
ledger names; `collect_volume` writes a new leaf under this burst's own key; the
edge between them is `resumed_from_burst_id`, recorded at spawn. The tests below
are about that shape — the archive's own guarantees (write-once, checksums,
credential exclusion) belong to `test_archive_contract.py`.

Since G1 the second half is two functions with a seam between them, and the seam
is where a Job pod will be cut: `collect_volume` moves *bytes* and runs wherever
the burst ran, `fold_artifacts` turns them into ledger rows and runs only in the
control plane. `harvest` below is the two of them in the order `execute_burst`
runs them; `test_collect_moves_bytes_and_folding_writes_rows` is the line itself.
"""

import json

import pytest
from control_plane.models import BurstRecord, BurstStatus
from control_plane.store import ArchiveError, LocalSessionArchive, SqliteLedger, session_key
from control_plane.volume import Snapshot, collect_volume, fold_artifacts, materialize_volume
from dev_agent.env import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME


@pytest.fixture
def stores(tmp_path):
    return LocalSessionArchive(tmp_path / "archive"), SqliteLedger(tmp_path / "ledger.sqlite3")


def _burst(burst_id, task_id="42", **kwargs):
    return BurstRecord(burst_id=burst_id, task_id=task_id, **kwargs)


def test_what_a_burst_proposes_to_remember_becomes_a_row_with_its_provenance(stores, tmp_path):
    """H4: the write path is the harvest's (TO-BE §9). The scope is derived from
    the burst's own task, so a burst cannot write into another product's memory
    even by asking to, and the status is the platform's, so nothing promotes
    itself into recall."""
    archive, ledger = stores
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    (volume / BURST_RESULT_FILENAME).write_text(
        json.dumps({
            "session_id": "s1",
            "memories": [
                {"claim": "the suite needs `uv sync` first", "evidence": "ModuleNotFoundError until it ran"},
                {"claim": "no evidence given", "evidence": "   "},
                {"claim": "", "evidence": "nothing claimed"},
            ],
        }),
        encoding="utf-8",
    )
    burst = _burst("b1", task_id="owner/product#42")
    harvest(volume, burst, archive, ledger)

    (entry,) = ledger.memory_entries()
    assert entry.claim == "the suite needs `uv sync` first"
    assert entry.scope == "product" and entry.repository == "owner/product"
    assert entry.proposed_by_burst == "b1" and entry.status == "proposed"


def test_a_burst_that_learned_nothing_proposes_nothing(stores, tmp_path):
    archive, ledger = stores
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    (volume / BURST_RESULT_FILENAME).write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    harvest(volume, _burst("b1", task_id="owner/product#42"), archive, ledger)
    assert ledger.memory_entries() == []


def test_a_ledger_that_refuses_a_proposal_does_not_fail_the_fold(stores, tmp_path):
    """A proposal is a by-product of the work, so a store error on it — not
    only a malformed proposal — is a log line (0023): the burst's own row and
    its violations still land, and the control plane folding it lives on."""
    archive, ledger = stores

    def refuse(entry):
        raise RuntimeError("permission denied for table memory_entries")

    ledger.propose_memory = refuse
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    (volume / BURST_RESULT_FILENAME).write_text(
        json.dumps({"session_id": "s1", "memories": [{"claim": "c", "evidence": "e"}]}), encoding="utf-8"
    )
    (volume / VIOLATIONS_FILENAME).write_text(
        json.dumps({"tool": "Bash", "reason": "WALL", "input": "cat /etc/hostname"}) + "\n", encoding="utf-8"
    )
    burst = _burst("b1", task_id="owner/product#42")
    harvest(volume, burst, archive, ledger)

    assert burst.session_id == "s1"
    assert [v.tool for v in ledger.violations("owner/product#42")] == ["Bash"]


def harvest(volume, burst, archive, ledger, archive_session=True):
    """The two phases as `execute_burst` runs them: bytes out, then rows in."""
    artifacts = collect_volume(
        volume, archive, session_key(burst.task_id, burst.burst_id) if archive_session else None
    )
    return fold_artifacts(artifacts, burst, ledger)


def _wrote(volume, text):
    (volume / "projects").mkdir(exist_ok=True)
    (volume / "projects" / "sess.jsonl").write_text(text, encoding="utf-8")


def test_a_first_burst_starts_empty(stores, tmp_path):
    """No parent, nothing to materialize — a task's first burst and an amnesiac
    burst are the same case, expressed the same way."""
    archive, _ = stores
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    assert volume.is_dir() and not any(volume.iterdir())


def test_harvest_writes_a_snapshot_per_burst_and_resume_materializes_the_leaf(stores, tmp_path):
    """A5's *done when*, end to end: two rounds, two snapshots, and the second
    volume holding what the first burst left."""
    archive, ledger = stores

    first = _burst("b1")
    volume1 = materialize_volume(tmp_path / "volumes" / "b1", archive)
    _wrote(volume1, '{"turn":1}\n')
    (volume1 / BURST_RESULT_FILENAME).write_text(
        json.dumps({"session_id": "sess-1", "model": "claude-sonnet-5-20260203", "num_turns": 7}),
        encoding="utf-8",
    )
    ledger.record_burst(first)
    outcome = harvest(volume1, first, archive, ledger)
    ledger.finish_burst(_finished(first))

    assert outcome.session_key == session_key("42", "b1") == "sessions/42/b1/session.tar.gz"
    assert len(outcome.session_checksum) == 64
    assert first.session_key == outcome.session_key  # and it went on the row

    # the ledger, not the task row, is what the next burst asks
    leaf = ledger.latest_archived_burst("42")
    assert leaf.burst_id == "b1"

    second = _burst("b2", resumed_from_burst_id=leaf.burst_id)
    volume2 = materialize_volume(tmp_path / "volumes" / "b2", archive, Snapshot.of(leaf))
    assert (volume2 / "projects" / "sess.jsonl").read_text() == '{"turn":1}\n'

    _wrote(volume2, '{"turn":2}\n')
    ledger.record_burst(second)
    harvest(volume2, second, archive, ledger)
    ledger.finish_burst(_finished(second))

    # two snapshots, both still there: the first was not overwritten
    assert ledger.latest_archived_burst("42").burst_id == "b2"
    restored = tmp_path / "restored"
    archive.get(first.session_key, restored, first.session_checksum)
    assert (restored / "projects" / "sess.jsonl").read_text() == '{"turn":1}\n'


def test_the_leaf_is_the_newest_archived_burst_not_the_newest_burst(stores, tmp_path):
    """The leaf stays the last *dev* burst, two ways (B5): a reviewer archives
    nothing, so it is not a node — and a tester archives *evidence*, snapshot and
    key and all, which the role filter keeps out of the tree just the same."""
    archive, ledger = stores

    dev = _burst("b1", started_at="2026-08-14T09:00:00+00:00")
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    _wrote(volume, '{"dev":1}\n')
    ledger.record_burst(dev)
    harvest(volume, dev, archive, ledger)
    ledger.finish_burst(_finished(dev))

    reviewer = _burst("b2", role="reviewer", kind="assess", started_at="2026-08-14T10:00:00+00:00")
    reviewer_volume = materialize_volume(tmp_path / "volumes" / "b2", archive)  # no parent: amnesiac
    _wrote(reviewer_volume, '{"reviewer":1}\n')
    ledger.record_burst(reviewer)
    outcome = harvest(reviewer_volume, reviewer, archive, ledger, archive_session=False)
    ledger.finish_burst(_finished(reviewer))

    assert outcome.session_key is None
    assert reviewer.session_key is None
    assert ledger.latest_archived_burst("42").burst_id == "b1"

    tester = _burst("b3", role="tester", kind="test", started_at="2026-08-14T11:00:00+00:00")
    tester_volume = materialize_volume(tmp_path / "volumes" / "b3", archive)  # never seeded
    _wrote(tester_volume, '{"tester":1}\n')
    ledger.record_burst(tester)
    harvest(tester_volume, tester, archive, ledger)  # B5: archive_session=True
    ledger.finish_burst(_finished(tester))

    assert tester.session_key == session_key("42", "b3")  # evidence, under its own key
    assert ledger.latest_archived_burst("42").burst_id == "b1"  # and still not the leaf

    # and the next dev round resumes the dev — never the reviewer, never the tester
    next_volume = materialize_volume(tmp_path / "volumes" / "b4", archive, Snapshot.of(ledger.latest_archived_burst("42"))
    )
    assert (next_volume / "projects" / "sess.jsonl").read_text() == '{"dev":1}\n'


def test_an_amnesiac_burst_cannot_read_the_devs_transcripts(stores, tmp_path):
    """`parent=None` is the whole mechanism: there is no way to ask for a volume
    that resumes nothing but still holds the session (REVIEWERS.md)."""
    archive, ledger = stores
    dev = _burst("b1")
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    _wrote(volume, '{"dev":1}\n')
    ledger.record_burst(dev)
    harvest(volume, dev, archive, ledger)

    reviewer_volume = materialize_volume(tmp_path / "volumes" / "b2", archive)
    assert not any(reviewer_volume.iterdir())


def test_a_retry_re_materializes_the_same_parent(stores, tmp_path):
    """Two children of one node, which is what makes the history a tree rather
    than a chain — and neither write touches the parent's object."""
    archive, ledger = stores
    parent = _burst("b1")
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    _wrote(volume, '{"turn":1}\n')
    ledger.record_burst(parent)
    harvest(volume, parent, archive, ledger)
    ledger.finish_burst(_finished(parent))

    for child_id in ("b2", "b3"):
        child_volume = materialize_volume(tmp_path / "volumes" / child_id, archive, Snapshot.of(parent))
        assert (child_volume / "projects" / "sess.jsonl").read_text() == '{"turn":1}\n'
        child = _burst(child_id, resumed_from_burst_id=parent.burst_id)
        ledger.record_burst(child)
        harvest(child_volume, child, archive, ledger)
        ledger.finish_burst(_finished(child))

    children = [
        b.burst_id
        for b in (ledger.get_burst("b2"), ledger.get_burst("b3"))
        if b.resumed_from_burst_id == "b1"
    ]
    assert children == ["b2", "b3"]
    assert ledger.get_burst("b1").session_checksum == parent.session_checksum  # untouched


def test_a_snapshot_the_ledger_disagrees_with_never_reaches_a_volume(stores, tmp_path):
    """The checksum comes from the ledger row, so tampering in the archive stops
    the next burst instead of seeding it with something else's bytes."""
    archive, ledger = stores
    parent = _burst("b1")
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    _wrote(volume, '{"turn":1}\n')
    ledger.record_burst(parent)
    harvest(volume, parent, archive, ledger)

    blob = tmp_path / "archive" / parent.session_key
    blob.write_bytes(blob.read_bytes() + b"tampered")

    with pytest.raises(ArchiveError, match="checksum mismatch"):
        materialize_volume(tmp_path / "volumes" / "b2", archive, Snapshot.of(parent))


def test_harvest_folds_the_result_and_the_violations_in(stores, tmp_path):
    archive, ledger = stores
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    (volume / BURST_RESULT_FILENAME).write_text(
        json.dumps(
            {
                "session_id": "sess-1",
                "model": "claude-sonnet-5-20260203",
                "total_cost_usd": 0.42,
                "num_turns": 7,
                "is_error": False,
            }
        ),
        encoding="utf-8",
    )
    (volume / VIOLATIONS_FILENAME).write_text(
        json.dumps({"tool": "Read", "reason": "WALL: private/", "input": "{}"}) + "\n",
        encoding="utf-8",
    )

    burst = _burst("b1", model="claude-sonnet-5")
    outcome = harvest(volume, burst, archive, ledger)

    assert burst.session_id == "sess-1"
    assert burst.model == "claude-sonnet-5-20260203"  # resolved beats requested
    assert burst.cost_usd == 0.42
    assert burst.num_turns == 7
    assert outcome.violation_count == 1
    assert len(ledger.violations("42")) == 1


def test_harvest_marks_error_bursts_failed(stores, tmp_path):
    archive, ledger = stores
    volume = materialize_volume(tmp_path / "volumes" / "b1", archive)
    (volume / BURST_RESULT_FILENAME).write_text(json.dumps({"is_error": True}), encoding="utf-8")

    burst = _burst("b1")
    harvest(volume, burst, archive, ledger)
    assert burst.status == BurstStatus.FAILED


def _finished(burst: BurstRecord) -> BurstRecord:
    """What the burst looks like on the way into finish_burst."""
    burst.status = BurstStatus.SUCCEEDED
    burst.finished_at = "2026-08-14T12:00:00+00:00"
    burst.exit_code = 0
    return burst
