---
name: papers-cli-skill
description: Use the Papers CLI to discover official paper metadata, download approved PDFs, and verify a local research collection.
---

Use `papers sources --json` to discover available providers and capabilities before choosing a source.

Use `papers search --source SOURCE --query QUERY --limit N --json` to get normalized results. Preserve the returned `ref` when proposing or acquiring a result.

Treat downloading a PDF as an external side effect. Obtain the user's approval before running `papers download REF --json`; do not treat `--dry-run` as approval.

Use `papers lookup REF --json` before download when metadata or provenance needs confirmation. After acquisition, use `papers path REF` to retrieve the local path and `papers verify REF --json` to check the stored digest. Use `papers verify --all --json` for a collection audit.

Use `papers remove REF --dry-run --json` to inspect a local removal before changing collection state. Removal accepts a local UUID or stored alias and never performs a provider lookup. Obtain the user's approval before running `papers remove REF --json`: it permanently removes the selected paper metadata and deletes its PDF only when the content-addressed object is not shared by another paper. There is no trash, undo, or restore command.

Do not pass arbitrary PDF URLs to the CLI. The CLI downloads only official, provider-approved URLs and stores verified content-addressed files locally.
