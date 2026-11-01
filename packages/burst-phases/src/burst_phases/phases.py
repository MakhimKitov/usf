"""What init does, and what the harvester does. Two functions, no framework.

Both are told everything through their environment, because that is what a pod
hands a container and what `docker run -e` hands one. Both fail loudly: a phase
that half-worked would leave the agent resuming a session that is not the one
the ledger names, or the control plane reading a result that was never written.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from session_volume import (
    BURST_RESULT_FILENAME,
    VIOLATIONS_FILENAME,
    sha256_of,
    tar_without_credentials,
    untar,
)

# The environment both phases read. Names, not values: a URL here is a
# capability for one object and expires with the burst.
VOLUME_DIR = "BURST_VOLUME_DIR"  # the shared volume, mounted at the same path in every container
SESSION_GET_URL = "BURST_SESSION_GET_URL"  # presigned GET for the parent snapshot
SESSION_SHA256 = "BURST_SESSION_SHA256"  # what the ledger recorded for it
SESSION_PUT_URL = "BURST_SESSION_PUT_URL"  # presigned PUT for this burst's snapshot
ARTIFACTS_PUT_URL = "BURST_ARTIFACTS_PUT_URL"  # presigned PUT for the small stuff
SESSION_KEY = "BURST_SESSION_KEY"  # the key the PUT writes to, for the record the control plane reads
CLONE_DIR = "BURST_CLONE_DIR"  # where the product's clone goes
CLONE_URL = "BURST_CLONE_URL"  # https://github.com/owner/name.git
CLONE_REF = "BURST_CLONE_REF"  # branch or sha; empty means the default branch
CLONE_TOKEN = "BURST_CLONE_TOKEN"  # contents:read, one repository — init's alone

HTTP_TIMEOUT_S = 300.0  # a snapshot is megabytes; a stuck transfer must not outlive the rail

# A ref is a branch or a commit, and git wants them checked out differently.
_IS_COMMIT = re.compile(r"[0-9a-f]{40}")


class PhaseError(RuntimeError):
    """A phase could not do its one job. The message says which one and why —
    never with a URL in it, because a presigned URL carries its own signature."""


@dataclass(frozen=True, slots=True)
class Artifacts:
    """What the harvester ships: the two files the agent wrote, and where the
    snapshot went. The control plane reads exactly this object and folds it into
    the ledger — it never sees the volume, because the volume dies with the pod."""

    result: dict | None
    violations: list[dict]
    session_key: str | None
    session_sha256: str | None

    def as_json(self) -> bytes:
        return json.dumps(
            {
                "result": self.result,
                "violations": self.violations,
                "session_key": self.session_key,
                "session_sha256": self.session_sha256,
            }
        ).encode()

    @classmethod
    def from_json(cls, blob: bytes) -> Artifacts:
        data = json.loads(blob)
        return cls(
            result=data.get("result"),
            violations=list(data.get("violations") or []),
            session_key=data.get("session_key"),
            session_sha256=data.get("session_sha256"),
        )


def _fetch(url: str, destination: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as response:  # noqa: S310 — a presigned https URL
            destination.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        raise PhaseError(f"the snapshot could not be read: HTTP {exc.code}") from None
    except OSError as exc:
        raise PhaseError(f"the snapshot could not be read: {exc.__class__.__name__}") from None


def _upload(url: str, body: bytes, what: str) -> None:
    request = urllib.request.Request(url, data=body, method="PUT")
    request.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:  # noqa: S310
            if response.status >= 300:
                raise PhaseError(f"{what} was refused: HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise PhaseError(f"{what} was refused: HTTP {exc.code}") from None
    except OSError as exc:
        raise PhaseError(f"{what} could not be written: {exc.__class__.__name__}") from None


def initialize(environ: dict[str, str]) -> Path:
    """Fill the burst's volume: the parent snapshot, then the clone.

    The snapshot's checksum is checked here, against the number the ledger
    recorded and passed in — the same refusal the control plane makes when it
    materializes on its own filesystem. A burst must never be handed bytes
    nobody recorded, wherever the bytes came from.
    """
    volume = Path(environ.get(VOLUME_DIR, "")).expanduser()
    if not str(volume) or str(volume) == ".":
        raise PhaseError(f"{VOLUME_DIR} names nothing")
    volume.mkdir(parents=True, exist_ok=True)

    url = environ.get(SESSION_GET_URL, "").strip()
    if url:
        expected = environ.get(SESSION_SHA256, "").strip()
        if not expected:
            raise PhaseError(f"{SESSION_GET_URL} without {SESSION_SHA256}: nothing to check the bytes against")
        with tempfile.TemporaryDirectory() as scratch:
            blob = Path(scratch) / "session.tar.gz"
            _fetch(url, blob)
            actual = sha256_of(blob)
            if actual != expected:
                raise PhaseError(
                    f"the snapshot does not match what the ledger recorded: "
                    f"ledger says {expected}, bytes say {actual}"
                )
            untar(blob, volume)

    clone_url = environ.get(CLONE_URL, "").strip()
    if clone_url:
        _clone(environ, clone_url)
    return volume


def _clone(environ: dict[str, str], clone_url: str) -> None:
    """The product's clone, with a token that can only read one repository.

    The token is written nowhere: it rides a single `git clone` invocation as
    an HTTP header and dies with the process — never in the URL, which `git
    clone` writes into the clone's `.git/config` as the origin. There it would
    outlive init: every role's agent would hold it, and a dev's push would ride
    it, read-only, past the credential helper (a 403 on the first JobsRunner
    burst that tried). The control plane's own clone does the same
    (`control_plane/clone.py`). `git` is told not to ask for anything
    interactively, so a token that does not work fails the phase instead of
    hanging a pod until its deadline.
    """
    destination = Path(environ.get(CLONE_DIR, "")).expanduser()
    if not str(destination) or str(destination) == ".":
        raise PhaseError(f"{CLONE_URL} without {CLONE_DIR}: nowhere to put the clone")
    token = environ.get(CLONE_TOKEN, "").strip()
    auth: list[str] = []
    if token and clone_url.startswith("https://"):
        host = urllib.parse.urlsplit(clone_url).netloc
        auth = ["-c", f"http.https://{host}/.extraheader=Authorization: Basic {_basic(token)}"]
    ref = environ.get(CLONE_REF, "").strip()
    # The same two moves the control plane's own clone makes, in the same order:
    # clone, then detach onto a commit if that is what the ref is. Not shallow —
    # the tree a burst gets here has to be the tree it gets on the host path, and
    # a dev burst reads history and pushes a branch out of it. What that costs
    # per burst is the clone cache's problem (#105), not correctness's.
    _run_git([*auth, "clone", clone_url, str(destination)], token, "the clone")
    if ref:
        if _IS_COMMIT.fullmatch(ref):
            _run_git(["-C", str(destination), "checkout", "--detach", ref], token, "the checkout")
        else:
            _run_git(["-C", str(destination), "checkout", "-B", ref, f"origin/{ref}"], token, "the checkout")


def _basic(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _run_git(args: list[str], token: str, what: str) -> None:
    """One git invocation, with nothing of it in the message if it fails.

    `git` is told not to ask for anything interactively, so a token that does not
    work fails the phase instead of hanging a pod until its deadline.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"},
        check=False,
    )
    if result.returncode != 0:
        # the token is in the command line, so the command is never echoed back
        detail = result.stderr or ""
        if token:
            detail = detail.replace(token, "***").replace(_basic(token), "***")
        last = detail.strip().splitlines()[-1:] or [""]
        raise PhaseError(f"{what} failed (exit {result.returncode}): {last[0]}"[:400])


