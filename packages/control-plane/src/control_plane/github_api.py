"""The scheduler's GitHub surface: read facts, reflect state (GITHUB-INTERFACE.md).

One repo, App-installation auth, a requests-compatible injectable transport —
the same testing seam as ``github_auth``. Every call is a small explicit REST
operation; pagination is a single page (phase-0 scale: a handful of open
issues and PRs).
"""

from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from control_plane.github_auth import GITHUB_API
from control_plane.telemetry import step_in_trace

COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"
# GitHub records a granted Copilot request under this login, not the [bot] one
COPILOT_REQUESTED_LOGIN = "Copilot"
# Copilot reviews can only be *requested* by a user with a Copilot seat — never
# by an App/bot token (GitHub answers 201 and silently drops the reviewer).
COPILOT_TOKEN_ENV = "GITHUB_COPILOT_REVIEW_TOKEN"

HttpRequest = Callable[..., Any]


class GitHubApiError(RuntimeError):
    """A GitHub call the scheduler depends on failed."""


@dataclass(slots=True)
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    state: str = "open"  # open | closed — a closed issue is a finished task (#93)

    @property
    def provenance(self) -> str:
        """``human`` only when explicitly labelled; everything else is tainted."""
        return "human" if "human" in self.labels else "auto"


@dataclass(slots=True)
class PullRequest:
    number: int
    state: str  # open | closed (merged PRs are closed + merged)
    merged: bool
    head_ref: str
    head_sha: str


@dataclass(slots=True)
class Review:
    id: int
    author: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    body: str


@dataclass(slots=True)
class ReviewComment:
    id: int  # the thread id the dev replies to (pulls/<n>/comments/<id>/replies)
    review_id: int | None
    author: str
    path: str
    line: int | None
    body: str


def _issue_of(item: dict) -> Issue:
    labels = [l["name"] if isinstance(l, dict) else str(l) for l in item.get("labels", [])]
    return Issue(
        number=item["number"],
        title=item.get("title", ""),
        body=item.get("body") or "",
        labels=labels,
        state=item.get("state", "open"),
    )


def _pr_of(item: dict) -> PullRequest:
    head = item.get("head") or {}
    return PullRequest(
        number=item["number"],
        state=item.get("state", ""),
        merged=bool(item.get("merged") or item.get("merged_at")),
        head_ref=head.get("ref", ""),
        head_sha=head.get("sha", ""),
    )


