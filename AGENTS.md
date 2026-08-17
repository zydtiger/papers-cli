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
- Use `prefix(scope): concise imperative summary` with scopes `cli`, `db`, `storage`, `download`, `sources`, `config`, `docs`, or `release`.
- Inspect status and diff before staging. Do not publish, add a remote, push, tag, release, or create forge objects without explicit approval.

## Release protocol

- Follow Semantic Versioning. Before `1.0.0`, use a patch bump for compatible fixes and a minor bump for new commands, sources, database changes, or breaking behavior. Treat `1.0.0` as the first stable CLI and database contract; after that, breaking changes require a major bump.
- Keep `pyproject.toml` as the package-version source of truth and refresh `uv.lock` whenever that version changes.
- Release only from a clean `main` that is synchronized with `origin/main`.
- Before release, run `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`, `uv run pytest`, and `uv build`. Also run an isolated live-provider download and verification smoke test with temporary data and cache directories; do not add live networking to the offline test suite.
- Prepare each version in a focused `chore(release): prepare vX.Y.Z` commit, create an annotated `vX.Y.Z` tag, and create a concise GitHub Release from that tag. Verify the published tag and installation from the exact tag.
- Always ask the user for explicit approval for the exact version immediately before pushing a release tag or creating, updating, or deleting a GitHub Release. Approval to edit, commit, or push ordinary commits is not release approval. Never move or replace an existing release tag.