def harvest(environ: dict[str, str]) -> Artifacts:
    """Ship what the burst left: the snapshot to its key, the rest to the record.

    The result file and the violations feed go into one small JSON object the
    control plane reads — the volume itself is gone the moment the pod is, so
    anything the ledger needs has to leave through a URL. The snapshot's
    checksum is computed here and travels in that object: the archive's own
    metadata copy is a convenience, and the ledger is the authority.
    """
    volume = Path(environ.get(VOLUME_DIR, "")).expanduser()
    if not volume.is_dir():
        raise PhaseError(f"{VOLUME_DIR} is not a directory the harvester can read")

    result: dict | None = None
    result_path = volume / BURST_RESULT_FILENAME
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))

    violations: list[dict] = []
    violations_path = volume / VIOLATIONS_FILENAME
    if violations_path.is_file():
        violations = [
            json.loads(line)
            for line in violations_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    session_key = None
    session_sha256 = None
    session_url = environ.get(SESSION_PUT_URL, "").strip()
    if session_url:
        with tempfile.TemporaryDirectory() as scratch:
            blob = Path(scratch) / "session.tar.gz"
            # the credential exclusion is the packer's, not the caller's: a
            # harvester cannot ship a token even by mistake
            session_sha256 = tar_without_credentials(volume, blob)
            _upload(session_url, blob.read_bytes(), "the snapshot")
        session_key = environ.get(SESSION_KEY, "").strip() or None

    artifacts = Artifacts(
        result=result, violations=violations, session_key=session_key, session_sha256=session_sha256
    )
    artifacts_url = environ.get(ARTIFACTS_PUT_URL, "").strip()
    if not artifacts_url:
        raise PhaseError(f"{ARTIFACTS_PUT_URL} names nothing — there is nowhere to report to")
    _upload(artifacts_url, artifacts.as_json(), "the artifacts record")
    return artifacts


def _run(phase, environ: dict[str, str], what: str) -> int:
    try:
        phase(environ)
    except PhaseError as exc:
        print(f"{what}: {exc}", file=sys.stderr)
        return 1
    return 0
