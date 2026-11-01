"""The pod-side phases, and the capabilities they are given (G2, #43).

Two halves that never meet in one process: `burst_phases` runs beside a burst and
holds URLs; `control_plane.phases` mints those URLs and reads back what the
harvester shipped. The tests below drive the first half as a stranger would — a
mapping in, an exit code out — and the second half against an archive.

The whole path, minted URLs → the two entrypoints as processes → the control
plane folding what came back, is `test_phase_round_trip.py`'s: it needs an
object store, so it runs in the stores job.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from burst_phases.phases import (
    ARTIFACTS_PUT_URL,
    CLONE_DIR,
    CLONE_REF,
    CLONE_TOKEN,
    CLONE_URL,
    SESSION_GET_URL,
    SESSION_KEY,
    SESSION_PUT_URL,
    SESSION_SHA256,
    VOLUME_DIR,
    Artifacts,
    PhaseError,
    harvest,
    initialize,
)
from control_plane.phases import PHASE_URL_TTL_S, artifacts_key, mint_phase_env, read_artifacts
from control_plane.store import ArchiveError, LocalSessionArchive
from control_plane.volume import Snapshot
from session_volume import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME, tar_without_credentials

KEY = "sessions/owner/product#42/b1/session.tar.gz"
PARENT_KEY = "sessions/owner/product#42/b0/session.tar.gz"
RECORD_KEY = "sessions/owner/product#42/b1/artifacts.json"


class Served:
    """An object store the phases can actually talk to: one URL per object,
    served by a thread. What a presigned URL is, minus the signature."""

    def __init__(self, tmp_path):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from threading import Thread

        self.root = tmp_path / "served"
        self.root.mkdir()
        self.refuse = set()
        store = self

        class Handler(BaseHTTPRequestHandler):
            def _path(self):
                return store.root / self.path.strip("/")

            def do_GET(self):  # noqa: N802
                target = self._path()
                if self.path.strip("/") in store.refuse or not target.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):  # noqa: N802
                if self.path.strip("/") in store.refuse:
                    self.send_response(403)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                target = self._path()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.rfile.read(length))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{name}"

    def put(self, name: str, body: bytes) -> None:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    def read(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def served(tmp_path):
    store = Served(tmp_path)
    yield store
    store.stop()


def a_snapshot(tmp_path, text="the parent's turn\n"):
    """A packed session, and its checksum — what the ledger would have recorded."""
    source = tmp_path / "source"
    (source / "projects").mkdir(parents=True)
    (source / "projects" / "sess.jsonl").write_text(text, encoding="utf-8")
    blob = tmp_path / "snapshot.tar.gz"
    return blob, tar_without_credentials(source, blob)


# --- init: the volume a burst boots against ---


def test_init_makes_an_empty_volume_when_there_is_nothing_to_restore(tmp_path):
    volume = tmp_path / "volume"
    assert initialize({VOLUME_DIR: str(volume)}) == volume
    assert volume.is_dir() and not any(volume.iterdir())


def test_init_restores_the_snapshot_the_ledger_named(tmp_path, served):
    blob, checksum = a_snapshot(tmp_path)
    served.put("parent.tar.gz", blob.read_bytes())
    volume = tmp_path / "volume"

    initialize({
        VOLUME_DIR: str(volume),
        SESSION_GET_URL: served.url("parent.tar.gz"),
        SESSION_SHA256: checksum,
    })
    assert (volume / "projects" / "sess.jsonl").read_text(encoding="utf-8") == "the parent's turn\n"


def test_init_refuses_bytes_the_ledger_did_not_record(tmp_path, served):
    """The same refusal the control plane makes on its own filesystem: a burst
    must never be handed a session nobody recorded, wherever it came from."""
    blob, _ = a_snapshot(tmp_path)
    served.put("parent.tar.gz", blob.read_bytes())
    volume = tmp_path / "volume"

    with pytest.raises(PhaseError, match="does not match what the ledger recorded"):
        initialize({
            VOLUME_DIR: str(volume),
            SESSION_GET_URL: served.url("parent.tar.gz"),
            SESSION_SHA256: "0" * 64,
        })
    assert not (volume / "projects").exists()  # nothing of it reached the volume

    with pytest.raises(PhaseError, match="nothing to check the bytes against"):
        initialize({VOLUME_DIR: str(volume), SESSION_GET_URL: served.url("parent.tar.gz")})


def test_init_says_so_when_the_object_is_gone(tmp_path, served):
    served.refuse.add("parent.tar.gz")
    with pytest.raises(PhaseError, match="HTTP 404"):
        initialize({
            VOLUME_DIR: str(tmp_path / "volume"),
            SESSION_GET_URL: served.url("parent.tar.gz"),
            SESSION_SHA256: "a" * 64,
        })


def _origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *args: subprocess.run(args, cwd=origin, capture_output=True, check=True)  # noqa: E731
    run("git", "init", "--initial-branch=main", ".")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (origin / "README.md").write_text("product\n", encoding="utf-8")
    run("git", "add", "README.md")
    run("git", "commit", "-m", "first")
    return origin


def test_init_clones_the_product_with_the_token_it_alone_holds(tmp_path):
    """A local repository stands in for GitHub: what is tested is that init
    produces a working tree at the ref it was given, and that a failure names
    the failure without echoing the token."""
    origin = _origin(tmp_path)

    clone = tmp_path / "clone"
    initialize({
        VOLUME_DIR: str(tmp_path / "volume"),
        CLONE_URL: str(origin),
        CLONE_REF: "main",
        CLONE_DIR: str(clone),
    })
    assert (clone / "README.md").read_text(encoding="utf-8") == "product\n"


def test_the_clone_keeps_no_token_in_its_config(tmp_path, monkeypatch):
    """`git clone` writes the URL it was given into `.git/config` as the origin.
    A token in that URL outlives init: every role's agent holds it, and a dev's
    `git push` rides it — read-only — past the credential helper, so the first
    JobsRunner burst that pushed got a 403. A local repository stands in for
    GitHub through `insteadOf`, which rewrites the plain URL alone: a URL with
    the token in it does not match, and the clone fails."""
    origin = _origin(tmp_path)
    url = "https://github.com/owner/product.git"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[url "{origin.as_uri()}"]\n\tinsteadOf = {url}\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    token = "ghs-a-real-looking-token"

    clone = tmp_path / "clone"
    initialize({
        VOLUME_DIR: str(tmp_path / "volume"),
        CLONE_URL: url,
        CLONE_REF: "main",
        CLONE_DIR: str(clone),
        CLONE_TOKEN: token,
    })

    assert (clone / "README.md").read_text(encoding="utf-8") == "product\n"
    config = (clone / ".git" / "config").read_text(encoding="utf-8")
    assert token not in config
    assert "extraheader" not in config
    origin_url = subprocess.run(
        ["git", "-C", str(clone), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert origin_url == url


def test_a_clone_that_fails_never_echoes_the_token(tmp_path):
    with pytest.raises(PhaseError) as refused:
        initialize({
            VOLUME_DIR: str(tmp_path / "volume"),
            CLONE_URL: "https://github.com/owner/nothing-here.git",
            CLONE_DIR: str(tmp_path / "clone"),
            CLONE_TOKEN: "ghs-a-real-looking-token",
        })
    assert "ghs-a-real-looking-token" not in str(refused.value)
    assert "the clone failed" in str(refused.value)


def test_a_clone_with_nowhere_to_go_is_refused(tmp_path):
    with pytest.raises(PhaseError, match="nowhere to put the clone"):
        initialize({VOLUME_DIR: str(tmp_path / "volume"), CLONE_URL: "https://example.com/x.git"})


# --- harvest: what leaves through a URL ---


def _worked_volume(tmp_path):
    volume = tmp_path / "volume"
    (volume / "projects").mkdir(parents=True)
    (volume / "projects" / "sess.jsonl").write_text("this burst's turn\n", encoding="utf-8")
    (volume / BURST_RESULT_FILENAME).write_text(
        json.dumps({"session_id": "sess-1", "is_error": False, "input_tokens": 10}), encoding="utf-8"
    )
    (volume / VIOLATIONS_FILENAME).write_text(
        json.dumps({"tool": "Read", "reason": "outside the clone"}) + "\n", encoding="utf-8"
    )
    return volume


def test_harvest_ships_the_snapshot_and_the_record(tmp_path, served):
    volume = _worked_volume(tmp_path)
    shipped = harvest({
        VOLUME_DIR: str(volume),
        SESSION_PUT_URL: served.url("snapshot.tar.gz"),
        SESSION_KEY: KEY,
        ARTIFACTS_PUT_URL: served.url("artifacts.json"),
    })

    assert shipped.result["session_id"] == "sess-1"
    assert shipped.violations == [{"tool": "Read", "reason": "outside the clone"}]
    assert shipped.session_key == KEY and len(shipped.session_sha256) == 64

    record = Artifacts.from_json(served.read("artifacts.json"))
    assert record == shipped  # what the control plane will read is what was shipped
    # and the snapshot really is the volume, packed the one way
    assert len(served.read("snapshot.tar.gz")) > 0


def test_a_volume_with_no_snapshot_key_still_reports(tmp_path, served):
    """The reviewer's shape (B5): nothing is archived, but the result still has
    to reach the control plane — the volume dies with the pod."""
    volume = _worked_volume(tmp_path)
    shipped = harvest({VOLUME_DIR: str(volume), ARTIFACTS_PUT_URL: served.url("artifacts.json")})
    assert shipped.session_key is None and shipped.session_sha256 is None
    assert shipped.result is not None
    assert "snapshot.tar.gz" not in {p.name for p in served.root.rglob("*")}


def test_a_burst_that_wrote_nothing_ships_an_empty_record(tmp_path, served):
    volume = tmp_path / "volume"
    volume.mkdir()
    shipped = harvest({VOLUME_DIR: str(volume), ARTIFACTS_PUT_URL: served.url("artifacts.json")})
    assert shipped.result is None and shipped.violations == []


def test_the_harvester_never_ships_a_credential(tmp_path, served):
    """The exclusion is the packer's, not the caller's — a harvester cannot ship
    a token even by mistake, because it does not choose what goes in."""
    volume = _worked_volume(tmp_path)
    (volume / ".credentials.json").write_text('{"token": "oat-secret"}', encoding="utf-8")
    harvest({
        VOLUME_DIR: str(volume),
        SESSION_PUT_URL: served.url("snapshot.tar.gz"),
        SESSION_KEY: KEY,
        ARTIFACTS_PUT_URL: served.url("artifacts.json"),
    })
    assert b"oat-secret" not in served.read("snapshot.tar.gz")


def test_harvest_refuses_with_nowhere_to_report(tmp_path):
    with pytest.raises(PhaseError, match="nowhere to report"):
        harvest({VOLUME_DIR: str(_worked_volume(tmp_path))})
    with pytest.raises(PhaseError, match="not a directory"):
        harvest({VOLUME_DIR: str(tmp_path / "nothing"), ARTIFACTS_PUT_URL: "http://x/y"})


def test_an_upload_that_is_refused_fails_the_phase(tmp_path, served):
    served.refuse.add("artifacts.json")
    with pytest.raises(PhaseError, match="HTTP 403"):
        harvest({VOLUME_DIR: str(_worked_volume(tmp_path)), ARTIFACTS_PUT_URL: served.url("artifacts.json")})


# --- the entrypoints, as a pod invokes them ---


def test_the_entrypoints_are_environment_and_an_exit_code(tmp_path):
    volume = tmp_path / "volume"
    done = subprocess.run(
        [sys.executable, "-c", "import sys; from burst_phases.cli import init_main; sys.exit(init_main())"],
        env={"PATH": "/usr/bin:/bin", VOLUME_DIR: str(volume)},
        capture_output=True, text=True,
    )
    assert done.returncode == 0 and volume.is_dir()

    refused = subprocess.run(
        [sys.executable, "-c", "import sys; from burst_phases.cli import harvest_main; sys.exit(harvest_main())"],
        env={"PATH": "/usr/bin:/bin", VOLUME_DIR: str(volume)},
        capture_output=True, text=True,
    )
    assert refused.returncode == 1
    assert "burst-harvest" in refused.stderr and "nowhere to report" in refused.stderr


# --- the minting side ---


class Presigning(LocalSessionArchive):
    """A local archive that can hand out URLs — what the object store does for
    real (`S3SessionArchive.presign_*`, tested against SeaweedFS in the stores
    job). Here so the minting rules can be read without a bucket."""

    def presign_get(self, key: str, expires_in: int) -> str:
        return f"https://store.example/{key}?op=get&expires={expires_in}&sig=abc"

    def presign_put(self, key: str, expires_in: int) -> str:
        return f"https://store.example/{key}?op=put&expires={expires_in}&sig=abc"


def test_a_local_directory_has_no_urls_to_hand_out(tmp_path):
    """The refusal a deployment meets when it tries to run bursts off the
    control plane's own filesystem without an object store."""
    archive = LocalSessionArchive(tmp_path / "archive")
    with pytest.raises(ArchiveError, match="ARCHIVE_URL"):
        mint_phase_env(
            archive, volume_dir="/volume", record_key=RECORD_KEY, parent=None, session_key=KEY
        )


