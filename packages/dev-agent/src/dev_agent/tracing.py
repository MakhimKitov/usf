"""The worker's own span: the box's link in a burst's trace (ARCHITECTURE.md §9).

The control plane hands the box a ``TRACEPARENT`` — its ``burst.run`` step —
and the CLI the SDK drives nests its tree under the context it is given.
Between the two sat this process, invisible: how long it took to start, the
walls it answered, how the run ended. This module gives it one span,
``agent.burst``, under the control plane's, and keeps it current while the SDK
runs. That is all the SDK needs: it injects the active OpenTelemetry context
into the CLI's environment, so the CLI's tree lands under this span instead of
beside it.

The switch is the CLI's own. The control plane turns a burst's telemetry on
with the variables `agent_telemetry_env` sets, and this process reads the same
ones — one declaration, two emitters in the box. Unset, nothing here runs, no
span is current, and the CLI inherits the control plane's ``TRACEPARENT``
exactly as it did before this module existed.

Nothing is printed: stdout is the burst's log, teed by the runner, and a
console exporter would bury the agent's prose in span dumps.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import INVALID_SPAN, Span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from dev_agent import env as agentenv
from dev_agent.env import BurstEnv

TRACER_NAME = "dev_agent"
SPAN_NAME = "agent.burst"

# How long the last export may hold the box's exit. A burst is over by then,
# and a collector that is down must not keep a finished pod alive: the CLI's
# own exporter gives up silently, and this one gives up after this.
EXPORT_TIMEOUT_S = 5


def enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether the control plane turned this burst's telemetry on (B2)."""
    env = os.environ if environ is None else environ
    return (
        env.get(agentenv.CLAUDE_CODE_ENABLE_TELEMETRY) == "1"
        and env.get(agentenv.OTEL_TRACES_EXPORTER) == "otlp"
        and bool(env.get(agentenv.OTEL_EXPORTER_OTLP_ENDPOINT, "").strip())
    )


def configure(
    environ: dict[str, str] | None = None, *, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """The worker's provider, or None when the burst's telemetry is off.

    Not installed as the process's global provider: nothing else in the box
    asks for one, and a span only has to be *current* for the SDK to hand its
    context to the CLI. ``exporter`` replaces OTLP — the suite passes an
    in-memory one. The resource is read from ``OTEL_SERVICE_NAME`` and
    ``OTEL_RESOURCE_ATTRIBUTES``, the ones the control plane already sets for
    the CLI, so both emitters in the box describe themselves identically.
    """
    env = os.environ if environ is None else environ
    if exporter is None and not enabled(env):
        return None
    provider = TracerProvider(resource=Resource.create())
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = env[agentenv.OTEL_EXPORTER_OTLP_ENDPOINT].strip().rstrip("/")
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", timeout=EXPORT_TIMEOUT_S)
            )
        )
    return provider


@contextmanager
def agent_span(provider: TracerProvider | None, env: BurstEnv) -> Iterator[Span]:
    """The worker's span for one burst, current for the block; a no-op without
    a provider. Its parent is the ``TRACEPARENT`` the control plane injected —
    a malformed one costs the parent, never the burst, and the span starts a
    trace of its own."""
    if provider is None:
        yield INVALID_SPAN
        return
    parent = (
        TraceContextTextMapPropagator().extract({"traceparent": env.traceparent})
        if env.traceparent
        else None
    )
    attributes: dict[str, str | bool] = {
        "task.id": env.task_id,
        "burst.id": env.burst_id,
        "burst.role": env.role,
        "burst.kind": env.burst_kind,
        "burst.resumed": env.resume_session_id is not None,
    }
    if env.model:
        attributes["burst.model"] = env.model
    if env.effort:
        attributes["burst.effort"] = env.effort
    tracer = provider.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(SPAN_NAME, context=parent, attributes=attributes) as span:
        yield span
