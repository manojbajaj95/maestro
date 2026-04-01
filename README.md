# Symphony

Symphony is a Python 3.14 orchestration service for polling Linear or GitHub issues, cloning the repository into per-issue workspaces, running Codex in app-server mode, and exposing lightweight runtime observability.

## What It Does

Symphony runs as a local orchestrator for a repository:

- polls issues from Linear or GitHub
- clones a dedicated workspace per issue
- renders a prompt from `WORKFLOW.md`
- starts Codex in app-server mode inside that workspace
- commits, pushes, and opens a PR when a run produces changes
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

- Python 3.14
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

## GitHub Issues Quick Start

Use this quick start if you want Symphony to manage the same repository with GitHub Issues as the tracker. This repository can manage itself with a committed `WORKFLOW.md`, and the committed workflow in this repo is already configured for GitHub Issues.

### Prerequisites

Make sure all of these are true before you start:

- GitHub Issues are enabled for the repository
- GitHub CLI `gh` is installed
- `gh auth status` succeeds, or you have run `gh auth login`
- Codex is installed and `codex app-server` is available on your path
- Python 3.14 and `uv` are installed

You can verify the local tools with:

```bash
gh auth status
codex app-server --help
uv --version
python3.14 --version
```

### 1. Repo Setup

Clone the repository, enter it, and install dependencies:

```bash
git clone <repo-url>
cd <repo-name>
uv sync --group dev
```

If your repository does not already commit a workflow file, create one from the example and then update it for GitHub Issues:

```bash
cp WORKFLOW.example.md WORKFLOW.md
```

This repository already includes a committed `WORKFLOW.md`, so you can usually confirm it matches the GitHub tracker configuration instead:

```bash
sed -n '1,40p' WORKFLOW.md
```

You should see the GitHub tracker settings used by this repo:

```md
tracker:
  kind: github
  assignee: "@me"
  states:
    to_do: status:todo
    in_progress: status:in-progress
    in_review: status:in-review
    blocked: status:blocked
workspace:
  root: ~/symphony
codex:
  command: codex app-server
server:
  port: 8080
```

### 2. GitHub Issue Setup

Create or confirm the workflow labels that Symphony uses to move issues through the state machine:

```bash
gh label create status:todo --color 1d76db --description "Ready for Symphony pickup"
gh label create status:in-progress --color fbca04 --description "Claimed by Symphony"
gh label create status:in-review --color 5319e7 --description "Waiting for human review"
gh label create status:blocked --color d73a4a --description "Excluded from Symphony polling"
```

If the labels already exist, confirm them instead:

```bash
gh label list | rg '^status:'
```

Because the committed workflow uses `assignee: "@me"`, issues must be assigned to the current GitHub user before Symphony will pick them up. A good first test issue looks like this:

- a small task
- labeled `status:todo`
- assigned to the current user

You can create one with:

```bash
gh issue create --title "docs: test Symphony with GitHub issues" --body "Small README-only test task." --label status:todo --assignee @me
```

### 3. Workflow Setup

Keep `WORKFLOW.md` in the repository root so the repository can manage itself. For a first run, the committed GitHub workflow in this repo is the right baseline:

```md
---
tracker:
  kind: github
  assignee: "@me"
  states:
    to_do: status:todo
    in_progress: status:in-progress
    in_review: status:in-review
    blocked: status:blocked
workspace:
  root: ~/symphony
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
```

Choose the workspace root carefully. `workspace.root` is where Symphony creates per-issue clones such as `~/symphony/<repo>/<issue>`. Change it in `WORKFLOW.md` if you want those workspaces somewhere else.

### 4. Running Symphony

Start Symphony from the repository root with the committed workflow:

```bash
uv run symphony ./WORKFLOW.md
```

The committed workflow serves the local dashboard on port `8080`, so open:

```text
http://127.0.0.1:8080
```

### 5. Verifying It Is Working

After startup, verify the tracker and dashboard are behaving as expected:

1. Confirm Symphony starts without `gh` or workflow configuration errors.
2. Open `http://127.0.0.1:8080` and check that the dashboard loads.
3. Confirm your test issue is small, labeled `status:todo`, and assigned to you.
4. Make sure the issue does not carry `status:blocked`.
5. Watch for Symphony to create a per-issue workspace under your configured `workspace.root`.

You can also verify the HTTP endpoints directly:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/api/v1/state
```

If Symphony does not pick up the issue, the most common cause is that one of these does not match `WORKFLOW.md`: the workflow-state label, the assignee, or the workspace configuration.

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
  states:
    to_do: Todo
    in_progress: In Progress
    in_review: In Review
    done: Done
    blocked: Blocked
workspace:
  root: ~/symphony
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
  assignee: "@me"
  states:
    to_do: status:todo
    in_progress: status:in-progress
    in_review: status:in-review
    blocked: status:blocked
workspace:
  root: ~/symphony
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

Issue details:
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

- polls issues in the configured `to_do` state for the configured project
- moves issues between configured Linear workflow states
- refreshes issue state by Linear issue id
- exposes the optional `linear_graphql` tool to Codex sessions

### GitHub

- polls issues via `gh issue list`
- treats workflow state as labels on open issues
- only picks issues labeled with the configured `to_do` state and assigned correctly
- moves issues through `to_do -> in_progress -> in_review`
- refreshes issue state via `gh issue view`
- does not expose `linear_graphql`

## Workspace Behavior

Each issue gets its own deterministic workspace directory under:

`~/symphony/<project-name>/<issue>`

Symphony will:

- clone the repository into the issue directory if it does not exist
- switch to an issue branch before Codex starts
- run `uv sync --group dev` before work in Python projects
- commit and push branch changes after a successful run
- create or reuse a GitHub pull request for the issue branch
- reuse the existing clone if it already exists
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
