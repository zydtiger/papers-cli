# Papers CLI

`papers` is a local-first, cross-platform command-line tool for discovering paper metadata, downloading official full text, and keeping a verifiable local collection. It supports the official arXiv Atom API, bioRxiv API, and PMC ID Converter plus PMC Cloud metadata.

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

- `search` and `lookup` (local and remote) accept the normalized remote-paper vocabulary: `ref`, `source`, `source_key`, `source_version`, `title`, `abstract`, `authors`, `categories`, `published_at`, `updated_at`, `doi`, `pmcid`, `pmid`, `license_code`, `fulltext_availability`, `landing_url`, `pdf_url`, and `content_urls`. `pdf_url` may be `null`; `content_urls` maps each format currently available from the source to its official URL. `fulltext_availability` is `available`, `unavailable`, or `unknown`.
- `list` additionally accepts the local-only fields `id`, `created_at`, `refreshed_at`, `file`, and `files`. `files` aggregates every stored attachment for a paper in stable format order. Each entry reports its `format`, `media_type`, SHA-256, size, relative path, provider `source`, source URL, source version, and retrieval timestamp. `file` remains the legacy default-PDF object (`sha256`, `byte_count`, `relative_path`, `source_url`) and is omitted if no PDF is attached; use `papers path REF... --format FORMAT --jsonl` for resolved absolute paths.

An empty selection, an empty comma-separated segment, a duplicate name, or a name outside the command's vocabulary fails as a usage error (exit status 2) before any provider request or collection access. Omitting `--fields` preserves the complete record for every command.

```sh
uv run papers search --source arxiv --query "quantum computing" --fields ref,title,authors --jsonl
uv run papers list --fields ref,title,file --jsonl
```

Field selection trims the serialized payload only; it does not reduce network, database, or in-memory work, and it does not replace `jq` for aggregation or transformations.

## Commands

- `papers sources --jsonl` reports installed source capabilities, one record per source. `metadata_search` reports whether a provider supports keyword metadata search, `lookup` reports identifier lookup support, `reference_formats` lists supported remote identifiers, and `fulltext_formats` lists available full-text media formats. The CLI accepts `pdf`, `txt`, and `xml` storage requests, but installed providers determine which can be retrieved: arXiv and bioRxiv advertise PDF only; PMC advertises PDF, text, and JATS XML subject to each article version's Cloud metadata. The existing `search` and `download` fields remain for compatibility; bioRxiv reports `search: "doi_only"` because a valid bioRxiv DOI is accepted as a lookup convenience despite `metadata_search: false`.
- `papers search --source SOURCE --query QUERY --limit N --jsonl` searches official metadata. arXiv supports a general query. bioRxiv accepts only a valid `10.1101/...` DOI, delegates it to lookup, and reports `unsupported_search` for keywords; a DOI for another publisher reports `invalid_ref`. Add `--fields` to return only selected top-level fields per result.
- `papers lookup REF... --jsonl` resolves each reference in input order, using a local UUID/alias or a recognized remote identifier, without changing local storage. Remote identifiers are source-qualified, such as `arxiv:2301.00001`, `biorxiv:10.1101/2024.01.01.123456`, and `pmc:PMC3531190`; unqualified arXiv identifiers, valid bioRxiv `10.1101/...` DOIs, and PMCIDs such as `PMC3531190.1` remain supported. A PMC lookup uses the ID Converter to select the current version unless a version is explicit, then reads that version's official PMC Cloud metadata. `doi:DOI` resolves an existing local DOI alias only. Papers CLI has no generic remote DOI resolver: an unresolved `doi:DOI` or DOI outside bioRxiv reports `unsupported_ref`. A malformed source-qualified identifier reports `invalid_ref`, while a valid identifier missing locally or from its source reports `not_found`. Order and duplicate references are preserved, emitting one record per input. `--fields` projects the shared remote-paper vocabulary onto local and remote records alike.
- `papers download REF... --format pdf|txt|xml --jsonl` obtains exactly one official full-text format and persists metadata and provenance. `--format` defaults to `pdf`; Papers CLI never converts a different format or falls back to another available format. If the source does not provide the requested format, it reports `format_unavailable` with the requested and available formats. It follows the same remote-reference and local-DOI-alias rules as `lookup`. Search results return reusable `ref` values.
- `papers list --jsonl`, `papers path REF... --format pdf|txt|xml --jsonl`, `papers verify REF... [--format pdf|txt|xml] --jsonl` (mutually exclusive with `--all`), and `papers verify --all [--format pdf|txt|xml] --jsonl` inspect the local collection. `path` defaults to PDF. `verify` without `--format` checks every stored attachment while still emitting one record per paper; `verify --format` checks only the requested format and reports `no_file` with `available_formats` instead of silently skipping it. Reference batches preserve input order and duplicates and emit one record per reference. `list` also accepts `--fields`, including its local-only field names.
- `papers remove REF --jsonl` removes one paper from the local collection. `REF` may be its UUID or a stored alias; the command never performs a provider lookup and intentionally accepts a single reference. Use `--dry-run` to inspect the planned removal without writing collection or cache state.