def test_minting_hands_out_one_capability_per_object(tmp_path):
    archive = Presigning(tmp_path / "archive")
    minted = mint_phase_env(
        archive,
        volume_dir="/volume",
        record_key=RECORD_KEY,
        parent=Snapshot(PARENT_KEY, "a" * 64),
        session_key=KEY,
        clone=("https://github.com/owner/product.git", "main", "/clone", "ghs-token"),
    )
    assert minted.init[VOLUME_DIR] == "/volume"
    assert PARENT_KEY in minted.init[SESSION_GET_URL]
    assert minted.init[SESSION_SHA256] == "a" * 64
    assert minted.init[CLONE_TOKEN] == "ghs-token"

    assert minted.harvest[SESSION_KEY] == KEY and KEY in minted.harvest[SESSION_PUT_URL]
    assert minted.record == RECORD_KEY and RECORD_KEY in minted.harvest[ARTIFACTS_PUT_URL]
    # the clone token is init's alone — the harvester has no use for it and no copy
    assert CLONE_TOKEN not in minted.harvest
    # and neither environment carries a credential for the store itself
    assert not any("SECRET" in name or "ACCESS_KEY" in name for name in {**minted.init, **minted.harvest})


def test_a_reviewer_gets_no_put_for_a_snapshot(tmp_path):
    archive = Presigning(tmp_path / "archive")
    minted = mint_phase_env(
        archive,
        volume_dir="/volume",
        record_key=RECORD_KEY,
        parent=Snapshot(PARENT_KEY, "a" * 64),
        session_key=None,
    )
    assert SESSION_PUT_URL not in minted.harvest and SESSION_KEY not in minted.harvest
    # the result still has to come home, and under *this* burst's prefix — a
    # reviewer archives nothing, and two bursts off one parent must not collide
    assert ARTIFACTS_PUT_URL in minted.harvest
    assert RECORD_KEY in minted.harvest[ARTIFACTS_PUT_URL]


