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
  labels: [agent]
  exclude_labels: [blocked]
  assignee: "@me"
workspace:
  root: ~/tmp/symphony-workspaces
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
