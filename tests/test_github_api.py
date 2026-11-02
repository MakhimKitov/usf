"""The scheduler's GitHub surface, against a faked requests transport."""

import pytest
import requests
from control_plane.github_api import (
    COPILOT_REVIEWER,
    GitHubApi,
    GitHubApiError,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class FakeTransport:
    """Records calls; replies from a {(method, path): response} table."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, *, headers=None, params=None, json=None, timeout=None):
        path = url.split("api.github.com", 1)[1]
        self.calls.append({"method": method, "path": path, "params": params, "json": json, "headers": headers})
        return self.routes.get((method, path), FakeResponse(200, payload=None))


def _api(routes):
    transport = FakeTransport(routes)
    return GitHubApi("owner/product", "ghs_test", request=transport), transport


def test_list_ready_issues_filters_prs_and_parses_labels():
    api, transport = _api(
        {
            ("GET", "/repos/owner/product/issues"): FakeResponse(
                200,
                payload=[
                    {"number": 7, "title": "fix it", "body": "text", "labels": [{"name": "ready"}, {"name": "human"}]},
                    {"number": 8, "title": "a pr", "labels": [], "pull_request": {"url": "x"}},
                    {"number": 9, "title": "auto one", "body": None, "labels": [{"name": "ready"}]},
                ],
            )
        }
    )
    issues = api.list_ready_issues()
    assert [issue.number for issue in issues] == [7, 9]
    assert issues[0].provenance == "human"
    assert issues[1].provenance == "auto"  # unlabelled = tainted
    assert issues[1].body == ""
    assert transport.calls[0]["params"]["labels"] == "ready"


def test_label_ops_and_tolerated_404():
    api, transport = _api(
        {("DELETE", "/repos/owner/product/issues/7/labels/ready"): FakeResponse(404, text="not there")}
    )
    api.remove_label(7, "ready")  # 404 = already gone — fine
    api.add_labels(7, ["in-progress"])
    assert transport.calls[1]["json"] == {"labels": ["in-progress"]}


def test_errors_raise_with_context():
    api, _ = _api({("POST", "/repos/owner/product/issues/7/comments"): FakeResponse(403, text="forbidden")})
    with pytest.raises(GitHubApiError, match="commenting on #7.*403"):
        api.comment(7, "hello")


def test_no_response_at_all_is_the_same_error_as_a_bad_one():
    """E6: a name-resolution blip surfaced as a raw requests.ConnectionError out
    of a tick and took a replica down. No response is a GitHubApiError like any
    other — the loop logs it and retries next tick."""

    def transport(method, url, **kwargs):
        raise requests.ConnectionError("Failed to resolve 'api.github.com'")

    api = GitHubApi("owner/product", "ghs_test", request=transport)
    with pytest.raises(GitHubApiError, match="listing ready issues: ConnectionError: .*resolve") as caught:
        api.list_ready_issues()
    assert isinstance(caught.value.__cause__, requests.ConnectionError)


def test_open_pr_matched_by_branch_prefix():
    api, _ = _api(
        {
            ("GET", "/repos/owner/product/pulls"): FakeResponse(
                200,
                payload=[
                    {"number": 2, "state": "open", "head": {"ref": "dev/1-coinflip", "sha": "abc"}},
                    {"number": 3, "state": "open", "head": {"ref": "dev/12-other", "sha": "def"}},
                ],
            )
        }
    )
    pr = api.open_pr_with_branch_prefix("dev/1-")
    assert pr is not None and pr.number == 2  # not the dev/12- one
    assert api.open_pr_with_branch_prefix("dev/99-") is None


def test_reviews_parse():
    api, _ = _api(
        {
            ("GET", "/repos/owner/product/pulls/2/reviews"): FakeResponse(
                200,
                payload=[
                    {"id": 10, "user": {"login": "MakhimKitov"}, "state": "CHANGES_REQUESTED", "body": "clown it"},
                    {"id": 11, "user": {"login": COPILOT_REVIEWER}, "state": "COMMENTED", "body": None},
                ],
            )
        }
    )
    reviews = api.list_reviews(2)
    assert reviews[0].author == "MakhimKitov" and reviews[0].state == "CHANGES_REQUESTED"
    assert reviews[1].body == ""


def test_request_reviewers_without_user_token_drops_copilot():
    api, transport = _api(
        {
            ("POST", "/repos/owner/product/pulls/2/requested_reviewers"): FakeResponse(
                201, payload={"requested_reviewers": [{"login": "MakhimKitov"}]}
            )
        }
    )
    dropped = api.request_reviewers(2, [COPILOT_REVIEWER, "MakhimKitov"])
    assert dropped == [COPILOT_REVIEWER]
    # Copilot is never sent on the App token — GitHub would 201 and drop it anyway
    posts = [c for c in transport.calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["json"] == {"reviewers": ["MakhimKitov"]}


def test_request_reviewers_routes_copilot_through_user_token():
    transport = FakeTransport(
        {
            ("POST", "/repos/owner/product/pulls/2/requested_reviewers"): FakeResponse(
                201,
                payload={"requested_reviewers": [{"login": "MakhimKitov"}, {"login": "Copilot"}]},
            )
        }
    )
    api = GitHubApi("owner/product", "ghs_app", request=transport, copilot_token="ghp_user")
    dropped = api.request_reviewers(2, [COPILOT_REVIEWER, "MakhimKitov"])
    assert dropped == []
    posts = [c for c in transport.calls if c["method"] == "POST"]
    assert [p["json"]["reviewers"] for p in posts] == [["MakhimKitov"], [COPILOT_REVIEWER]]
    assert posts[0]["headers"]["Authorization"] == "Bearer ghs_app"
    assert posts[1]["headers"]["Authorization"] == "Bearer ghp_user"


def test_request_reviewers_detects_silent_drop():
    # 201 whose echo is missing the reviewer = GitHub dropped it (the PR #6 lesson:
    # a granted Copilot request echoes back as login "Copilot")
    transport = FakeTransport(
        {
            ("POST", "/repos/owner/product/pulls/2/requested_reviewers"): FakeResponse(
                201, payload={"requested_reviewers": [{"login": "MakhimKitov"}]}
            )
        }
    )
    api = GitHubApi("owner/product", "ghs_app", request=transport, copilot_token="ghp_user")
    assert api.request_reviewers(2, [COPILOT_REVIEWER]) == [COPILOT_REVIEWER]


def test_token_supplier_is_consulted_per_call():
    tokens = iter(["ghs_first", "ghs_second"])
    transport = FakeTransport({})
    api = GitHubApi("owner/product", lambda: next(tokens), request=transport)
    api.comment(1, "one")
    api.comment(2, "two")
    auths = [call["headers"]["Authorization"] for call in transport.calls]
    assert auths == ["Bearer ghs_first", "Bearer ghs_second"]


def test_post_review_inline_findings():
    transport = FakeTransport(
        {("POST", "/repos/owner/product/pulls/2/reviews"): FakeResponse(200, payload={"id": 1})}
    )
    api = GitHubApi("owner/product", "ghs_reviewer", request=transport)
    api.post_review(
        2,
        "Sound change.",
        [
            {"path": "bot/x.py", "line": 3, "body": "off-by-one"},
            {"path": "specs/a.md", "line": None, "body": "file-level note"},
        ],
    )
    (post,) = [c for c in transport.calls if c["method"] == "POST"]
    assert post["json"]["event"] == "COMMENT"
    # line-anchored finding rides inline; file-level folds into the body
    assert post["json"]["comments"] == [
        {"path": "bot/x.py", "line": 3, "side": "RIGHT", "body": "off-by-one"}
    ]
    assert "file-level note" in post["json"]["body"]


def test_post_review_falls_back_to_body_only_on_422():
    calls = {"n": 0}

    class FlakyTransport(FakeTransport):
        def __call__(self, method, url, **kwargs):
            response = super().__call__(method, url, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(422, text="line not in diff")
            return FakeResponse(200, payload={"id": 2})

    transport = FlakyTransport({})
    api = GitHubApi("owner/product", "ghs_reviewer", request=transport)
    api.post_review(
        2,
        "verdict",
        [{"path": "bot/x.py", "line": 999, "body": "stale line"}],
        event="REQUEST_CHANGES",
    )
    assert len(transport.calls) == 2
    retry = transport.calls[1]["json"]
    assert "comments" not in retry  # degraded but delivered
    assert "bot/x.py:999" in retry["body"] and "stale line" in retry["body"]
    # the review *state* must survive the degradation — it's the round trigger
    assert retry["event"] == "REQUEST_CHANGES"


def test_create_issue_posts_and_returns_number():
    transport = FakeTransport(
        {("POST", "/repos/owner/product/issues"): FakeResponse(201, payload={"number": 13})}
    )
    api = GitHubApi("owner/product", "ghs_reviewer", request=transport)
    number = api.create_issue("symptom title", "### Summary\nbody", ["auto"])
    assert number == 13
    (post,) = transport.calls
    assert post["json"] == {"title": "symptom title", "body": "### Summary\nbody", "labels": ["auto"]}


def test_list_open_issues_by_label_filters_prs():
    transport = FakeTransport(
        {
            ("GET", "/repos/owner/product/issues"): FakeResponse(
                200,
                payload=[
                    {"number": 1, "title": "real issue", "body": "", "labels": [{"name": "auto"}]},
                    {"number": 2, "title": "a PR", "body": "", "labels": [], "pull_request": {}},
                ],
            )
        }
    )
    api = GitHubApi("owner/product", "ghs_reviewer", request=transport)
    issues = api.list_open_issues_by_label("auto")
    assert [i.number for i in issues] == [1]
    assert transport.calls[0]["params"]["labels"] == "auto"


def test_read_file_decodes_the_contents_answer():
    """The contents API answers base64 — `read_file` hands back text (C2)."""
    import base64

    payload = {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode("---\nenv: [DISCORD_TOKEN]\n---\n".encode()).decode(),
    }
    api, _ = _api({("GET", "/repos/owner/product/contents/TESTING.md"): FakeResponse(200, payload=payload)})
    assert api.read_file("TESTING.md") == "---\nenv: [DISCORD_TOKEN]\n---\n"


def test_read_file_answers_none_for_absence_and_directories():
    """404 is an answer, not an error — preflight asks about files that are
    allowed to be missing. A directory is not a readable file either."""
    api, _ = _api(
        {
            ("GET", "/repos/owner/product/contents/CLAUDE.md"): FakeResponse(
                404, payload={"message": "Not Found"}, text="not found"
            ),
            ("GET", "/repos/owner/product/contents/.github"): FakeResponse(
                200, payload=[{"type": "file", "name": "PULL_REQUEST_TEMPLATE.md"}]
            ),
        }
    )
    assert api.read_file("CLAUDE.md") is None
    assert api.read_file(".github") is None


def test_path_exists_sees_files_directories_and_absence():
    api, _ = _api(
        {
            ("GET", "/repos/owner/product/contents/.commitlintrc.json"): FakeResponse(
                200, payload={"type": "file", "encoding": "base64", "content": ""}
            ),
            ("GET", "/repos/owner/product/contents/.github/ISSUE_TEMPLATE"): FakeResponse(
                200, payload=[{"type": "file", "name": "issue.yml"}]
            ),
            ("GET", "/repos/owner/product/contents/CLAUDE.md"): FakeResponse(
                404, payload={"message": "Not Found"}, text="not found"
            ),
        }
    )
    assert api.path_exists(".commitlintrc.json") is True
    assert api.path_exists(".github/ISSUE_TEMPLATE") is True
    assert api.path_exists("CLAUDE.md") is False


def test_contents_errors_other_than_404_still_raise():
    api, _ = _api(
        {("GET", "/repos/owner/product/contents/TESTING.md"): FakeResponse(503, text="down")}
    )
    with pytest.raises(GitHubApiError, match="reading TESTING.md.*503"):
        api.read_file("TESTING.md")


# --- the GitHub call as a span inside a turn ---


def test_a_call_inside_a_turn_is_a_span_that_names_the_call_and_never_the_token(spans):
    from control_plane.telemetry import turn_span

    api, _ = _api({("GET", "/repos/owner/product/issues/7"): FakeResponse(200, payload={"number": 7, "title": "t", "body": "", "labels": [], "state": "open"})})
    api.get_issue(7)  # outside a turn: the poll's shape, untraced
    assert spans.get_finished_spans() == ()

    with turn_span("owner/product#7", "work"):
        api.get_issue(7)
    (call,) = [span for span in spans.get_finished_spans() if span.name == "github.request"]
    assert call.attributes["github.operation"] == "reading issue #7"
    assert call.attributes["http.request.method"] == "GET"
    assert call.attributes["url.path"] == "/repos/owner/product/issues/7"
    assert call.attributes["http.response.status_code"] == 200
    assert "ghs_test" not in repr(dict(call.attributes))


def test_a_refused_call_marks_its_span(spans):
    from control_plane.telemetry import turn_span
    from opentelemetry.trace import StatusCode

    api, _ = _api({("GET", "/repos/owner/product/issues/7"): FakeResponse(500, text="boom")})
    with pytest.raises(GitHubApiError):
        with turn_span("owner/product#7", "work"):
            api.get_issue(7)
    (call,) = [span for span in spans.get_finished_spans() if span.name == "github.request"]
    assert call.status.status_code == StatusCode.ERROR
    assert call.attributes["http.response.status_code"] == 500
