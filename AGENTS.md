# Agent Instructions

This repository implements Symphony as a Python 3.13 `uv`-managed orchestration tool.

## Working Norms

- Use `uv` for dependency management, local execution, tests, linting, formatting, and type checks.
- Use Conventional Commit style messages and PR titles.
- Preferred prefixes are `feat:`, `fix:`, `chore:`, `docs:`, and `refactor:`.
- Run `pre-commit run --all-files` before finishing when the repo has `pre-commit` installed.
- Keep changes aligned to the Symphony spec and avoid adding generic boilerplate.

## Expected Commands

- `uv sync`
- `uv run pytest`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run ty check`

## Repo Guidance

- Preserve `uv` as the canonical workflow tool.
- Keep the dashboard lightweight and server-rendered unless requirements change.
- Treat tracker credentials and any local workflow files as operator-owned inputs.
