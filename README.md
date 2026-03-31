# Symphony

Symphony is a Python 3.13 orchestration service for polling Linear or GitHub issues, creating per-issue workspaces, running Codex in app-server mode, and exposing lightweight runtime observability.

## What It Does

Symphony runs as a local orchestrator for a repository:

- polls issues from Linear or GitHub
- creates a dedicated workspace per issue
- renders a prompt from `WORKFLOW.md`
- starts Codex in app-server mode inside that workspace
- retries or reconciles runs as issue state changes
- exposes `/healthz`, `/api/v1/state`, and a lightweight status dashboard

## Tooling

This repository uses `uv` for everything:

- dependency management
- lockfile management
- local execution
- tests
- linting and formatting
- type checking

## Requirements

- Python 3.13
- `uv`
- Codex with `codex app-server` available on your path
- for Linear:
  - a Linear API key
  - a Linear project `slugId`
- for GitHub:
  - GitHub CLI `gh`
  - `gh auth login` already completed in the repo context

## Installation

Install the project and dev dependencies:

```bash
uv sync --group dev
```

## Configuration

Symphony reads a single `WORKFLOW.md` file.

The file contains:

- YAML front matter for runtime config
- Markdown body for the prompt template passed to Codex

Start from the example:

```bash
cp WORKFLOW.example.md WORKFLOW.md
```

### Linear Example

```md
---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: your-project
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
    - Canceled
workspace:
  root: ~/tmp/symphony-workspaces
polling:
  interval_ms: 30000
agent:
  max_concurrent_agents: 2
  max_turns: 10
codex:
  command: codex app-server
server:
  port: 8080
---
Work on issue {{ issue.identifier }}: {{ issue.title }}

Attempt number: {{ attempt }}
```

### GitHub Example

```md
---
tracker:
  kind: github
  labels: [agent]
  exclude_labels: [blocked]
  assignee: "@me"
workspace:
  root: ~/tmp/symphony-workspaces
polling:
  interval_ms: 30000
agent:
  max_concurrent_agents: 1
  max_turns: 8
codex:
  command: codex app-server
server:
  port: 8080
---
Work on issue {{ issue.identifier }}: {{ issue.title }}

Current description:
{{ issue.description }}
```

### Prompt Variables

The prompt template supports:

- `issue`
- `attempt`

Useful `issue` fields include:

- `issue.identifier`
- `issue.title`
- `issue.description`
- `issue.labels`
- `issue.state.name`
- `issue.url`

## Running Symphony

Run with the default `./WORKFLOW.md`:

```bash
uv run symphony
```

Run with an explicit workflow path:

```bash
uv run symphony ./WORKFLOW.md
```

Override the HTTP port from the CLI:

```bash
uv run symphony ./WORKFLOW.md --port 8080
```

## Observability

When `server.port` is configured, Symphony serves:

- `GET /healthz`
- `GET /api/v1/state`
- `GET /`

The dashboard is intentionally simple and server-rendered.

## Tracker Behavior

### Linear

- polls configured active states in the configured project
- refreshes issue state by Linear issue id
- exposes the optional `linear_graphql` tool to Codex sessions

### GitHub

- polls issues via `gh issue list`
- filters by labels, excluded labels, and assignee
- refreshes issue state via `gh issue view`
- does not expose `linear_graphql`

## Workspace Behavior

Each issue gets its own deterministic workspace directory under `workspace.root`.

Symphony will:

- create the directory if it does not exist
- reuse it if it already exists
- run configured hooks
- clean up terminal issue workspaces

## Quality Checks

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pre-commit run --all-files
```

## Project Layout

- `src/symphony/`: runtime modules for config loading, tracker integration, workspace management, app-server handling, orchestration, and observability
- `tests/`: conformance-oriented tests for core Symphony behavior
- `AGENTS.md`: agent-facing repo guidance

## Tracker Support

Supported trackers:

- `linear`
- `github`

Linear supports the optional `linear_graphql` tool exposed to Codex sessions.
GitHub uses the `gh` CLI for issue polling and issue-state refresh.

## Skills

The requested Codex skills for Python repos are:

- `ruff`
- `uv`
- `ty`
- `uv-trusted-publish-github-action`
- `release-please-changelog`

Restart Codex after installation so newly added skills are available to agents.
