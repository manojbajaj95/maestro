---
tracker:
  kind: github
  labels: [agent]
  exclude_labels: [blocked]
  assignee: "@me"
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

Repository conventions:
- Follow AGENTS.md
- Keep changes focused to the issue
- Run only lightweight issue-appropriate checks before finishing
- Prefer `uv run ruff check .` for small changes
- Run broader validation only when the change requires it
- Do not run `pre-commit run --all-files` unless explicitly needed
