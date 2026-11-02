"""Test-wide defaults.

`open_ledger` honours `LEDGER_DSN` and `open_archive` honours `ARCHIVE_URL`, which
means that once either is set — as both are for the whole service-backed CI job —
*every* `PlatformStores.open(data_dir)` stops using `data_dir` for that store and
goes to the shared one instead. That is correct in production (the DSN *is* the
ledger, the URL *is* the archive, and `--data-dir` never had a say) and wrong for a
test suite, where it turns one database and one bucket into order-dependent
fixtures for tests that only ever wanted scratch space.

The archive makes this sharper than the ledger did: snapshots are write-once, so
two tests that happen to agree on a task and burst id don't interleave, they fail —
and the second one fails somewhere unrelated to what it was testing.

So: by default a test gets the single-node backends, isolated under its own
tmp_path. The files that mean to exercise the service backends read the variables
at import time into module constants and pass them explicitly, so they are
unaffected by this and still run — which is what lets one `pytest` invocation
cover both backends of both stores without any of them interfering.

Full-scenario parity across backends is gate A7's rehearsal (#18), not this
suite's job: the scheduler tests fake GitHub and docker, and giving each of them
its own database and key prefix would cost a CREATE DATABASE per test to prove
something the two contract suites already prove per method.
"""

import pytest
from control_plane.store import ARCHIVE_URL_ENV, LEDGER_DSN_ENV


@pytest.fixture(autouse=True)
def isolated_stores_by_default(monkeypatch):
    monkeypatch.delenv(LEDGER_DSN_ENV, raising=False)
    monkeypatch.delenv(ARCHIVE_URL_ENV, raising=False)


@pytest.fixture()
def spans():
    """What the control plane traced during the test, ended spans in order.

    The process has one tracer provider (`init_telemetry` installs it once), so
    each test adds its own in-memory exporter to it rather than a provider of
    its own; the exporter is shut at teardown and takes nothing after.
    """
    from control_plane.telemetry import init_telemetry
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    init_telemetry()
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.shutdown()
