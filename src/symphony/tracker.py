"""Tracker backends for Linear and GitHub."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .errors import TrackerError
from .models import Issue, IssueStateRef, TrackerConfig


def _parse_datetime(raw: str | None) -> datetime:
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class TrackerClient(Protocol):
    config: TrackerConfig

    async def fetch_candidate_issues(self) -> list[Issue]: ...

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]: ...

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]: ...

    async def execute_raw_query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]: ...


def _normalize_linear_issue(node: dict[str, Any]) -> Issue:
    labels = [item.get("name", "") for item in node.get("labels", {}).get("nodes", [])]
    blocked_by: list[str] = []
    for relation in node.get("inverseRelations", {}).get("nodes", []):
        if relation.get("type") == "blocks" and relation.get("sourceIssue"):
            blocked_by.append(relation["sourceIssue"]["identifier"])
    priority = node.get("priority")
    if not isinstance(priority, int):
        priority = None
    state_node = node.get("state") or {}
    return Issue(
        id=node["id"],
        identifier=node["identifier"],
        title=node.get("title") or "",
        url=node.get("url"),
        description=node.get("description"),
        priority=priority,
        state=IssueStateRef(
            id=state_node.get("id"),
            name=state_node.get("name") or "",
            type=state_node.get("type"),
        ),
        labels=labels,
        blocked_by=blocked_by,
        created_at=_parse_datetime(node.get("createdAt")),
        updated_at=_parse_datetime(node.get("updatedAt")),
    )


class LinearTrackerClient:
    def __init__(self, config: TrackerConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = http_client or httpx.AsyncClient(
            timeout=self.config.timeout_ms / 1000,
            headers={"Authorization": self.config.api_key or ""},
        )

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self.config.endpoint,
                json={"query": query, "variables": variables},
            )
        except httpx.HTTPError as exc:
            raise TrackerError("linear_api_request") from exc
        if response.status_code != 200:
            raise TrackerError(f"linear_api_status:{response.status_code}")
        payload = response.json()
        if payload.get("errors"):
            raise TrackerError("linear_graphql_errors")
        if "data" not in payload:
            raise TrackerError("linear_unknown_payload")
        return payload["data"]

    async def fetch_candidate_issues(self) -> list[Issue]:
        query = """
        query Candidates($projectSlug: String!, $states: [String!], $first: Int!, $after: String) {
          issues(
            filter: {
              project: { slugId: { eq: $projectSlug } }
              state: { name: { in: $states } }
            }
            first: $first
            after: $after
          ) {
            nodes {
              id
              identifier
              title
              url
              description
              priority
              createdAt
              updatedAt
              state { id name type }
              labels { nodes { name } }
              inverseRelations { nodes { type sourceIssue { identifier } } }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        variables = {
            "projectSlug": self.config.project_slug,
            "states": self.config.active_states,
            "first": self.config.page_size,
            "after": None,
        }
        issues: list[Issue] = []
        while True:
            data = await self._execute(query, variables)
            raw = data.get("issues")
            if not isinstance(raw, dict):
                raise TrackerError("linear_unknown_payload")
            issues.extend(_normalize_linear_issue(node) for node in raw.get("nodes", []))
            page_info = raw.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return issues
            end_cursor = page_info.get("endCursor")
            if not end_cursor:
                raise TrackerError("linear_missing_end_cursor")
            variables["after"] = end_cursor

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if not state_names:
            return []
        query = """
        query IssuesByState($projectSlug: String!, $states: [String!], $first: Int!) {
          issues(
            filter: {
              project: { slugId: { eq: $projectSlug } }
              state: { name: { in: $states } }
            }
            first: $first
          ) {
            nodes {
              id
              identifier
              title
              url
              description
              priority
              createdAt
              updatedAt
              state { id name type }
              labels { nodes { name } }
              inverseRelations { nodes { type sourceIssue { identifier } } }
            }
          }
        }
        """
        data = await self._execute(
            query,
            {
                "projectSlug": self.config.project_slug,
                "states": state_names,
                "first": self.config.page_size,
            },
        )
        raw = data.get("issues", {})
        return [_normalize_linear_issue(node) for node in raw.get("nodes", [])]

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if not issue_ids:
            return []
        query = """
        query IssueStates($ids: [ID!]) {
          issues(filter: { id: { in: $ids } }) {
            nodes {
              id
              identifier
              title
              url
              description
              priority
              createdAt
              updatedAt
              state { id name type }
              labels { nodes { name } }
              inverseRelations { nodes { type sourceIssue { identifier } } }
            }
          }
        }
        """
        data = await self._execute(query, {"ids": issue_ids})
        raw = data.get("issues", {})
        return [_normalize_linear_issue(node) for node in raw.get("nodes", [])]

    async def execute_raw_query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        text = query.strip()
        if not text:
            raise TrackerError("invalid_graphql_query")
        if text.count("query ") + text.count("mutation ") > 1:
            raise TrackerError("multiple_graphql_operations")
        return await self._execute(text, variables or {})


async def run_gh(args: Sequence[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TrackerError(f"gh_command_failed:{' '.join(args)}:{stderr.decode().strip()}")
    return stdout.decode()


def _normalize_github_issue(raw: dict[str, Any]) -> Issue:
    labels = [item.get("name", "").lower() for item in raw.get("labels", [])]
    return Issue(
        id=str(raw["number"]),
        identifier=f"#{raw['number']}",
        title=raw.get("title") or "",
        url=raw.get("url"),
        description=raw.get("body") or "",
        priority=None,
        state=IssueStateRef(name=(raw.get("state") or "").lower()),
        labels=labels,
        blocked_by=[],
        created_at=_parse_datetime(raw.get("createdAt")),
        updated_at=_parse_datetime(raw.get("updatedAt")),
    )


class GitHubTrackerClient:
    def __init__(self, config: TrackerConfig, runner=run_gh) -> None:
        self.config = config
        self._runner = runner

    async def fetch_candidate_issues(self) -> list[Issue]:
        args = [
            "issue",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,state,labels,body,url,createdAt,updatedAt",
            "--limit",
            str(self.config.page_size),
        ]
        for label in self.config.labels:
            args.extend(["--label", label])
        if self.config.assignee:
            args.extend(["--assignee", self.config.assignee])
        output = await self._runner(args)
        issues = [_normalize_github_issue(item) for item in json.loads(output)]
        if self.config.exclude_labels:
            excluded = {label.lower() for label in self.config.exclude_labels}
            issues = [issue for issue in issues if not excluded.intersection(issue.labels)]
        issues.sort(key=lambda item: item.created_at)
        return issues

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if not state_names:
            return []
        wanted = {name.lower() for name in state_names}
        output = await self._runner(
            [
                "issue",
                "list",
                "--state",
                "all",
                "--json",
                "number,title,state,labels,body,url,createdAt,updatedAt",
                "--limit",
                str(self.config.page_size),
            ]
        )
        return [issue for issue in (_normalize_github_issue(item) for item in json.loads(output)) if issue.state.name in wanted]

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        issues: list[Issue] = []
        for issue_id in issue_ids:
            output = await self._runner(
                [
                    "issue",
                    "view",
                    str(issue_id),
                    "--json",
                    "number,title,state,labels,body,url,createdAt,updatedAt",
                ]
            )
            issues.append(_normalize_github_issue(json.loads(output)))
        return issues

    async def execute_raw_query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        raise TrackerError("github_raw_query_not_supported")


def build_tracker_client(config: TrackerConfig) -> TrackerClient:
    if config.kind == "github":
        return GitHubTrackerClient(config)
    return LinearTrackerClient(config)
