from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from typing import Final, NoReturn

import httpx

from .config import ensure_paths, get_paths
from .db import LIST_PAPER_FIELDS, Database
from .downloader import download_pdf
from .errors import PapersError
from .models import REMOTE_PAPER_FIELDS, RemotePaper
from .sources import adapter_for, infer_adapter, source_capabilities
from .storage import local_path, remove_local, verify_file

SCHEMA_VERSION = 1

JSONL_HELP = "emit one versioned JSONL record per logical result"

# Commands that accept --fields and the top-level field vocabulary each may project.
FIELD_VOCABULARIES: Final[dict[str, tuple[str, ...]]] = {
    "search": REMOTE_PAPER_FIELDS,
    "lookup": REMOTE_PAPER_FIELDS,
    "list": LIST_PAPER_FIELDS,
}

USAGE_EPILOG = (
    "Machine-readable output: --jsonl emits one versioned JSON object per logical "
    "result on stdout, in result order. An empty successful result set emits no "
    "records. A usage or command-level failure emits exactly one error object and a "
    "non-zero exit status. Machine summaries and handled command or usage errors are "
    "not written to stderr, and verification summaries are reported in human mode "
    "only."
)

MACHINE_CONTRACT_EPILOG = (
    "Machine-readable output: --jsonl emits one versioned JSON object per logical "
    "result on stdout, in result order. An empty success emits no records. A usage "
    "or command-level failure emits exactly one error object and a non-zero exit "
    "status. Machine summaries and handled command or usage errors are not written "
    "to stderr."
)

VERIFY_CONTRACT_EPILOG = (
    f"{MACHINE_CONTRACT_EPILOG} Verification summaries are reported in human mode only."
)


def _envelope(data: object) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "ok": True, "data": data}


def _error(error: PapersError) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": {"code": error.code, "message": str(error), "details": error.details},
    }


def _render_jsonl(records: Sequence[object]) -> None:
    for record in records:
        print(json.dumps(_envelope(record), sort_keys=True, separators=(",", ":")))


def _render_human(command: str, records: Sequence[object]) -> None:
    if command == "verify":
        verifications = [record for record in records if isinstance(record, dict)]
        verified = sum(record.get("ok") is True for record in verifications)
        for verification in verifications:
            print(json.dumps(verification, indent=2, sort_keys=True))
        print(f"Verified {verified} of {len(verifications)} papers.")
        return
    for record in records:
        if isinstance(record, str):
            print(record)
        else:
            print(json.dumps(record, indent=2, sort_keys=True))


def _remote_from_local(record: dict[str, object]) -> RemotePaper:
    required = (
        "source",
        "source_key",
        "title",
        "abstract",
        "authors",
        "categories",
        "landing_url",
        "pdf_url",
    )
    if any(key not in record for key in required):
        raise PapersError("storage_corrupt", "Local metadata is incomplete", exit_code=5)
    authors = record["authors"]
    categories = record["categories"]
    if not isinstance(authors, list) or not isinstance(categories, list):
        raise PapersError("storage_corrupt", "Local metadata lists are invalid", exit_code=5)
    return RemotePaper(
        source=str(record["source"]),
        source_key=str(record["source_key"]),
        source_version=str(record["source_version"])
        if record["source_version"] is not None
        else None,
        title=str(record["title"]),
        abstract=str(record["abstract"]),
        authors=[str(item) for item in authors],
        categories=[str(item) for item in categories],
        published_at=str(record["published_at"]) if record["published_at"] is not None else None,
        updated_at=str(record["updated_at"]) if record["updated_at"] is not None else None,
        doi=str(record["doi"]) if record["doi"] is not None else None,
        landing_url=str(record["landing_url"]),
        pdf_url=str(record["pdf_url"]),
    )


def _get_remote(ref: str, database: Database | None, client: httpx.Client) -> RemotePaper:
    if database is None:
        adapter, raw = infer_adapter(ref)
        return adapter.lookup(raw, client)
    try:
        local = database.get(ref)
    except PapersError as error:
        if error.code != "not_found":
            raise
        adapter, raw = infer_adapter(ref)
        return adapter.lookup(raw, client)
    local_remote = _remote_from_local(local)
    return adapter_for(local_remote.source).lookup(local_remote.source_key, client)


def _lookup_record(ref: str, database: Database | None, client: httpx.Client) -> dict[str, object]:
    if database is not None:
        try:
            return database.get(ref)
        except PapersError as error:
            if error.code != "not_found":
                raise
    adapter, raw = infer_adapter(ref)
    return adapter.lookup(raw, client).as_dict()


def _fields_help(command: str) -> str:
    return (
        "return only these comma-separated top-level fields per record "
        "(default: all fields); empty segments, duplicate names, and unknown names "
        f"are usage errors; allowed fields: {', '.join(FIELD_VOCABULARIES[command])}"
    )


