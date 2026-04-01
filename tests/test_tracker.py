from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from symphony.errors import TrackerError
from symphony.models import Issue, IssueStateRef, TrackerConfig
from symphony.tracker import GitHubTrackerClient, LinearTrackerClient


def make_transport(payloads: list[dict]) -> httpx.MockTransport:
    remaining = payloads.copy()

    def handler(request: httpx.Request) -> httpx.Response:
        body = remaining.pop(0)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_candidate_issues_paginates_and_normalizes() -> None:
    client = httpx.AsyncClient(
        transport=make_transport(
            [
                {
                    "data": {
                        "issues": {
                            "nodes": [
                                {
                                    "id": "1",
                                    "identifier": "ABC-1",
                                    "title": "One",
                                    "priority": 1,
                                    "createdAt": "2026-01-01T00:00:00Z",
                                    "updatedAt": "2026-01-01T00:00:00Z",
                                    "state": {"name": "Todo"},
                                    "labels": {"nodes": [{"name": "Bug"}]},
                                    "inverseRelations": {
                                        "nodes": [
                                            {
                                                "type": "blocks",
                                                "sourceIssue": {"identifier": "ABC-0"},
                                            }
                                        ]
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                },
                {
                    "data": {
                        "issues": {
                            "nodes": [
                                {
                                    "id": "2",
                                    "identifier": "ABC-2",
                                    "title": "Two",
                                    "priority": 2,
                                    "createdAt": "2026-01-02T00:00:00Z",
                                    "updatedAt": "2026-01-02T00:00:00Z",
                                    "state": {"name": "Todo"},
                                    "labels": {"nodes": []},
                                    "inverseRelations": {"nodes": []},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            ]
        )
    )
    tracker = LinearTrackerClient(
        TrackerConfig(kind="linear", api_key="token", project_slug="proj"),
        http_client=client,
    )
    issues = await tracker.fetch_candidate_issues()
    assert [issue.identifier for issue in issues] == ["ABC-1", "ABC-2"]
    assert issues[0].labels == ["bug"]
    assert issues[0].blocked_by == ["ABC-0"]
    assert issues[0].created_at == datetime(2026, 1, 1, 0, 0, tzinfo=issues[0].created_at.tzinfo)


@pytest.mark.asyncio
async def test_fetch_issues_by_states_empty_skips_api() -> None:
    tracker = LinearTrackerClient(
        TrackerConfig(kind="linear", api_key="token", project_slug="proj"),
        http_client=httpx.AsyncClient(transport=make_transport([])),
    )
    issues = await tracker.fetch_issues_by_states([])
    assert issues == []


@pytest.mark.asyncio
async def test_github_tracker_filters_and_normalizes() -> None:
    async def fake_run(args: list[str]) -> str:
        assert "--label" in args
        assert "status:todo" in args
        return """
        [
          {
            "number": 2,
            "title": "Two",
            "state": "OPEN",
            "labels": [{"name": "status:todo"}],
            "body": "hello",
            "url": "https://example.com/2",
            "createdAt": "2026-01-02T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z"
          },
          {
            "number": 1,
            "title": "One",
            "state": "OPEN",
            "labels": [{"name": "status:blocked"}],
            "body": "skip me",
            "url": "https://example.com/1",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z"
          }
        ]
        """

    tracker = GitHubTrackerClient(
        TrackerConfig(kind="github"),
        runner=fake_run,
    )
    issues = await tracker.fetch_candidate_issues()
    assert [issue.identifier for issue in issues] == ["#2"]
    assert issues[0].state.name == "open"


@pytest.mark.asyncio
async def test_github_tracker_moves_between_state_labels() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["issue", "view"]:
            return """
            {
              "number": 2,
              "title": "Two",
              "state": "OPEN",
              "labels": [{"name": "status:todo"}],
              "body": "hello",
              "url": "https://example.com/2",
              "createdAt": "2026-01-02T00:00:00Z",
              "updatedAt": "2026-01-02T00:00:00Z"
            }
            """
        return ""

    tracker = GitHubTrackerClient(TrackerConfig(kind="github"), runner=fake_run)
    issue = await tracker.fetch_issue_states_by_ids(["2"])

    await tracker.move_to_in_progress(issue[0])

    assert any(
        call[:3] == ["issue", "edit", "2"]
        and "--add-label" in call
        and "status:in-progress" in call
        and "--remove-label" in call
        and "status:todo" in call
        for call in calls
    )


@pytest.mark.asyncio
async def test_github_tracker_detects_invalid_multiple_state_labels() -> None:
    tracker = GitHubTrackerClient(TrackerConfig(kind="github"), runner=lambda args: "")
    raw_issue = Issue(
        id="1",
        identifier="#1",
        title="Broken",
        state=IssueStateRef(name="open"),
        labels=["status:todo", "status:in-progress"],
        blocked_by=[],
        created_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        updated_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
    )
    with pytest.raises(TrackerError):
        await tracker.read_canonical_state(raw_issue)
