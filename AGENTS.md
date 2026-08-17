# Repository Guidelines

## Project layout

- `src/papers_cli/` contains the application; `tests/` contains offline tests and fixtures.
- The root `SKILL.md` is the authoritative, single-file project skill source. Do not copy it into `.agents/`.
- Keep paper PDFs outside the repository. Runtime data belongs in the platform data directory.

## Tooling and validation

- Use `uv` with Python 3.12 or later.
- Run `uv run ruff check .`, `uv run pyright`, and `uv run pytest` for material changes.
- Tests must not require a live network; use mocked transports and fixtures.

## Git workflow

- Base branch: `main`.
- Allowed commit prefixes: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`, `perf`, `chore`, `revert`.
- Use `prefix(scope): concise imperative summary` with scopes `cli`, `db`, `storage`, `download`, `sources`, `config`, or `docs`.
- Inspect status and diff before staging. Do not publish, add a remote, push, tag, release, or create forge objects without explicit approval.
