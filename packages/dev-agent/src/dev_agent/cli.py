"""Container entrypoint: ``dev-agent run`` (a burst) / ``check`` (boot smoke) / ``walls`` (M4 probes).

``check`` verifies the box is viable — env contract satisfied, clone present,
the SDK's bundled ``claude`` CLI in place, and, when told to resume, the
session's transcript on the volume — without touching the network — and
reports which SDK and CLI the image carries. It is the M0 exit criterion
("docker run boots an agent in a clone") and the first thing to run when
debugging an image.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import claude_agent_sdk
import structlog
from claude_agent_sdk._cli_version import __cli_version__

from dev_agent.burst import run_burst_sync
from dev_agent.env import BurstEnv, EnvError
from dev_agent.log import configure_logging
from dev_agent.probes import walls

log = structlog.get_logger()

# The CLI a burst runs (#120). The SDK's transport prefers the binary its wheel
# bundles over anything on PATH, so this is the engine — and its version is
# the lock's, because the image installs the lock's SDK.
BUNDLED_CLI = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


def _check(env: BurstEnv) -> int:
    findings: dict[str, bool | str] = {
        "env": True,
        "claude_cli": os.access(BUNDLED_CLI, os.X_OK),
        "repo_dir": env.repo_dir.is_dir(),
        "git_repo": (env.repo_dir / ".git").exists(),
        "session_dir": env.session_dir.is_dir(),
    }
    if env.resume_session_id:
        # the restore half of the snapshot choreography, checked without the
        # engine: the transcript the CLI would resume is on the volume (G4)
        findings["resume_transcript"] = any(
            env.session_dir.glob(f"projects/*/{env.resume_session_id}.jsonl")
        )
    ok = all(v is True for v in findings.values())
    # reported, not checked: which engine this image carries, whatever day it was built
    findings |= {
        "sdk_version": claude_agent_sdk.__version__,
        "cli_version": __cli_version__,
        "at": datetime.now(UTC).isoformat(),
    }
    env.result_path.parent.mkdir(parents=True, exist_ok=True)
    env.result_path.write_text(
        json.dumps({"check": findings, "ok": ok}, indent=2), encoding="utf-8"
    )
    print(json.dumps(findings, indent=2))
    print("check:", "ok" if ok else "FAILED")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dev-agent",
        description="Burst worker — configured entirely by environment (see dev_agent.env).",
    )
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "walls"])
    args = parser.parse_args(argv)
    configure_logging()

    try:
        env = BurstEnv.from_environ()
    except EnvError as exc:
        log.error("environment contract unsatisfied", error=str(exc))
        return 2

    # Correlation in-band, for the whole process (TO-BE §12). This side of the
    # boundary has no ledger and no trace backend — what it has is a stdout that
    # gets teed to `data/logs/<burst_id>.log`, and that file is often all a reader
    # has. The ids come from the environment because the control plane put them
    # there; nothing here invents one.
    bindings = {"task_id": env.task_id, "burst_id": env.burst_id}
    if env.trace_id:
        bindings["trace_id"] = env.trace_id
    structlog.contextvars.bind_contextvars(**bindings)

    if args.command == "check":
        return _check(env)
    if args.command == "walls":
        return walls(env)  # the box tries its own walls (G6); no engine, no key

    result = run_burst_sync(env)
    log.info(
        "burst done",  # task and burst ids come from the bound context, not from here
        session=result.session_id,
        turns=result.num_turns,
        cost_usd=result.total_cost_usd,
        error=result.is_error,
    )
    return 1 if result.is_error else 0


if __name__ == "__main__":
    sys.exit(main())
