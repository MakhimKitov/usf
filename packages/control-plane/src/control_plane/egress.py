"""A burst's egress, declared as data (G6, TO-BE §6 "Network walls").

The platform does not enforce egress — that is the adopter's CNI, and a policy
the platform wrote for one CNI would be an instrument, not a capability. What
the platform owes is the **declaration**: for every burst, the exact hosts it
needs, each with the reason, as structured metadata on the Job. An adopter's
policy is then generated from (or checked against) data rather than guessed,
and the M4 suite proves on *their* cluster that an undeclared host fails.

What a burst needs, and nothing else:

- **GitHub** — the init phase clones over HTTPS, and a dev agent pushes and
  opens its PR. One pod, one network namespace: the clone's host is the pod's.
- **the inference API** — the engine's one upstream.
- **the toolchain's package index** — the dev-agent image's toolchain is uv, and
  until warm product images bake the dependencies in (TO-BE §6), `uv sync`
  fetches them. A burst that has to install the product's dependencies and
  cannot is a wall that blocks the work, which is not a wall but a bug.
- **the archive** — the phases' presigned GET and PUTs, by the host the URLs
  were minted for.
- **the OTLP endpoint** — when the deployment exports the agent's telemetry.
- **the product's sandbox** — tester bursts only: the hosts the product's
  `TESTING.md` declares (`egress:`), the same manifest its credentials come from.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from dev_agent import env as agentenv

from control_plane.runner import BurstSpec, burst_env

# The annotation a Job carries its declaration under — JSON, a list of entries.
EGRESS_ANNOTATION = "usf/egress"

GITHUB_HOSTS = ("github.com", "api.github.com")
INFERENCE_HOST = "api.anthropic.com"
TOOLCHAIN_HOSTS = ("pypi.org", "files.pythonhosted.org")


@dataclass(frozen=True, slots=True)
class Egress:
    host: str
    port: int
    why: str


def declaration(spec: BurstSpec, phase_urls: list[str] = ()) -> list[Egress]:
    """Every host ``spec``'s pod may reach, each once, with why. ``phase_urls``
    are the presigned URLs the phases were minted — their hosts are the archive."""
    entries: list[Egress] = [Egress(host, 443, "GitHub: the clone, and a dev burst's push and PR") for host in GITHUB_HOSTS]
    if spec.command == "run":
        entries.append(Egress(INFERENCE_HOST, 443, "the inference API"))
        entries += [Egress(host, 443, "the toolchain's package index (uv)") for host in TOOLCHAIN_HOSTS]
    for url in phase_urls:
        host, port = _host_port(url)
        if host:
            entries.append(Egress(host, port, "the archive: the phases' presigned URLs"))
    otlp = burst_env(spec).get(agentenv.OTEL_EXPORTER_OTLP_ENDPOINT)
    if otlp:
        host, port = _host_port(otlp)
        if host:
            entries.append(Egress(host, port, "the agent's telemetry (OTLP)"))
    if spec.role == "tester":
        entries += [Egress(host, 443, "the product's sandbox (TESTING.md)") for host in spec.egress]
    seen: set[tuple[str, int]] = set()
    unique = []
    for entry in entries:
        if (entry.host, entry.port) not in seen:
            seen.add((entry.host, entry.port))
            unique.append(entry)
    return unique


def as_json(entries: list[Egress]) -> list[dict]:
    return [asdict(entry) for entry in entries]


def _host_port(url: str) -> tuple[str | None, int]:
    parts = urlsplit(url)
    port = parts.port or (80 if parts.scheme == "http" else 443)
    return parts.hostname, port
