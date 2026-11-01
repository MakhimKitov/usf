"""Session-volume choreography: materialize before boot, collect after exit.

The burst container mounts one control-plane-owned directory as its
``CLAUDE_CONFIG_DIR``. Before boot the parent burst's snapshot is materialized
into it (so ``resume`` finds the transcript locally); after the burst the result
file and the violations feed are read out of it and the whole volume is archived
under this burst's own key. The agent needs no storage credentials at any point.

Since A5 the two halves are a tree walk rather than a blob swap: materialize
descends from the leaf the ledger names, collect writes a new leaf, and the edge
between them is ``resumed_from_burst_id``. Retry re-materializes the same parent;
nothing in the archive is ever rewritten (STORES.md, "The session archive").

Three functions, and the line between them is the seam G1 (#42) drew. The first
two are *phases of the runner*: they move bytes, and where they run is the
runner's business — this machine's filesystem for `DockerRunner`, an init
container and a harvester for a Job pod (G2/G3). The third is the control
plane's: folding what came back into the ledger needs credentials a burst's
substrate must never hold.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dev_agent.env import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME

from control_plane.models import BurstRecord, BurstStatus, MemoryEntry, Violation
import structlog

from control_plane.store import ArchiveError, Ledger, SessionArchive


log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A node of the session tree, as the phase that materializes it needs it:
    the archive key and the checksum the *ledger* recorded for it (A5). Never
    read from the archive — the archive is asked to prove the bytes match."""

    key: str
    checksum: str

    @classmethod
    def of(cls, burst: BurstRecord | None) -> Snapshot | None:
        if burst is None or not burst.session_key:
            return None
        return cls(key=burst.session_key, checksum=burst.session_checksum or "")


@dataclass(slots=True)
class BurstArtifacts:
    """What a finished burst left behind, wherever it ran — bytes and nothing
    else. The runner's collect phase produces it; `fold_artifacts` is the only
    thing that turns it into ledger rows."""

    result: dict | None = None
    violations: list[dict] = field(default_factory=list)
    session_key: str | None = None
    session_checksum: str | None = None

    @property
    def memories(self) -> list[dict]:
        """What the burst proposed to remember — part of the result contract
        (TO-BE §9), so it travels wherever the result does, including out of a
        pod through the harvester's record."""
        return list((self.result or {}).get("memories") or [])


@dataclass(slots=True)
class HarvestOutcome:
    result: dict | None
    violation_count: int
    session_key: str | None
    session_checksum: str | None


def materialize_volume(
    volume: Path, archive: SessionArchive | None, parent: Snapshot | None = None
) -> Path:
    """A fresh volume for this burst, materialized from its parent node.

    ``parent`` is the leaf of the task's session tree — the snapshot this burst
    continues from (A5). ``None`` means start empty, which covers both honest
    cases with one parameter: a task's first burst has no parent, and a
    deliberately-amnesiac burst is given none. The reviewer is the second kind,
    and the property it needs is unchanged — the dev's transcripts must not be
    *readable* from inside, not merely unresumed — but it is impossible to ask
    for a parent and not materialize it.

    The checksum travels with the key in `Snapshot`, and it came from the
    ledger's row, never from the archive: the archive is asked to prove the bytes
    match what the ledger recorded, and a mismatch raises before anything
    reaches the volume.
    """
    if volume.exists():
        shutil.rmtree(volume)
    volume.mkdir(parents=True)
    if parent is not None and parent.key:
        if archive is None:
            raise ArchiveError(f"no archive to materialize {parent.key} from")
        archive.get(parent.key, volume, parent.checksum)
    return volume


