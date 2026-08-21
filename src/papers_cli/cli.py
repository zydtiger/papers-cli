from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from typing import NoReturn

import httpx

from .config import ensure_paths, get_paths
from .db import Database
from .downloader import download_pdf
from .errors import PapersError
from .models import RemotePaper
from .sources import adapter_for, infer_adapter, source_capabilities
from .storage import local_path, remove_local, verify_file

SCHEMA_VERSION = 1


def _envelope(data: object) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "ok": True, "data": data}


def _error(error: PapersError) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": {"code": error.code, "message": str(error), "details": error.details},
    }


def _render(value: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_envelope(value), sort_keys=True, separators=(",", ":")))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


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


class PapersArgumentParser(argparse.ArgumentParser):
    json_requested = False

    def error(self, message: str) -> NoReturn:
        if self.json_requested:
            raise PapersError("usage", message, exit_code=2)
        super().error(message)


def build_parser() -> PapersArgumentParser:
    parser = PapersArgumentParser(
        prog="papers", description="Find and verify official research PDFs."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sources = commands.add_parser("sources", help="List source capabilities")
    sources.add_argument("--json", action="store_true")

    search = commands.add_parser("search", help="Search a source")
    search.add_argument("--source", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--json", action="store_true")

    lookup = commands.add_parser("lookup", help="Look up metadata")
    lookup.add_argument("ref")
    lookup.add_argument("--json", action="store_true")

    download = commands.add_parser("download", help="Download official PDFs")
    download.add_argument("refs", nargs="+")
    download.add_argument("--dry-run", action="store_true")
    download.add_argument("--json", action="store_true")

    listing = commands.add_parser("list", help="List locally stored papers")
    listing.add_argument("--source")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--json", action="store_true")

    path = commands.add_parser("path", help="Print a local PDF path")
    path.add_argument("ref")
    path.add_argument("--json", action="store_true")

    remove = commands.add_parser("remove", help="Remove a paper from the local collection")
    remove.add_argument("ref")
    remove.add_argument("--dry-run", action="store_true")
    remove.add_argument("--json", action="store_true")

    verify = commands.add_parser("verify", help="Verify downloaded PDFs")
    verify_target = verify.add_mutually_exclusive_group(required=True)
    verify_target.add_argument("ref", nargs="?")
    verify_target.add_argument("--all", action="store_true")
    verify.add_argument("--json", action="store_true")
    return parser


def execute(args: argparse.Namespace) -> object:
    if getattr(args, "limit", 1) < 1 or getattr(args, "limit", 1) > 100:
        raise PapersError("invalid_limit", "--limit must be between 1 and 100", exit_code=2)
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
            if database is not None:
                try:
                    return database.get(args.ref)
                except PapersError as error:
                    if error.code != "not_found":
                        raise
            adapter, raw = infer_adapter(args.ref)
            timeout = httpx.Timeout(30.0, connect=10.0)
            with httpx.Client(timeout=timeout, headers={"User-Agent": "papers-cli/0.1"}) as client:
                return adapter.lookup(raw, client).as_dict()
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
            return remove_local(paths, database, args.ref, dry_run=args.dry_run)
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
            return str(local_path(paths, database.get(args.ref)))
        if args.command == "verify":
            records = database.list(None, None) if args.all else [database.get(args.ref)]
            verifications = [verify_file(paths, record) for record in records]
            if args.all:
                return {
                    "results": verifications,
                    "verified": sum(result["ok"] is True for result in verifications),
                    "total": len(verifications),
                }
            return verifications[0]

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
    PapersArgumentParser.json_requested = "--json" in arguments
    as_json = parser.json_requested
    try:
        args = parser.parse_args(arguments)
        as_json = bool(getattr(args, "json", False))
        _render(execute(args), as_json)
        return 0
    except PapersError as error:
        if as_json:
            print(json.dumps(_error(error), sort_keys=True, separators=(",", ":")))
        else:
            print(f"error [{error.code}]: {error}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
