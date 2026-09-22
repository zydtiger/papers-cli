# Papers CLI

`papers` is a local-first, cross-platform command-line tool for discovering paper metadata, downloading official PDFs, and keeping a verifiable local collection. It currently supports the official arXiv Atom API and the official bioRxiv API.

## Setup

```sh
uv sync
uv run papers sources --jsonl
uv run papers search --source arxiv --query "quantum computing" --limit 5 --jsonl
uv run papers download arxiv:2301.00001 --jsonl
uv run papers list --jsonl
```

## Machine-readable output

Every command accepts `--jsonl` for JSON Lines output. Each logical result is one independently parseable JSON object on its own stdout line, wrapped in the versioned envelope `{"schema_version": 1, "ok": true, "data": ...}`.

- Multi-result commands emit one line per logical result, in result order; single-result commands emit exactly one line.
- A successful command with no results emits nothing and exits zero.
- A usage or command-level failure emits exactly one `{"schema_version": 1, "ok": false, "error": ...}` object on stdout, keeps its documented non-zero exit status, and pairs it with an `error.code` where possible.
- Machine summaries and handled command or usage errors are not written to stderr.
- `papers verify REF... --jsonl` and `papers verify --all --jsonl` emit only per-paper verification records; there is no machine summary record. Totals are a human-mode concern, and agents can derive them with `jq -s` (see SKILL.md).

Without `--jsonl`, commands print human-readable output. Batch and whole-collection verification report a readable summary line in that mode.

## Field selection

The read-only metadata commands `search`, `lookup`, and `list` accept `--fields` with one comma-separated list of non-empty, unique top-level field names, such as `--fields ref,title,authors`. Each result record then contains only the selected fields, and the same projection applies in human and `--jsonl` mode. Record cardinality, ordering, values, and the versioned JSONL envelope are unchanged; this builds on the JSONL and batch-reference contracts from Issue #7.

- `search` and `lookup` (local and remote) accept the normalized remote-paper vocabulary: `ref`, `source`, `source_key`, `source_version`, `title`, `abstract`, `authors`, `categories`, `published_at`, `updated_at`, `doi`, `landing_url`, `pdf_url`.
- `list` additionally accepts the local-only fields `id`, `created_at`, `refreshed_at`, and `file`. `file` is one top-level field whose nested object (`sha256`, `byte_count`, `relative_path`, `source_url`) is returned intact when present and omitted when the paper has no attached PDF; use `papers path REF... --jsonl` for resolved absolute paths.

An empty selection, an empty comma-separated segment, a duplicate name, or a name outside the command's vocabulary fails as a usage error (exit status 2) before any provider request or collection access. Omitting `--fields` preserves the complete record for every command.

```sh
uv run papers search --source arxiv --query "quantum computing" --fields ref,title,authors --jsonl
uv run papers list --fields ref,title,file --jsonl
```

Field selection trims the serialized payload only; it does not reduce network, database, or in-memory work, and it does not replace `jq` for aggregation or transformations.

## Commands

- `papers sources --jsonl` reports installed source capabilities, one record per source.
- `papers search --source SOURCE --query QUERY --limit N --jsonl` searches official metadata. arXiv supports a general query; bioRxiv currently accepts a DOI only because its official API has no general full-text search endpoint. Add `--fields` to return only selected top-level fields per result.
- `papers lookup REF... --jsonl` resolves each reference in input order, using a local UUID/alias or a recognized remote identifier, without changing local storage. Order and duplicate references are preserved, emitting one record per input. `--fields` projects the shared remote-paper vocabulary onto local and remote records alike.
- `papers download REF... --jsonl` obtains the official PDFs and persists metadata and provenance. Search results return reusable `ref` values.
- `papers list --jsonl`, `papers path REF... --jsonl`, `papers verify REF... --jsonl` (mutually exclusive with `--all`), and `papers verify --all --jsonl` inspect the local collection. Reference batches preserve input order and duplicates and emit one record per reference. `list` also accepts `--fields`, including its local-only field names.
- `papers remove REF --jsonl` removes one paper from the local collection. `REF` may be its UUID or a stored alias; the command never performs a provider lookup and intentionally accepts a single reference. Use `--dry-run` to inspect the planned removal without writing collection or cache state.

There is no CLI approval flag: the invoking agent or person decides whether a download is allowed. `--dry-run` reports intended downloads without writing files or metadata.

## Storage and verification

SQLite stores metadata, aliases, provenance, and relative object paths; it never stores PDF blobs. Verified PDFs are immutable content-addressed objects:

```text
objects/sha256/ab/cd/<full-sha256>.pdf
```

The downloader accepts only adapter-supplied HTTPS URLs, revalidates every redirect against an allowlist, limits downloads to 100 MiB, checks the PDF signature, calculates SHA-256 while streaming, `fsync`s, and atomically publishes the object. Existing digests are reused.

Removal deletes the selected paper's metadata, aliases, and file links. A PDF object is deleted only when no other paper references its SHA-256 file record; shared objects are retained. If an expected object is already missing, Papers CLI still removes its stale metadata and reports `already_missing`. Removal is destructive and does not provide trash, undo, or restore behavior, so inspect `papers remove REF --dry-run --jsonl` first when the target or object sharing is uncertain.

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
papers sources --jsonl
```

Then install the committed root skill globally in file mode:

```sh
skillctl add --global https://github.com/zydtiger/papers-cli.git --file SKILL.md --name papers-cli-skill --ref main
skillctl update --global papers-cli-skill
```

The root `SKILL.md` is a single-file source so file-mode installation has no external resource dependency.