def collect_volume(
    volume: Path, archive: SessionArchive | None, archive_as: str | None = None
) -> BurstArtifacts:
    """Read what a finished burst left in its volume, and archive the volume.

    Bytes only — no ledger, no burst record. That is the G1 line: this half runs
    wherever the burst ran (here for `DockerRunner`, a harvester container for a
    Job pod), and nothing that runs beside a burst may hold ledger credentials.

    ``archive_as`` is the key to write the snapshot under, or None to write none
    — reviewer bursts only (B5): they read and never execute, so there is
    nothing to prove. A *tester's* volume is archived, not as a node but as
    evidence: the transcript records what the sandbox did (every tool call with
    its verbatim result), which is stronger than anything the report says.
    Archiving it cannot make it resumable — `latest_archived_burst` is
    role-filtered, so leafhood stays the dev's.
    """
    artifacts = BurstArtifacts()
    result_path = volume / BURST_RESULT_FILENAME
    if result_path.is_file():
        artifacts.result = json.loads(result_path.read_text(encoding="utf-8"))

    violations_path = volume / VIOLATIONS_FILENAME
    if violations_path.is_file():
        artifacts.violations = [
            json.loads(line)
            for line in violations_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    if archive_as is not None:
        if archive is None:
            raise ArchiveError(f"no archive to write {archive_as} to")
        # The snapshot is this burst's own, keyed by it, and written once.
        artifacts.session_key = archive_as
        artifacts.session_checksum = archive.put(archive_as, volume)
    return artifacts


def fold_artifacts(
    artifacts: BurstArtifacts, burst: BurstRecord, ledger: Ledger
) -> HarvestOutcome:
    """The control plane's half: what the burst left, written into the stores.

    Everything here needs credentials or authority a burst's substrate must not
    have — the violations feed becomes rows, and the burst record learns what it
    cost and what it left. Both checksum halves go on the record, which
    `finish_burst` persists: the ledger is where a checksum is authoritative.
    """
    if artifacts.result is not None:
        result = artifacts.result
        burst.session_id = result.get("session_id") or burst.session_id
        burst.model = result.get("model") or burst.model  # resolved beats requested
        burst.cost_usd = result.get("total_cost_usd")
        burst.input_tokens = result.get("input_tokens")
        burst.output_tokens = result.get("output_tokens")
        burst.num_turns = result.get("num_turns")
        if result.get("is_error"):
            burst.status = BurstStatus.FAILED

    # What the burst proposes to remember (TO-BE §9, H4). The scope is *derived*
    # — a product entry names the burst's own product, taken from its task id —
    # so a burst cannot write into another product's memory even by asking to;
    # and `propose_memory` forces the status, so nothing promotes itself into
    # recall. A malformed proposal is dropped with a line, never a failed burst:
    # the deliverable was the work, and this is a by-product of having done it.
    repository = burst.task_id.rsplit("#", 1)[0] if "#" in burst.task_id else None
    for proposal in artifacts.memories:
        try:
            stored = ledger.propose_memory(
                MemoryEntry(
                    scope="product" if repository else "platform",
                    claim=str(proposal.get("claim") or ""),
                    evidence=str(proposal.get("evidence") or ""),
                    repository=repository,
                    proposed_by_burst=burst.burst_id,
                )
            )
            log.info("memory proposed", entry=stored.id, burst=burst.burst_id, scope=stored.scope)
        except Exception as exc:  # noqa: BLE001 — a by-product never fails the fold, a store error included (0023)
            log.warning("a memory proposal was refused", burst=burst.burst_id, reason=str(exc))

    for entry in artifacts.violations:
        ledger.record_violation(
            Violation(
                task_id=burst.task_id,
                burst_id=burst.burst_id,
                tool=entry.get("tool", ""),
                reason=entry.get("reason", ""),
                detail=entry.get("input", ""),
            )
        )

    if artifacts.session_key is not None:
        burst.session_key = artifacts.session_key
        burst.session_checksum = artifacts.session_checksum
    return HarvestOutcome(
        result=artifacts.result,
        violation_count=len(artifacts.violations),
        session_key=burst.session_key,
        session_checksum=burst.session_checksum,
    )
