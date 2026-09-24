---
name: papers-cli-skill
description: Use the Papers CLI to discover official paper metadata, download approved full text, and verify a local research collection.
---

Use `papers sources --jsonl` to discover available providers and capabilities before choosing a source. Inspect `metadata_search`, `lookup`, `reference_formats`, and `fulltext_formats`: they respectively describe keyword metadata search, identifier lookup, supported remote identifier types, and available full-text media formats. The CLI accepts `pdf`, `txt`, and `xml` requests, but providers determine actual availability; the currently installed arXiv and bioRxiv providers advertise PDF only. Keep the legacy `search` and `download` fields for compatibility. bioRxiv reports `search: "doi_only"` because a valid bioRxiv DOI is delegated to lookup, while `metadata_search` is false.

Use `papers search --source SOURCE --query QUERY --limit N --fields ref,title,authors --jsonl` to get normalized results. Preserve the returned `ref` when proposing or acquiring a result. bioRxiv accepts only a valid `10.1101/...` DOI through this command; it reports `unsupported_search` for a keyword and `invalid_ref` for a DOI belonging to another publisher.

Use source-qualified remote references such as `arxiv:2301.00001` or `biorxiv:10.1101/2024.01.01.123456`. Unqualified arXiv identifiers and valid bioRxiv `10.1101/...` DOIs remain supported. `doi:DOI` is a local collection alias only: use it only after the paper is already stored, and expect `unsupported_ref` if it cannot be resolved locally. Papers CLI has no generic remote DOI resolver, so a DOI outside bioRxiv reports `unsupported_ref`. A malformed source-qualified identifier reports `invalid_ref`; a valid remote or local identifier that is absent reports `not_found`.

Use `--fields` with `search`, `lookup`, and `list` to request only the fields a workflow needs: `--fields ref,title,authors` fits discovery and approval reviews, and `papers list --fields ref,title,file --jsonl` builds a local collection manifest. Omit `--fields` when a step needs the complete record, such as reading an abstract for approval. Selections are existing top-level field names only, apply identically in human and `--jsonl` modes, and invalid selections fail as usage errors before any provider request or collection read. Keep jq for summaries and transformations that field selection cannot express.

The shared fields for `search` and `lookup` are `ref`, `source`, `source_key`, `source_version`, `title`, `abstract`, `authors`, `categories`, `published_at`, `updated_at`, `doi`, `landing_url`, `pdf_url`, and `content_urls`. `pdf_url` can be null; `content_urls` describes format-specific official URLs. Only `list` additionally accepts `id`, `created_at`, `refreshed_at`, legacy default-PDF `file`, and aggregate `files`. Each `files` entry carries format, media type, checksum, path, actual file-provider `source`, source URL/version, and retrieval time. Use `path --format FORMAT` for resolved absolute paths. Empty selections, empty comma-separated segments, duplicate names, and unknown names are usage errors.

Treat downloading full text as an external side effect. Obtain the user's approval before running `papers download REF... --format pdf|txt|xml --jsonl`; do not treat `--dry-run` as approval. The default format is `pdf`. The CLI never converts another format or falls back when the requested format is unavailable; it reports `format_unavailable` with structured requested/available-format details. Prefer one batched invocation for an approved set of references: reference-accepting commands preserve input order and duplicates and emit one record per reference.

Use `papers lookup REF... --jsonl` before download when metadata or provenance needs confirmation. After acquisition, use `papers path REF... --format FORMAT --jsonl` to retrieve local paths. `path` defaults to PDF. Use `papers verify REF... --jsonl` to check every stored file for a paper, or add `--format FORMAT` to check only one; a requested but absent local format returns `no_file` with `available_formats`. Use `papers verify --all [--format FORMAT] --jsonl` for a collection audit; explicit references and `--all` are mutually exclusive.

Machine-readable output is JSON Lines: one versioned JSON envelope per logical result on stdout, no output for an empty success, exactly one error object for a handled command or usage failure, and no handled errors or summaries on stderr. `verify` emits only per-paper records with no machine summary; derive totals with jq when needed:

```sh
set -o pipefail
papers verify --all --jsonl | jq -s '
  if any(.[]; .ok != true) then error("Verification command failed")
  else {total: length, verified: (map(select(.data.ok)) | length), failed: (map(select(.data.ok | not)) | length)}
  end'
```

Check the pipeline exit status before using totals. Envelope `.ok` reports command success; `.data.ok` reports whether an individual paper's selected or stored files passed verification.

Use `papers remove REF --dry-run --jsonl` to inspect a local removal before changing collection state. Removal accepts a local UUID or stored alias, never performs a provider lookup, and intentionally accepts a single reference. Obtain the user's approval before running `papers remove REF --jsonl`: it permanently removes the selected paper metadata and each attached file only when the content-addressed object is not shared by another paper. There is no trash, undo, or restore command.

Do not pass arbitrary full-text URLs to the CLI. The CLI downloads only official, provider-approved URLs and stores verified content-addressed files locally.
