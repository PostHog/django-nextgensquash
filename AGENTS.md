# Agent notes

Design, config keys, and the planner guards live in README.md. Read it first.

## Commands

- `uv sync` then `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format .`, `uv run mypy src`
- Run from this directory. From a PostHog flox shell, prefix every uv call with
  `env -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT`, or uv installs into the monorepo venv.

## Rules

- Output is deterministic and consumers diff it. A change to `emit.py` or `planning.py` must
  keep byte-identical output for a tree it does not intend to change. Re-emit against a real
  project (PostHog) and `diff -r` before and after.
- Every guard that refuses a tree, and every rule that drops or rewrites an operation, needs a
  test in `tests/` with the shape that triggered it.
- `retire` deletes files. Run it only on a tree whose `install` output is applied everywhere,
  never against a live project checkout while iterating.
- The package is a dev-only tool for its consumers. Generated files import the project's
  `OPERATIONS_MODULE`, never this package.
- `main` takes PRs with signed commits only. Conventional commits, imperative titles.
- Comments explain why, not history. No "changed because" or review references in code.
