"""GitHub App auth: mint short-lived installation tokens for bursts.

The App's private key never leaves the control plane (a wall, §8). Each burst
gets a fresh **installation access token** — 1-hour expiry, scoped to whatever
the App was granted (contents + pull_requests + issues write; no merge/admin).
Token lifetime bounding burst lifetime is a feature: keep the wall-clock cap
under an hour or the push at the end of a long burst fails.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import jwt
import requests

from secret_refs import SecretError, SecretResolver

from control_plane.telemetry import step_in_trace

GITHUB_API = "https://api.github.com"

APP_ID = "GITHUB_APP_ID"
PRIVATE_KEY_PATH = "GITHUB_APP_PRIVATE_KEY_PATH"
INSTALLATION_ID = "GITHUB_APP_INSTALLATION_ID"

# The role-prefix scheme (TO-BE §4, D1): one org-level App per pipeline role.
# App names are the adopter's choice — the platform's contract is these env
# prefixes over the same three names. The dev App reads them bare; MONITOR_ is
# reserved now, its App arrives with the monitor at T4.
REVIEWER_APP_PREFIX = "REVIEWER_"
TESTER_APP_PREFIX = "TESTER_"
MONITOR_APP_PREFIX = "MONITOR_"
ROLE_PREFIXES: dict[str, str] = {
    "dev": "",
    "reviewer": REVIEWER_APP_PREFIX,
    "tester": TESTER_APP_PREFIX,
    "monitor": MONITOR_APP_PREFIX,
}

# The burst token tier's per-role grant (TO-BE §4, D2): minted *below* the
# App's own grant, so "a burst can't reach another product" (§13) is a
# per-token fact. Only the dev role holds a GitHub credential inside the
# container at all — reviewer and tester bursts stay credential-free, which
# is the stronger wall; their absence here is deliberate.
BURST_PERMISSIONS: dict[str, dict[str, str]] = {
    "dev": {"contents": "write", "issues": "write", "pull_requests": "write"},
}

# What an init container is given to fetch the product with, when the burst runs
# somewhere that inherits no mount (G3). It is not a role's grant: every role's
# working tree is read the same way, and the container that holds this token is
# dead before the agent's first turn. A reviewer or tester *agent* still holds
# nothing — that wall is which container the token reaches, not which scope it has.
CLONE_PERMISSIONS: dict[str, str] = {"contents": "read"}

# requests-compatible callables, injectable for tests
HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]


class GitHubAuthError(RuntimeError):
    """App credentials are missing, wrong, or the exchange failed."""


@dataclass(slots=True)
class AppCredentials:
    app_id: str
    private_key: str
    installation_id: str | None = None

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None, prefix: str = ""
    ) -> AppCredentials | None:
        """None when the App is not configured (burst runs without GitHub).

        ``prefix`` selects a second App identity from the same env — the
        reviewer App reads ``REVIEWER_GITHUB_APP_ID`` etc. (REVIEWERS.md).
        ``*_GITHUB_APP_PRIVATE_KEY_PATH`` holds a secret ref — a path, or any
        ``env:``/``file:``/``helper:`` ref the SecretResolver takes (D4).
        """
        env = dict(os.environ) if environ is None else environ
        app_id = env.get(prefix + APP_ID, "").strip()
        key_ref = env.get(prefix + PRIVATE_KEY_PATH, "").strip()
        if not app_id and not key_ref:
            return None
        if not app_id or not key_ref:
            raise GitHubAuthError(f"set both {prefix + APP_ID} and {prefix + PRIVATE_KEY_PATH} (or neither)")
        # the key is a secret ref (D4): a plain path keeps its pre-D4 meaning
        # (file:), and env:/helper: reach wherever the adopter materialized it
        try:
            private_key = SecretResolver(env).resolve(key_ref, bare_as="file")
        except SecretError as exc:
            raise GitHubAuthError(f"{prefix + PRIVATE_KEY_PATH}: {exc}") from exc
        return cls(
            app_id=app_id,
            private_key=private_key,
            installation_id=env.get(prefix + INSTALLATION_ID, "").strip() or None,
        )


@dataclass(slots=True)
class InstallationToken:
    token: str
    expires_at: str
    permissions: dict[str, str]
    app_slug: str = ""

    @property
    def bot_login(self) -> str:
        return f"{self.app_slug}[bot]"


def make_app_jwt(creds: AppCredentials, now: int | None = None) -> str:
    """A 9-minute RS256 JWT identifying the App itself (not an installation)."""
    at = int(time.time()) if now is None else now
    payload = {"iat": at - 60, "exp": at + 540, "iss": creds.app_id}
    return jwt.encode(payload, creds.private_key, algorithm="RS256")


def _check(send: Callable[[], Any], what: str) -> Any:
    """Make the call and read it: a transport failure (no response — DNS, a
    dropped link, a timeout) and a bad status are the same GitHubAuthError to
    the caller, which retries next tick instead of dying (E6)."""
    with step_in_trace("github.request", **{"github.operation": what}) as span:
        try:
            response = send()
        except requests.RequestException as exc:
            raise GitHubAuthError(f"{what} failed: {exc.__class__.__name__}: {str(exc)[:300]}") from exc
        span.set_attribute("http.response.status_code", response.status_code)
        if response.status_code >= 300:
            raise GitHubAuthError(f"{what} failed: HTTP {response.status_code} {response.text[:300]}")
        return response.json()


def discover_installation(
    creds: AppCredentials, http_get: HttpGet = requests.get
) -> tuple[str, str]:
    """(installation_id, app_slug). Requires exactly one installation unless pinned."""
    headers = {"Authorization": f"Bearer {make_app_jwt(creds)}"}
    installations = _check(
        lambda: http_get(f"{GITHUB_API}/app/installations", headers=headers, timeout=30),
        "listing installations",
    )
    if not installations:
        raise GitHubAuthError("the App has no installations — install it on the product repo")
    if creds.installation_id:
        for inst in installations:
            if str(inst["id"]) == creds.installation_id:
                return creds.installation_id, inst["app_slug"]
        raise GitHubAuthError(f"installation {creds.installation_id} not found for this App")
    if len(installations) > 1:
        ids = ", ".join(str(inst["id"]) for inst in installations)
        raise GitHubAuthError(f"multiple installations ({ids}) — set {INSTALLATION_ID}")
    return str(installations[0]["id"]), installations[0]["app_slug"]


def mint_installation_token(
    creds: AppCredentials,
    http_get: HttpGet = requests.get,
    http_post: HttpPost = requests.post,
    repositories: list[str] | None = None,
    permissions: dict[str, str] | None = None,
) -> InstallationToken:
    """Mint an installation token; ``repositories``/``permissions`` downscope
    it below the App's grant at mint (D2). ``repositories`` takes bare repo
    names, resolved within the installation. Omitted = the App's full grant —
    the broad, cached control-plane tier."""
    installation_id, app_slug = discover_installation(creds, http_get)
    headers = {"Authorization": f"Bearer {make_app_jwt(creds)}"}
    body: dict[str, Any] = {}
    if repositories is not None:
        body["repositories"] = repositories
    if permissions is not None:
        body["permissions"] = permissions
    data = _check(
        lambda: http_post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers=headers,
            json=body or None,
            timeout=30,
        ),
        "minting installation token",
    )
    return InstallationToken(
        token=data["token"],
        expires_at=data.get("expires_at", ""),
        permissions=data.get("permissions", {}),
        app_slug=app_slug,
    )


def bot_git_identity(token: InstallationToken, app_id: str) -> tuple[str, str]:
    """(user.name, user.email) for commits authored by the App bot."""
    name = token.bot_login
    email = f"{app_id}+{name}@users.noreply.github.com"
    return name, email


class InstallationTokenCache:
    """Callable token supplier that re-mints within 5 minutes of expiry.

    Serves the scheduler's polling across hour-plus runs; bursts still mint
    their own fresh token each spawn.
    """

    MARGIN_S = 300

    def __init__(self, creds: AppCredentials) -> None:
        self._creds = creds
        self._token: str | None = None
        self._expires: float = 0.0

    def __call__(self) -> str:
        if self._token is None or time.time() > self._expires - self.MARGIN_S:
            minted = mint_installation_token(self._creds)
            self._token = minted.token
            self._expires = _expiry_epoch(minted.expires_at)
        return self._token


def _expiry_epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 3600  # GitHub's default installation-token lifetime