def _selected_fields(args: argparse.Namespace) -> tuple[str, ...] | None:
    value = getattr(args, "fields", None)
    if value is None:
        return None
    allowed = FIELD_VOCABULARIES[args.command]
    selected = value.split(",")
    if "" in selected:
        raise PapersError("usage", "--fields must not contain empty field names", exit_code=2)
    seen: set[str] = set()
    for name in selected:
        if name in seen:
            raise PapersError(
                "usage", f"--fields contains duplicate field name '{name}'", exit_code=2
            )
        seen.add(name)
    unknown = [name for name in selected if name not in allowed]
    if unknown:
        raise PapersError(
            "usage",
            f"--fields has unknown field name(s) for {args.command}: "
            f"{', '.join(unknown)}; allowed: {', '.join(allowed)}",
            exit_code=2,
        )
    return tuple(selected)


def _project(record: object, fields: tuple[str, ...]) -> object:
    if not isinstance(record, dict):
        raise PapersError("storage_corrupt", "Field selection requires object records", exit_code=5)
    return {name: record[name] for name in fields if name in record}


class PapersArgumentParser(argparse.ArgumentParser):
    jsonl_requested = False

    def error(self, message: str) -> NoReturn:
        if self.jsonl_requested:
            raise PapersError("usage", message, exit_code=2)
        super().error(message)