def test_a_burst_with_nowhere_to_report_is_refused(tmp_path):
    archive = LocalSessionArchive(tmp_path / "archive")
    with pytest.raises(ArchiveError, match="nowhere to report"):
        mint_phase_env(archive, volume_dir="/volume", record_key="", parent=None, session_key=None)


def test_the_artifacts_key_sits_beside_the_snapshot():
    assert artifacts_key(KEY) == "sessions/owner/product#42/b1/artifacts.json"
    assert PHASE_URL_TTL_S >= 3600  # longer than any burst's rail, and it expires


def test_what_the_harvester_shipped_reads_back_as_the_control_planes_own_shape(tmp_path, served):
    """The seam: a `BurstArtifacts` from a pod is indistinguishable from one a
    host phase produced, so `fold_artifacts` cannot tell where the burst ran."""
    archive = LocalSessionArchive(tmp_path / "archive")
    volume = _worked_volume(tmp_path)
    harvest({
        VOLUME_DIR: str(volume),
        SESSION_PUT_URL: served.url("snapshot.tar.gz"),
        SESSION_KEY: KEY,
        ARTIFACTS_PUT_URL: served.url("record.json"),
    })
    # the archive receives what the harvester PUT — here by hand, since the
    # local archive has no HTTP surface to presign
    record_key = artifacts_key(KEY)
    Path(archive.root / record_key).parent.mkdir(parents=True, exist_ok=True)
    Path(archive.root / record_key).write_bytes(served.read("record.json"))

    artifacts = read_artifacts(archive, record_key)
    assert artifacts.result["session_id"] == "sess-1"
    assert artifacts.violations == [{"tool": "Read", "reason": "outside the clone"}]
    assert artifacts.session_key == KEY and len(artifacts.session_checksum) == 64

    with pytest.raises(ArchiveError, match="left no record"):
        read_artifacts(archive, "sessions/owner/product#42/b9/artifacts.json")
