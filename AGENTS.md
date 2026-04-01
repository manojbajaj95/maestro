# Symphony Agent Guide

This repository implements Symphony as a Python 3.14 `uv`-managed orchestration tool.

The repo is intentionally small and operational. Most bugs here are not isolated logic bugs. They are lifecycle bugs: issue state, workspace state, app-server state, git state, or tracker state getting out of sync. Agents working in this repo should think in terms of end-to-end flows, not single functions.

## What This Repo Does

Symphony is a bridge between:

- a tracker
  GitHub Issues or Linear
- a local per-issue workspace
- Codex running in `app-server` mode
- git and PR publication
- a lightweight runtime dashboard

At a high level, the service does this:

1. Poll the tracker for issues in the canonical `to_do` state.
2. Claim the issue by moving it to `in_progress`.
3. Prepare a deterministic workspace under `~/symphony/<project>/<issue>`.
4. Clone the repo, check out the issue branch, and run `uv sync --group dev`.
5. Render the `WORKFLOW.md` prompt and run Codex via the app-server protocol.
6. If Codex succeeds:
   - publish changes through git and GitHub when files changed
   - move the tracker state to `in_review`
7. If Codex fails:
   - move the tracker state back to `to_do`
   - leave structured logs for human follow-up

The service is designed around a human review loop. Symphony should prepare work and hand off cleanly. It should not merge PRs or silently complete the whole software lifecycle without a human checkpoint.

## Core Concepts

### Canonical Workflow States

Internally Symphony thinks in these canonical states:

- `to_do`
- `in_progress`
- `in_review`
- `done`
- `blocked`

Tracker-specific encoding:

- Linear uses configured native workflow state names.
- GitHub keeps issues open and uses workflow labels:
  - `status:todo`
  - `status:in-progress`
  - `status:in-review`
  - `status:blocked`
  - closed issue means `done`

The agent only picks up `to_do`.

### Source of Truth

The tracker is the durable source of truth for workflow state.

The orchestrator has in-memory runtime state for observability:

- currently running issues
- claimed issues for this process
- recent errors
- aggregate Codex usage

That runtime state is useful for the dashboard, but it is not the workflow authority. If the process restarts, the tracker state must still prevent duplicate work.

### Per-Issue Workspaces

Every issue gets a dedicated workspace clone:

- root: `~/symphony/<project>/<issue>`
- branch: `codex/<issue-slug>`

The source repo is cloned from the local checkout, but the workspace `origin` is rewritten to the real upstream remote so pushes and PR creation work correctly.

These workspaces are not scratch directories. They are the real execution environment for Codex, validation, commit, push, and PR creation.

## How It Is Implemented

The important modules are:

- `src/symphony/workflow.py`
  Loads `WORKFLOW.md`, resolves env vars, and builds the typed service config.
- `src/symphony/models.py`
  Shared typed models and config, including tracker state mapping.
- `src/symphony/tracker.py`
  Tracker adapters for GitHub and Linear, including workflow-state reads and transitions.
- `src/symphony/workspace.py`
  Per-issue workspace preparation, branch setup, `uv sync`, and publish flow.
- `src/symphony/app_server.py`
  Codex app-server protocol client.
- `src/symphony/runner.py`
  Connects prompt rendering, workspace prep, and Codex execution for one issue.
- `src/symphony/orchestrator.py`
  Poll loop, dispatch, claim logic, rollback, reconciliation, and runtime state.
- `src/symphony/server.py`
  Lightweight HTTP observability surface.
- `src/symphony/service.py`
  Wires the service together.
- `src/symphony/cli.py`
  CLI entrypoint.

The clean mental model is:

- `tracker.py` owns durable workflow state
- `workspace.py` owns filesystem and git state
- `app_server.py` owns Codex protocol state
- `orchestrator.py` owns scheduling and process-local runtime state

If a change crosses those boundaries, be explicit about which layer owns the invariant.

## Debugging Guide

Start with the runtime surface, not the code.

### First Checks

Use these in this order:

