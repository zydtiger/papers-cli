# Papers CLI

`papers` is a local-first, cross-platform command-line tool for discovering paper metadata, downloading official PDFs, and keeping a verifiable local collection. It currently supports the official arXiv Atom API and the official bioRxiv API.

## Setup

```sh
uv sync
uv run papers sources --json
uv run papers search --source arxiv --query "quantum computing" --limit 5 --json
uv run papers download arxiv:2301.00001 --json
uv run papers list --json
```

All `--json` commands emit exactly one versioned JSON document on stdout. A non-zero exit status is paired with an `error.code` where possible.

## Commands

- `papers sources --json` reports installed source capabilities.
- `papers search --source SOURCE --query QUERY --limit N --json` searches official metadata. arXiv supports a general query; bioRxiv currently accepts a DOI only because its official API has no general full-text search endpoint.
- `papers lookup REF --json` resolves a local UUID/alias or looks up a recognized remote identifier without changing local storage.
- `papers download REF... --json` obtains the official PDF and persists metadata and provenance. Search results return reusable `ref` values.
- `papers list --json`, `papers path REF`, `papers verify REF --json`, and `papers verify --all --json` inspect the local collection.

There is no CLI approval flag: the invoking agent or person decides whether a download is allowed. `--dry-run` reports intended downloads without writing files or metadata.

## Storage and verification

SQLite stores metadata, aliases, provenance, and relative object paths; it never stores PDF blobs. Verified PDFs are immutable content-addressed objects:

```text
objects/sha256/ab/cd/<full-sha256>.pdf
```

The downloader accepts only adapter-supplied HTTPS URLs, revalidates every redirect against an allowlist, limits downloads to 100 MiB, checks the PDF signature, calculates SHA-256 while streaming, `fsync`s, and atomically publishes the object. Existing digests are reused.

Incomplete downloads live in the disposable cache as `downloads/download-*.part`. After validation and `fsync`, Papers CLI atomically moves the part into the data directory's content-addressed object tree. This installation assumes the configured cache and data directories are on the same filesystem.

Runtime paths:

- macOS data: `~/Library/Application Support/papers-cli`; cache: `~/Library/Caches/papers-cli`
- Linux data: `${XDG_DATA_HOME:-~/.local/share}/papers-cli`; cache: `${XDG_CACHE_HOME:-~/.cache}/papers-cli`

For automation and tests, `PAPERS_CLI_DATA_DIR` and `PAPERS_CLI_CACHE_DIR` override these locations with absolute paths.

Each imported paper has a UUIDv7 internal ID. Python 3.12 does not provide `uuid.uuid7()`, so Papers CLI implements the RFC 9562 v7 bit layout locally (Unix-millisecond timestamp plus cryptographically random payload) and tests its version/variant. This preserves Python 3.12+ support without a UUID dependency.

## Architecture

`sources.py` normalizes official provider responses into source-specific identities; `db.py` persists those records and aliases; `downloader.py` validates and stores bytes; `storage.py` verifies objects. A source identity is never automatically merged across providers, even if a DOI matches, avoiding incorrect cross-provider deduplication. File identity is SHA-256.

## Development

```sh
uv run ruff check .
uv run pyright
uv run pytest
```

Live-provider smoke checks are intentionally not part of the test suite.

## Installing the executable and project skill

Install the `papers` executable globally with uv:

```sh
uv tool install git+https://github.com/zydtiger/papers-cli.git
papers sources --json
```

Then install the committed root skill globally in file mode:

```sh
skillctl add --global https://github.com/zydtiger/papers-cli.git --file SKILL.md --name papers-cli-skill --ref main
skillctl update --global papers-cli-skill
```

The root `SKILL.md` is a single-file source so file-mode installation has no external resource dependency.