class GitHubApi:
    """Operations against one repo. ``token`` may be a string or a supplier
    callable (the scheduler passes ``InstallationTokenCache`` so hour-long
    runs survive token expiry)."""

    def __init__(
        self,
        repo: str,
        token: str | Callable[[], str],
        request: HttpRequest = requests.request,
        copilot_token: str | None = None,
    ) -> None:
        self.repo = repo  # "owner/name"
        self._token = token if callable(token) else (lambda: token)
        self._request = request
        self._copilot_token = copilot_token or None

    def _call(
        self,
        method: str,
        path: str,
        what: str,
        *,
        params: dict | None = None,
        json: Any = None,
        ok: tuple[int, ...] = (),
        token: str | None = None,
    ) -> Any:
        # a span inside a turn and nothing outside one (`step_in_trace`): the
        # method, the path and what the call was for — never the token
        with step_in_trace(
            "github.request",
            **{"github.operation": what, "http.request.method": method, "url.path": f"/repos/{self.repo}{path}"},
        ) as span:
            try:
                response = self._request(
                    method,
                    f"{GITHUB_API}/repos/{self.repo}{path}",
                    headers={
                        "Authorization": f"Bearer {token or self._token()}",
                        "Accept": "application/vnd.github+json",
                    },
                    params=params,
                    json=json,
                    timeout=30,
                )
            except requests.RequestException as exc:
                # no response at all — DNS, a dropped link, a timeout. The same
                # failure as a bad status to every caller: the tick skips or retries,
                # the replica lives (E6: a name-resolution blip took a replica down)
                raise GitHubApiError(f"{what}: {exc.__class__.__name__}: {str(exc)[:300]}") from exc
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 300 and response.status_code not in ok:
                raise GitHubApiError(f"{what}: HTTP {response.status_code} {response.text[:300]}")
        try:
            return response.json()
        except ValueError:
            return None

    # --- issues ---

    def list_ready_issues(self) -> list[Issue]:
        data = self._call(
            "GET",
            "/issues",
            "listing ready issues",
            params={"labels": "ready", "state": "open", "per_page": 100},
        )
        # the issues endpoint returns PRs too — they carry a pull_request key
        return [_issue_of(item) for item in data if "pull_request" not in item]

    def get_issue(self, number: int) -> Issue:
        return _issue_of(self._call("GET", f"/issues/{number}", f"reading issue #{number}"))

    def list_open_issues_by_label(self, label: str) -> list[Issue]:
        data = self._call(
            "GET",
            "/issues",
            f"listing open '{label}' issues",
            params={"labels": label, "state": "open", "per_page": 100},
        )
        return [_issue_of(item) for item in data if "pull_request" not in item]

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        """File an issue and return its number (the auto-provenance relay,
        TESTER.md — the content is agent-authored, this is just the pipe)."""
        data = self._call(
            "POST",
            "/issues",
            f"filing issue {title!r}",
            json={"title": title, "body": body, "labels": labels},
        )
        return int(data["number"])

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._call("POST", f"/issues/{number}/labels", f"labelling #{number}", json={"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        # 404 = the label wasn't there — the state we wanted
        self._call("DELETE", f"/issues/{number}/labels/{label}", f"unlabelling #{number}", ok=(404,))

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/issues/{number}/comments", f"commenting on #{number}", json={"body": body})

    # --- pull requests ---

    def open_pr_with_branch_prefix(self, prefix: str) -> PullRequest | None:
        """The dev's PR for an issue: head branch ``dev/<n>-<slug>``."""
        pulls = self._call("GET", "/pulls", "listing open PRs", params={"state": "open", "per_page": 100})
        for item in pulls:
            if (item.get("head") or {}).get("ref", "").startswith(prefix):
                return _pr_of(item)
        return None

    def get_pr(self, number: int) -> PullRequest:
        return _pr_of(self._call("GET", f"/pulls/{number}", f"reading PR #{number}"))

    def list_reviews(self, pr_number: int) -> list[Review]:
        data = self._call(
            "GET", f"/pulls/{pr_number}/reviews", f"listing reviews of PR #{pr_number}", params={"per_page": 100}
        )
        return [
            Review(
                id=item["id"],
                author=(item.get("user") or {}).get("login", ""),
                state=item.get("state", ""),
                body=item.get("body") or "",
            )
            for item in data
        ]

    def list_review_comments(self, pr_number: int) -> list[ReviewComment]:
        data = self._call(
            "GET", f"/pulls/{pr_number}/comments", f"listing review comments of PR #{pr_number}", params={"per_page": 100}
        )
        return [
            ReviewComment(
                id=item["id"],
                review_id=item.get("pull_request_review_id"),
                author=(item.get("user") or {}).get("login", ""),
                path=item.get("path", ""),
                line=item.get("line"),
                body=item.get("body") or "",
            )
            for item in data
        ]

    def request_reviewers(self, pr_number: int, reviewers: list[str]) -> list[str]:
        """Request reviews; returns the reviewers GitHub **silently dropped**.

        A 201 does not mean everyone was added. Copilot in particular is only
        granted to a *user* token whose owner holds a Copilot seat, and only
        under the ``copilot-pull-request-reviewer[bot]`` name (a granted
        request is then recorded as login ``Copilot``); any reviewer the token
        can't request is dropped without an error. So Copilot goes through the
        dedicated user token when configured, and the response's
        ``requested_reviewers`` echo is checked instead of trusted.
        """
        dropped: list[str] = []
        humans = [r for r in reviewers if r != COPILOT_REVIEWER]
        if humans:
            dropped += self._post_reviewers(pr_number, humans)
        if COPILOT_REVIEWER in reviewers:
            if self._copilot_token is None:
                dropped.append(COPILOT_REVIEWER)
            else:
                dropped += self._post_reviewers(pr_number, [COPILOT_REVIEWER], token=self._copilot_token)
        return dropped

    def _post_reviewers(self, pr_number: int, reviewers: list[str], token: str | None = None) -> list[str]:
        data = self._call(
            "POST",
            f"/pulls/{pr_number}/requested_reviewers",
            f"re-requesting review on PR #{pr_number}",
            json={"reviewers": reviewers},
            token=token,
        )
        echoed = {(user or {}).get("login", "") for user in (data or {}).get("requested_reviewers", [])}
        expected = {r: COPILOT_REQUESTED_LOGIN if r == COPILOT_REVIEWER else r for r in reviewers}
        return [r for r, login in expected.items() if login not in echoed]

    def post_review(self, pr_number: int, body: str, findings: list[dict], event: str = "COMMENT") -> None:
        """Post a review with inline findings (the reviewer App's delivery
        path, REVIEWERS.md). ``event`` is APPROVE / REQUEST_CHANGES / COMMENT —
        legal here because the reviewer App is never the PR author. GitHub
        422s the whole review when any finding's line isn't in the diff —
        fall back to a body-only review carrying the findings as text:
        degraded, but delivered (same event, so the review state survives)."""
        inline = [f for f in findings if f.get("line")]
        flat_only = [f for f in findings if not f.get("line")]
        if flat_only:  # file-level findings can't ride the inline array — body text
            body += "\n\n" + "\n\n".join(f"**{f['path']}** — {f['body']}" for f in flat_only)
        payload: dict[str, Any] = {"event": event, "body": body}
        if inline:
            payload["comments"] = [
                {"path": f["path"], "line": f["line"], "side": "RIGHT", "body": f["body"]}
                for f in inline
            ]
        try:
            self._call(
                "POST",
                f"/pulls/{pr_number}/reviews",
                f"posting review on PR #{pr_number}",
                json=payload,
            )
        except GitHubApiError:
            if not inline:
                raise
            flat = "\n\n".join(f"**{f['path']}:{f.get('line', '?')}** — {f['body']}" for f in inline)
            self._call(
                "POST",
                f"/pulls/{pr_number}/reviews",
                f"posting review on PR #{pr_number} (body-only fallback)",
                json={"event": event, "body": f"{body}\n\n{flat}"},
            )

    def repo_owner(self) -> str:
        return self._call("GET", "", "reading the repo")["owner"]["login"]

    # --- repo contents (registration preflight, C2) ---

    def read_file(self, path: str) -> str | None:
        """The file's text at the default branch's HEAD, or None when nothing
        readable lives there. 404 is an answer here, not an error — preflight
        asks about files that are allowed to be missing."""
        data = self._call("GET", f"/contents/{path}", f"reading {path}", ok=(404,))
        if (
            isinstance(data, dict)
            and data.get("encoding") == "base64"
            and isinstance(data.get("content"), str)
        ):
            return base64.b64decode(data["content"]).decode("utf-8")
        return None

    def path_exists(self, path: str) -> bool:
        """Whether anything lives at the path — a directory answers as a list,
        a file as a typed object, absence as a tolerated 404."""
        data = self._call("GET", f"/contents/{path}", f"probing {path}", ok=(404,))
        return isinstance(data, list) or (isinstance(data, dict) and bool(data.get("type")))


def slug_from_origin(repo: Path) -> str | None:
    """``owner/name`` parsed from the clone's origin URL (ssh or https)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
    )
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", out.stdout.strip())
    return match.group(1) if match else None