def build_parser() -> PapersArgumentParser:
    parser = PapersArgumentParser(
        prog="papers",
        description="Find and verify official research PDFs.",
        epilog=USAGE_EPILOG,
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sources = commands.add_parser(
        "sources",
        help="List source capabilities",
        description="List installed source capabilities, one JSONL record per source.",
        epilog=MACHINE_CONTRACT_EPILOG,
        allow_abbrev=False,
    )
    sources.add_argument("--jsonl", action="store_true", help=JSONL_HELP)

    search = commands.add_parser(
        "search",
        help="Search a source",
        description="Search a source's official metadata, one JSONL record per result.",
        epilog=MACHINE_CONTRACT_EPILOG,
        allow_abbrev=False,
    )
    search.add_argument("--source", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--jsonl", action="store_true", help=JSONL_HELP)
    search.add_argument("--fields", help=_fields_help("search"))

    lookup = commands.add_parser(
        "lookup",
        help="Look up metadata for one or more references in input order",
        description=(
            "Resolve each reference, in input order, against the local collection or "
            "the source API, one JSONL record per reference; duplicates are preserved."
        ),
        epilog=MACHINE_CONTRACT_EPILOG,
        allow_abbrev=False,
    )
    lookup.add_argument("refs", nargs="+")
    lookup.add_argument("--jsonl", action="store_true", help=JSONL_HELP)
    lookup.add_argument("--fields", help=_fields_help("lookup"))

    download = commands.add_parser(
        "download",
        help="Download official PDFs",
        description=(
            "Download official PDFs for the given references, one JSONL record per "
            "reference in input order."
        ),
        epilog=MACHINE_CONTRACT_EPILOG,
        allow_abbrev=False,
    )
    download.add_argument("refs", nargs="+")
    download.add_argument("--dry-run", action="store_true")
    download.add_argument("--jsonl", action="store_true", help=JSONL_HELP)

    listing = commands.add_parser(
        "list",
        help="List locally stored papers",
        description="List locally stored papers, one JSONL record per paper.",
        epilog=MACHINE_CONTRACT_EPILOG,
        allow_abbrev=False,
    )
    listing.add_argument("--source")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--jsonl", action="store_true", help=JSONL_HELP)
    listing.add_argument("--fields", help=_fields_help("list"))

    path = commands.add_parser(
        "path",
        help="Print local PDF paths for one or more references in input order",
        description=(
            "Print the local PDF path for each reference in input order, one JSONL "
            "record per reference."
        ),
        epilog=MACHINE_CONTRACT_EPILOG,
        allow_abbrev=False,
    )
    path.add_argument("refs", nargs="+")
    path.add_argument("--jsonl", action="store_true", help=JSONL_HELP)

    remove = commands.add_parser(
        "remove",
        help="Remove a paper from the local collection",
        description=(
            "Remove one paper from the local collection, emitting exactly one JSONL record."
        ),
        epilog=MACHINE_CONTRACT_EPILOG,
        allow_abbrev=False,
    )
    remove.add_argument("ref")
    remove.add_argument("--dry-run", action="store_true")
    remove.add_argument("--jsonl", action="store_true", help=JSONL_HELP)

    verify = commands.add_parser(
        "verify",
        help="Verify downloaded PDFs for references or the whole collection",
        description=(
            "Verify stored PDFs for the given references or the whole collection, one "
            "JSONL record per paper and no machine summary record."
        ),
        epilog=VERIFY_CONTRACT_EPILOG,
        allow_abbrev=False,
    )
    verify.add_argument("refs", nargs="*")
    verify.add_argument("--all", action="store_true")
    verify.add_argument("--jsonl", action="store_true", help=JSONL_HELP)
    return parser


def execute(args: argparse.Namespace) -> Sequence[object]:
    if getattr(args, "limit", 1) < 1 or getattr(args, "limit", 1) > 100:
        raise PapersError("invalid_limit", "--limit must be between 1 and 100", exit_code=2)
    if args.command == "verify":
        if args.all and args.refs:
            raise PapersError("usage", "--all cannot be combined with references", exit_code=2)
        if not args.all and not args.refs:
            raise PapersError("usage", "verify requires references or --all", exit_code=2)
    if args.command == "sources":
        return list(source_capabilities())

    paths = get_paths()
    if args.command == "search":
        timeout = httpx.Timeout(30.0, connect=10.0)
        with httpx.Client(timeout=timeout, headers={"User-Agent": "papers-cli/0.1"}) as client:
            found = adapter_for(args.source.lower()).search(args.query, args.limit, client)
            return [paper.as_dict() for paper in found]

    if args.command == "lookup":
        database = None
        try:
            database = (
                Database(paths.database_path, read_only=True)
                if paths.database_path.is_file()
                else None
            )
            timeout = httpx.Timeout(30.0, connect=10.0)
            with httpx.Client(timeout=timeout, headers={"User-Agent": "papers-cli/0.1"}) as client:
                return [_lookup_record(ref, database, client) for ref in args.refs]
        except sqlite3.Error as error:
            raise PapersError(
                "storage_unavailable", "Unable to read the local collection", exit_code=5
            ) from error
        finally:
            if database is not None:
                database.close()

    if args.command == "download" and args.dry_run:
        database = None
        try:
            database = (
                Database(paths.database_path, read_only=True)
                if paths.database_path.is_file()
                else None
            )
            timeout = httpx.Timeout(30.0, connect=10.0)
            with httpx.Client(timeout=timeout, headers={"User-Agent": "papers-cli/0.1"}) as client:
                return [
                    {"ref": paper.ref, "dry_run": True, "pdf_url": paper.pdf_url}
                    for paper in (_get_remote(ref, database, client) for ref in args.refs)
                ]
        except sqlite3.Error as error:
            raise PapersError(
                "storage_unavailable", "Unable to read the local collection", exit_code=5
            ) from error
        finally:
            if database is not None:
                database.close()

    if args.command == "remove":
        if not paths.database_path.is_file():
            raise PapersError("not_found", f"No local paper matches {args.ref}", exit_code=3)
        database = None
        try:
            database = Database(paths.database_path, read_only=args.dry_run)
            return [remove_local(paths, database, args.ref, dry_run=args.dry_run)]
        except sqlite3.Error as error:
            raise PapersError(
                "storage_unavailable", "Unable to update the local collection", exit_code=5
            ) from error
        finally:
            if database is not None:
                database.close()

    ensure_paths(paths)
    database = Database(paths.database_path)
    try:
        if args.command == "list":
            return database.list(args.source, args.limit)
        if args.command == "path":
            return [str(local_path(paths, database.get(ref))) for ref in args.refs]
        if args.command == "verify":
            records = (
                database.list(None, None) if args.all else [database.get(ref) for ref in args.refs]
            )
            return [verify_file(paths, record) for record in records]

        timeout = httpx.Timeout(30.0, connect=10.0)
        with httpx.Client(timeout=timeout, headers={"User-Agent": "papers-cli/0.1"}) as client:
            if args.command == "download":
                stored = []
                for ref in args.refs:
                    paper = _get_remote(ref, database, client)
                    adapter = adapter_for(paper.source)
                    downloaded = download_pdf(client, paper.pdf_url, adapter.allowed_hosts, paths)
                    paper_id = database.upsert_paper(paper)
                    database.attach_file(paper_id, downloaded, paper.source_version)
                    stored.append(database.get(paper_id))
                return stored
    finally:
        database.close()
    raise AssertionError(f"Unhandled command {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    PapersArgumentParser.jsonl_requested = "--jsonl" in arguments
    as_jsonl = parser.jsonl_requested
    try:
        args = parser.parse_args(arguments)
        as_jsonl = bool(args.jsonl)
        fields = _selected_fields(args)
        records = execute(args)
        if fields is not None:
            records = [_project(record, fields) for record in records]
        if as_jsonl:
            _render_jsonl(records)
        else:
            _render_human(args.command, records)
        return 0
    except PapersError as error:
        if as_jsonl:
            print(json.dumps(_error(error), sort_keys=True, separators=(",", ":")))
        else:
            print(f"error [{error.code}]: {error}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
