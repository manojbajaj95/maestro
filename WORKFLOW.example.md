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
  prepare: uv sync --group dev
polling:
  interval_ms: 30000
agent:
  max_concurrent_agents: 2
  max_retry_backoff_ms: 300000
  max_turns: 10
codex:
  command: codex app-server
  turn_timeout_ms: 300000
server:
  port: 8080
---
Work on issue {{ issue.identifier }}: {{ issue.title }}

Attempt number: {{ attempt }}

Use the repository and issue context to complete the task safely.

<!--
GitHub tracker example:

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
  prepare: uv sync --group dev
polling:
  interval_ms: 30000
agent:
  max_concurrent_agents: 1
codex:
  command: codex app-server
server:
  port: 8080
---
-->
