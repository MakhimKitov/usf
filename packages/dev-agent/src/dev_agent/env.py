"""The container environment contract.

Everything a burst needs arrives as environment variables: the control plane sets
them, the container consumes them. ``control_plane.runner`` imports these constants,
so the two sides cannot drift. The dependency points one way only — this package
must never import ``control_plane`` (the image cannot contain platform code).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# The two files a burst leaves in its volume are declared in `session-volume`
# (G2): three parties have to agree on them and none may import the others —
# this worker writes them, the control plane folds them, and the pod-side
# harvester ships them. Re-exported below so the env contract still reads whole.
from session_volume import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME

# --- variable names we own (the contract) ---
ROLE = "AGENT_ROLE"
TASK_ID = "AGENT_TASK_ID"
BURST_ID = "AGENT_BURST_ID"
BURST_KIND = "AGENT_BURST_KIND"
TASK_PROMPT = "AGENT_TASK_PROMPT"
REPO_DIR = "AGENT_REPO_DIR"
RESUME_SESSION_ID = "AGENT_RESUME_SESSION_ID"
# Set when the resumed node already has an abandoned attempt grown from it (H5):
# the burst is a new branch, so the SDK forks the session to a new id rather
# than continuing the one the abandoned attempt continued.
FORK_SESSION = "AGENT_FORK_SESSION"
MAX_BUDGET_USD = "AGENT_MAX_BUDGET_USD"
MAX_TURNS = "AGENT_MAX_TURNS"
PERMISSION_MODE = "AGENT_PERMISSION_MODE"
MODEL = "AGENT_MODEL"
# How hard the model thinks (#119): the role row's `effort`, unset when the row
# leaves it to the CLI's default for the model.
EFFORT = "AGENT_EFFORT"
GIT_USER_NAME = "AGENT_GIT_USER_NAME"
GIT_USER_EMAIL = "AGENT_GIT_USER_EMAIL"

# --- standard variables we rely on but don't own ---
CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"
TRACEPARENT = "TRACEPARENT"
# Subscription auth (claude setup-token). NOTE: ANTHROPIC_API_KEY outranks the
# OAuth token in the CLI's precedence — the runner must inject exactly one.
CLAUDE_CODE_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
# The short-lived installation token, if the burst talks to GitHub. GH_TOKEN is
# the same value — the gh CLI reads it natively.
GITHUB_TOKEN = "GITHUB_TOKEN"
GH_TOKEN = "GH_TOKEN"
# The CLI's own ceiling on a Bash call (H2, #104). Read by the CLI the SDK
# drives, not by this package — named here because the control plane sets them
# and the contract is one list.
BASH_DEFAULT_TIMEOUT_MS = "BASH_DEFAULT_TIMEOUT_MS"
BASH_MAX_TIMEOUT_MS = "BASH_MAX_TIMEOUT_MS"

# Claude Code's own telemetry (B2). Set by the control plane, read by the CLI the
# SDK drives — this package reads none of them, and doesn't need to: the SDK passes
# the process environment to the CLI, and the CLI's exporter is its own. Named here
# so the two sides can't drift, like everything else in this module.
#
# Reference: code.claude.com/docs/en/monitoring-usage. Traces are beta and take two
# switches, ENABLE_TELEMETRY plus ENHANCED_TELEMETRY_BETA; span names and attributes
# may change between CLI releases, so `control_plane.telemetry` records the version
# this was verified against.
CLAUDE_CODE_ENABLE_TELEMETRY = "CLAUDE_CODE_ENABLE_TELEMETRY"
CLAUDE_CODE_ENHANCED_TELEMETRY_BETA = "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"
OTEL_TRACES_EXPORTER = "OTEL_TRACES_EXPORTER"
OTEL_LOGS_EXPORTER = "OTEL_LOGS_EXPORTER"
OTEL_METRICS_EXPORTER = "OTEL_METRICS_EXPORTER"
OTEL_EXPORTER_OTLP_PROTOCOL = "OTEL_EXPORTER_OTLP_PROTOCOL"
OTEL_EXPORTER_OTLP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTEL_TRACES_EXPORT_INTERVAL = "OTEL_TRACES_EXPORT_INTERVAL"
OTEL_LOGS_EXPORT_INTERVAL = "OTEL_LOGS_EXPORT_INTERVAL"
OTEL_SERVICE_NAME = "OTEL_SERVICE_NAME"
OTEL_RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"
OTEL_LOG_USER_PROMPTS = "OTEL_LOG_USER_PROMPTS"
OTEL_LOG_TOOL_DETAILS = "OTEL_LOG_TOOL_DETAILS"

# What the box reports itself as, next to the control plane's own `control-plane`.
AGENT_SERVICE_NAME = "dev-agent"

ROLES = ("dev", "reviewer", "tester")
BURST_KINDS = ("work", "review", "assess", "test")
# The SDK's `EffortLevel`, spelled here so the control plane can check a role
# row against it without importing the SDK. `test_env_contract` holds the two
# equal, so an SDK that adds a level fails the suite rather than a burst.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# --- container filesystem contract (mount points, well-known files) ---
CONTAINER_SESSION_DIR = "/session"  # control-plane-owned volume; CLAUDE_CONFIG_DIR
CONTAINER_REPO_DIR = "/workspace/repo"  # the product clone
__all_shape__ = (BURST_RESULT_FILENAME, VIOLATIONS_FILENAME)  # imported above, re-exported here

# Walls are hooks + the container + the scoped token — not interactive prompts,
# which nobody is present to answer.
DEFAULT_PERMISSION_MODE = "bypassPermissions"


class EnvError(ValueError):
    """The environment does not satisfy the burst contract."""


@dataclass(slots=True)
class BurstEnv:
    """A parsed, validated burst environment."""

    task_id: str
    burst_id: str
    task_prompt: str = ""
    role: str = "dev"
    burst_kind: str = "work"
    repo_dir: Path = field(default_factory=lambda: Path(CONTAINER_REPO_DIR))
    session_dir: Path = field(default_factory=lambda: Path(CONTAINER_SESSION_DIR))
    resume_session_id: str | None = None
    fork_session: bool = False
    max_budget_usd: float | None = None
    max_turns: int | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    model: str | None = None
    effort: str | None = None
    github_token: str | None = None
    git_user_name: str | None = None
    git_user_email: str | None = None
    traceparent: str = ""

    @property
    def trace_id(self) -> str:
        """The trace this burst belongs to, or "" — read out of the traceparent.

        Parsed by hand rather than with an OTel API on purpose: what a log line
        needs is the id, and the image should not have to carry a tracing SDK to
        write a correlated line (B2 gives the agent its own exporter; this is not
        that). The field is validated because it is a value the container is *told*
        — a malformed one should cost the trace_id on a log line and nothing else.
        """
        parts = self.traceparent.split("-")
        if len(parts) != 4 or parts[0] != "00" or len(parts[1]) != 32:
            return ""
        trace_id = parts[1].lower()
        if not all(c in "0123456789abcdef" for c in trace_id) or trace_id == "0" * 32:
            return ""
        return trace_id

    @property
    def result_path(self) -> Path:
        return self.session_dir / BURST_RESULT_FILENAME

    @property
    def violations_path(self) -> Path:
        return self.session_dir / VIOLATIONS_FILENAME

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> BurstEnv:
        env = dict(os.environ) if environ is None else environ
        missing = [name for name in (TASK_ID, BURST_ID) if not env.get(name)]
        if missing:
            raise EnvError(f"missing required environment: {', '.join(missing)}")

        role = env.get(ROLE, "dev")
        if role not in ROLES:
            raise EnvError(f"{ROLE} must be one of {ROLES}, got {role!r}")
        kind = env.get(BURST_KIND, "work")
        if kind not in BURST_KINDS:
            raise EnvError(f"{BURST_KIND} must be one of {BURST_KINDS}, got {kind!r}")
        effort = env.get(EFFORT) or None
        if effort is not None and effort not in EFFORT_LEVELS:
            raise EnvError(f"{EFFORT} must be one of {EFFORT_LEVELS}, got {effort!r}")

        def _float(name: str) -> float | None:
            raw = env.get(name)
            try:
                return float(raw) if raw else None
            except ValueError as exc:
                raise EnvError(f"{name} must be a number, got {raw!r}") from exc

        def _int(name: str) -> int | None:
            raw = env.get(name)
            try:
                return int(raw) if raw else None
            except ValueError as exc:
                raise EnvError(f"{name} must be an integer, got {raw!r}") from exc

        return cls(
            task_id=env[TASK_ID],
            burst_id=env[BURST_ID],
            task_prompt=env.get(TASK_PROMPT, ""),
            role=role,
            burst_kind=kind,
            repo_dir=Path(env.get(REPO_DIR, CONTAINER_REPO_DIR)),
            session_dir=Path(env.get(CLAUDE_CONFIG_DIR, CONTAINER_SESSION_DIR)),
            resume_session_id=env.get(RESUME_SESSION_ID) or None,
            fork_session=env.get(FORK_SESSION) == "1",
            max_budget_usd=_float(MAX_BUDGET_USD),
            max_turns=_int(MAX_TURNS),
            permission_mode=env.get(PERMISSION_MODE, DEFAULT_PERMISSION_MODE),
            model=env.get(MODEL) or None,
            effort=effort,
            github_token=env.get(GITHUB_TOKEN) or None,
            git_user_name=env.get(GIT_USER_NAME) or None,
            git_user_email=env.get(GIT_USER_EMAIL) or None,
            traceparent=env.get(TRACEPARENT, ""),
        )
