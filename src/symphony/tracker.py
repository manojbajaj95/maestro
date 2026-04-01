"""Tracker backends for Linear and GitHub."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
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

    async def read_canonical_state(self, issue: Issue) -> str: ...

    async def move_to_in_progress(self, issue: Issue) -> Issue: ...

    async def move_to_in_review(self, issue: Issue) -> Issue: ...

    async def move_to_to_do(self, issue: Issue) -> Issue: ...

    async def execute_raw_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


def _normalize_linear_issue(node: Mapping[str, Any]) -> Issue:
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
        id=str(node["id"]),
        identifier=str(node["identifier"]),
        title=str(node.get("title") or ""),
        url=node.get("url"),
        description=node.get("description"),
        priority=priority,
        state=IssueStateRef(
            id=state_node.get("id"),
            name=str(state_node.get("name") or ""),
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
        self._state_id_cache: dict[str, str] | None = None
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
            "states": [self.config.states.to_do],
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
        canonical = {
            "to_do": self.config.states.to_do,
            "in_progress": self.config.states.in_progress,
            "in_review": self.config.states.in_review,
            "done": self.config.states.done,
            "blocked": self.config.states.blocked,
        }
        resolved = [canonical.get(name, name) for name in state_names]
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
                "states": resolved,
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

    async def read_canonical_state(self, issue: Issue) -> str:
        state_name = issue.state.name.lower()
        mapping = {
            (self.config.states.to_do or "").lower(): "to_do",
            (self.config.states.in_progress or "").lower(): "in_progress",
            (self.config.states.in_review or "").lower(): "in_review",
            (self.config.states.done or "").lower(): "done",
            (self.config.states.blocked or "").lower(): "blocked",
        }
        return mapping.get(state_name, "unknown")

    async def move_to_in_progress(self, issue: Issue) -> Issue:
        return await self._move_issue_to_state(issue, self.config.states.in_progress or "")

    async def move_to_in_review(self, issue: Issue) -> Issue:
        return await self._move_issue_to_state(issue, self.config.states.in_review or "")

    async def move_to_to_do(self, issue: Issue) -> Issue:
        return await self._move_issue_to_state(issue, self.config.states.to_do or "")

    async def _move_issue_to_state(self, issue: Issue, state_name: str) -> Issue:
        if not state_name:
            raise TrackerError("linear_missing_target_state")
        state_id = await self._state_id_for_name(state_name)
        mutation = """
        mutation UpdateIssueState($id: String!, $stateId: String!) {
          issueUpdate(id: $id, input: { stateId: $stateId }) {
            success
            issue {
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
        data = await self._execute(mutation, {"id": issue.id, "stateId": state_id})
        result = data.get("issueUpdate") or {}
        if not result.get("success"):
            raise TrackerError("linear_issue_update_failed")
        node = result.get("issue")
        if not isinstance(node, Mapping):
            raise TrackerError("linear_unknown_payload")
        return _normalize_linear_issue(node)

    async def _state_id_for_name(self, state_name: str) -> str:
        if self._state_id_cache is None:
            query = """
            query ProjectStates($projectSlug: String!) {
              projects(filter: { slugId: { eq: $projectSlug } }, first: 1) {
                nodes {
                  team {
                    states {
                      nodes { id name }
                    }
                  }
                }
              }
            }
            """
            data = await self._execute(query, {"projectSlug": self.config.project_slug})
            projects = data.get("projects") or {}
            nodes = projects.get("nodes") or []
            team = nodes[0].get("team") if nodes else None
            states = (team or {}).get("states") or {}
            cache: dict[str, str] = {}
            for node in states.get("nodes", []):
                name = str(node.get("name") or "").lower()
                state_id = str(node.get("id") or "")
                if name and state_id:
                    cache[name] = state_id
            self._state_id_cache = cache
        state_id = self._state_id_cache.get(state_name.lower()) if self._state_id_cache else None
        if not state_id:
            raise TrackerError(f"linear_unknown_state:{state_name}")
        return state_id

    async def execute_raw_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
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


def _normalize_github_issue(raw: Mapping[str, Any]) -> Issue:
    labels = [item.get("name", "").lower() for item in raw.get("labels", [])]
    return Issue(
        id=str(raw["number"]),
        identifier=f"#{raw['number']}",
        title=str(raw.get("title") or ""),
        url=raw.get("url"),
        description=str(raw.get("body") or ""),
        priority=None,
        state=IssueStateRef(name=str(raw.get("state") or "").lower()),
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
        if self.config.states.to_do:
            args.extend(["--label", self.config.states.to_do])
        if self.config.assignee:
            args.extend(["--assignee", self.config.assignee])
        output = await self._runner(args)
        issues = [_normalize_github_issue(item) for item in json.loads(output)]
        eligible = [issue for issue in issues if await self.read_canonical_state(issue) == "to_do"]
        eligible.sort(key=lambda item: item.created_at)
        return eligible

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
        matched: list[Issue] = []
        for item in json.loads(output):
            issue = _normalize_github_issue(item)
            if await self.read_canonical_state(issue) in wanted:
                matched.append(issue)
        return matched

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        issues: list[Issue] = []
        for issue_id in issue_ids:
            issues.append(await self._fetch_issue(issue_id))
        return issues

    async def read_canonical_state(self, issue: Issue) -> str:
        if issue.state.name == "closed":
            return "done"
        labels = {label.lower() for label in issue.labels}
        mapping = {
            (self.config.states.to_do or "").lower(): "to_do",
            (self.config.states.in_progress or "").lower(): "in_progress",
            (self.config.states.in_review or "").lower(): "in_review",
            (self.config.states.blocked or "").lower(): "blocked",
        }
        matches = [canonical for label, canonical in mapping.items() if label and label in labels]
        if len(matches) > 1:
            raise TrackerError("github_multiple_workflow_labels")
        if matches:
            return matches[0]
        return "unknown"

    async def move_to_in_progress(self, issue: Issue) -> Issue:
        return await self._move_issue_to_label(issue.id, self.config.states.in_progress or "")

    async def move_to_in_review(self, issue: Issue) -> Issue:
        return await self._move_issue_to_label(issue.id, self.config.states.in_review or "")

    async def move_to_to_do(self, issue: Issue) -> Issue:
        return await self._move_issue_to_label(issue.id, self.config.states.to_do or "")

    async def _fetch_issue(self, issue_id: str) -> Issue:
        output = await self._runner(
            [
                "issue",
                "view",
                str(issue_id),
                "--json",
                "number,title,state,labels,body,url,createdAt,updatedAt",
            ]
        )
        return _normalize_github_issue(json.loads(output))

    async def _move_issue_to_label(self, issue_id: str, target: str) -> Issue:
        if not target:
            raise TrackerError("github_missing_target_label")
        issue = await self._fetch_issue(issue_id)
        current_state = await self.read_canonical_state(issue)
        if current_state == "done":
            raise TrackerError("github_issue_already_closed")
        workflow_labels = {
            label.lower()
            for label in [
                self.config.states.to_do,
                self.config.states.in_progress,
                self.config.states.in_review,
                self.config.states.blocked,
            ]
            if label
        }
        current_labels = {
            label.lower() for label in issue.labels if label.lower() in workflow_labels
        }
        args = ["issue", "edit", str(issue_id), "--add-label", target]
        remove = sorted(current_labels - {target.lower()})
        if remove:
            args.extend(["--remove-label", ",".join(remove)])
        await self._runner(args)
        return await self._fetch_issue(issue_id)

    async def execute_raw_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise TrackerError("github_raw_query_not_supported")


def build_tracker_client(config: TrackerConfig) -> TrackerClient:
    if config.kind == "github":
        return GitHubTrackerClient(config)
    return LinearTrackerClient(config)
