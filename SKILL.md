---
name: papers-cli-skill
description: Use the Papers CLI to discover official paper metadata, download approved PDFs, and verify a local research collection.
---

Use `papers sources --jsonl` to discover available providers and capabilities before choosing a source.

Use `papers search --source SOURCE --query QUERY --limit N --jsonl` to get normalized results. Preserve the returned `ref` when proposing or acquiring a result.

Treat downloading a PDF as an external side effect. Obtain the user's approval before running `papers download REF... --jsonl`; do not treat `--dry-run` as approval. Prefer one batched invocation for an approved set of references: reference-accepting commands preserve input order and duplicates and emit one record per reference.

Use `papers lookup REF... --jsonl` before download when metadata or provenance needs confirmation. After acquisition, use `papers path REF... --jsonl` to retrieve local paths and `papers verify REF... --jsonl` to check stored digests. Use `papers verify --all --jsonl` for a collection audit.

Machine-readable output is JSON Lines: one versioned JSON envelope per logical result on stdout, no output for an empty success, exactly one error object for a failed invocation, and nothing on stderr. `verify` emits only per-paper records with no machine summary; derive totals with jq when needed:

```sh
papers verify --all --jsonl | jq -s '{total: length, verified: (map(select(.data.ok)) | length), failed: (map(select(.data.ok | not)) | length)}'
```

Use `papers remove REF --dry-run --jsonl` to inspect a local removal before changing collection state. Removal accepts a local UUID or stored alias, never performs a provider lookup, and intentionally accepts a single reference. Obtain the user's approval before running `papers remove REF --jsonl`: it permanently removes the selected paper metadata and deletes its PDF only when the content-addressed object is not shared by another paper. There is no trash, undo, or restore command.

Do not pass arbitrary PDF URLs to the CLI. The CLI downloads only official, provider-approved URLs and stores verified content-addressed files locally.