```bash
tail -n 200 symphony.log
curl -s http://127.0.0.1:8080/api/v1/state
ps -o pid,ppid,etime,stat,command -ax | rg 'uv run symphony|(^| )codex app-server$'
```

These tell you:

- whether Symphony is running
- which issue is active
- whether the worker exited normally or failed
- whether there are stale app-server processes

### Codex Session Logs

When the tracker and dashboard say an issue is running but you need the fine-grained reason, inspect the latest session log:

```bash
ls -1t ~/.codex/sessions/*/*/*/*.jsonl | head
tail -n 200 <session-log>
```

That is usually where you find the real hang or failure point:

- app-server protocol mismatch
- long-running validation
- unexpected git state
- prompt misunderstanding
- publish step failure

### Workspace Inspection

The issue workspace is the real execution environment. Check it directly:

```bash
git -C ~/symphony/<project>/<issue> status --short
git -C ~/symphony/<project>/<issue> branch --show-current
git -C ~/symphony/<project>/<issue> remote -v
```

When debugging publication problems, always verify:

- branch name is `codex/<issue>`
- `origin` points to the real GitHub remote, not the local repo path
- changed files are actually present in the workspace

### Common Failure Classes

- Tracker/state bugs
  Symptoms: issue keeps getting re-picked, wrong workflow label/state, bad restart behavior.
- Workspace bugs
  Symptoms: wrong directory, nested clone, wrong branch, missing `.git`, wrong remote.
- App-server bugs
  Symptoms: `turn_timeout`, protocol errors, zero token usage, stuck sessions.
- Publish bugs
  Symptoms: commit exists locally but no push, no PR, PR on wrong branch, `gh` remote errors.
- Prompt/behavior bugs
  Symptoms: Codex does the wrong work, reruns broad validation, touches unrelated files.

## Expected Commands

Use `uv` for everything:

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pre-commit run --all-files
```

Do not introduce parallel toolchains or one-off package manager flows unless there is a concrete reason and the repo is updated consistently.

## Philosophy

### Human Review Is Required

This repo is intentionally built around a human-in-the-loop review flow. The correct outcome is usually:

- agent prepares work
- agent publishes branch and PR when needed
- tracker moves to `in_review`
- human decides what happens next

Do not optimize this into silent full automation unless the requirements explicitly change.

### Tracker State Must Be Durable

Do not solve lifecycle problems only in memory.

If the goal is “don’t re-run this issue after restart,” the answer should usually be a tracker state transition, not just an orchestrator set or local cache.

### Prefer State Machine Thinking

When making changes, think in transitions and ownership:

- who can move `to_do -> in_progress`
- who can move `in_progress -> in_review`
- what happens on failure
- what is durable after restart

If a change weakens those answers, it is probably the wrong change.

### Be Conservative With User Work

Never silently stage, revert, or overwrite unrelated work.

This is especially important because Symphony manages git workspaces and may coexist with:

- prior issue work
- partial user edits
- generated files
- stale validation artifacts

### Favor End-to-End Validation

Local unit tests are necessary but not sufficient here.

For lifecycle changes, the strongest validation is:

1. tracker picks the right issue
2. issue moves to the correct claimed state
3. workspace is prepared correctly
4. Codex runs successfully
5. publish behavior is correct
6. tracker ends in the correct next state

If a fix only makes tests pass but leaves that flow uncertain, it is incomplete.

## Working Norms

- Use Conventional Commit messages and PR titles.
- Preferred prefixes are `feat:`, `fix:`, `chore:`, `docs:`, and `refactor:`.
- Keep the dashboard lightweight and server-rendered unless requirements change.
- Treat tracker credentials, `WORKFLOW.md`, and workspace contents as operator-owned inputs.
- Avoid generic boilerplate and keep changes aligned to Symphony’s orchestration model.

## What Every Agent Should Remember

- The tracker is the workflow authority.
- The workspace is the execution authority.
- The session log is the best source for “what is it doing right now?”
- A successful run is not just “Codex answered”; it is “state, workspace, git, and publish all landed correctly.”
- The safest fixes are the ones that make restart behavior correct.