There is no CLI approval flag: the invoking agent or person decides whether a download is allowed. `--dry-run` reports intended downloads without writing files or metadata.

## Storage and verification

SQLite stores metadata, aliases, provenance, and relative object paths; it never stores full-text blobs. Verified full-text files are immutable content-addressed objects:

```text
objects/sha256/ab/cd/<full-sha256>.<format>
```

The downloader accepts only adapter-supplied HTTPS URLs, revalidates every redirect against an allowlist, limits downloads to 100 MiB, and validates the requested format without conversion before atomically publishing the object. PDFs must have the expected content type and signature, parse structurally, and expose at least one readable page; encrypted PDFs are rejected because their pages cannot be inspected. XML is parsed with `defusedxml`: a JATS `article` needs non-empty `body` text, and safe external JATS `DOCTYPE` declarations are allowed while entities and external expansion remain blocked. Text must be non-empty UTF-8 and cannot begin as an HTML document; text that merely mentions CAPTCHA remains valid. HTTP 401 and 403 responses report `download_access`, separately from unavailable formats and network failures. No rejected staging file becomes an object or collection attachment. The downloader calculates SHA-256 while streaming, `fsync`s, and then publishes the object. Existing digests are reused.

Removal deletes the selected paper's metadata, aliases, and every attached file link. An object is deleted only when no other paper references its SHA-256 file record; shared objects are retained. If an expected object is already missing, Papers CLI still removes its stale metadata and reports `already_missing`. Removal is destructive and does not provide trash, undo, or restore behavior, so inspect `papers remove REF --dry-run --jsonl` first when the target or object sharing is uncertain.

Incomplete downloads live in the disposable cache as `downloads/download-*.part`. After validation and `fsync`, Papers CLI atomically moves the part into the data directory's content-addressed object tree. This installation assumes the configured cache and data directories are on the same filesystem.

Runtime paths:

- macOS data: `~/Library/Application Support/papers-cli`; cache: `~/Library/Caches/papers-cli`
- Linux data: `${XDG_DATA_HOME:-~/.local/share}/papers-cli`; cache: `${XDG_CACHE_HOME:-~/.cache}/papers-cli`

For automation and tests, `PAPERS_CLI_DATA_DIR` and `PAPERS_CLI_CACHE_DIR` override these locations with absolute paths.

Each imported paper has a UUIDv7 internal ID. Python 3.12 does not provide `uuid.uuid7()`, so Papers CLI implements the RFC 9562 v7 bit layout locally (Unix-millisecond timestamp plus cryptographically random payload) and tests its version/variant. This preserves Python 3.12+ support without a UUID dependency.

## Architecture

`sources.py` normalizes official provider responses into source-specific identities; `db.py` persists those records and aliases; `downloader.py` validates and stores bytes; `storage.py` verifies objects. A source identity is never automatically merged across providers, even if a DOI matches, avoiding incorrect cross-provider deduplication. File identity is SHA-256.

PMC downloads use only URLs supplied by the current PMC Cloud metadata record. The provider translates its exact `s3://pmc-oa-opendata/...` object key to the equivalent allowlisted `https://pmc-oa-opendata.s3.amazonaws.com/...` retrieval URL; it never derives a publisher or PMC HTML file URL. PMC Cloud uses `binary/octet-stream` for its PDF, text, and XML objects, which is accepted only for PMC targets and still undergoes the requested format's body validation. PDF is optional in PMC Cloud metadata. A known PMC record without Cloud metadata falls back to PMC ESummary for official metadata and reports `fulltext_availability: "unknown"`; an embargoed version reports `unavailable` without attempting a file download.

## Development

Use Python 3.12 or later with `uv`. Install the hook runner with `uv tool install prek`, then activate it with `prek install`. See `.pre-commit-config.yaml` for the authoritative validation commands and file scopes; CI uses the same hook configuration.

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
