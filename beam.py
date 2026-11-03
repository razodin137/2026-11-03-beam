#!/usr/bin/env python3
"""Beam — drop files and links in inbox/, get visual HTML briefs in sorted/."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None  # type: ignore[assignment]

from beamlib import api, prompts
from beamlib.acquire import looks_like_url
from beamlib.config import (
    COLLECTIONS,
    FAILED,
    INBOX,
    LOGS,
    PROCESSING,
    ROOT,
    SORTED,
    category_meta,
    ensure_dirs,
    load_config,
)
from beamlib.pipeline import Processor, status
from beamlib.util import atomic_write_text, local_stamp, slugify, unique_path


VERSION = "1.0.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beam",
        description="Turn links, files, and notes into visual HTML briefs, sorted by an Ollama decision model.",
    )
    parser.add_argument("--version", action="version", version=f"beam {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="create the folder tree and config")

    add_parser = subparsers.add_parser("add", help="put a URL, file, or text note into inbox/")
    add_parser.add_argument("source", help="a URL, an existing file path, a text note, or - for stdin")
    add_parser.add_argument("--name", help="filename stem for a text note or URL")
    add_parser.add_argument("--text", action="store_true", help="force treating the argument as text")

    watch_parser = subparsers.add_parser("watch", help="watch inbox/ and process new items")
    watch_parser.add_argument("--once", action="store_true", help="process what is already in inbox/ and exit")
    watch_parser.add_argument("--interval", type=float, help="seconds between scans (default: config watch.interval_seconds)")
    watch_parser.add_argument("--verbose", action="store_true", help="print extra detail")

    serve_parser = subparsers.add_parser("serve", help="run the local web app and background queue")
    serve_parser.add_argument("--host", help="bind address (default: config server.host)")
    serve_parser.add_argument("--port", type=int, help="port (default: config server.port)")
    serve_parser.add_argument("--open", action="store_true", dest="open_browser", help="open the app in a browser")

    process_parser = subparsers.add_parser("process", help="process one file now")
    process_parser.add_argument("path", help="file to process (copied into inbox/ unless it is already there)")

    subparsers.add_parser("index", help="rebuild index.html from sorted/ metadata")
    subparsers.add_parser("rebuild", help="re-render every page from saved metadata (no model calls)")
    subparsers.add_parser("status", help="show inbox, failed, and per-category counts")
    doctor_parser = subparsers.add_parser("doctor", help="check Ollama, models, tools, and folders")
    doctor_parser.add_argument("--test", action="store_true", help="send a tiny JSON request to each configured model")

    return parser


def cmd_init(cfg: dict[str, Any]) -> int:
    ensure_dirs()
    for key in cfg["categories"]:
        directory = SORTED / key
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch(exist_ok=True)
    for directory in (INBOX, PROCESSING, COLLECTIONS, FAILED, LOGS):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch(exist_ok=True)
    print(f"Beam is ready at {ROOT}")
    print(f"  drop zone: {INBOX}")
    print(f"  categories: {', '.join(cfg['categories'])}")
    print("  next: ./beam.py serve --open")
    print("        ./beam.py add 'https://example.com/article' --name my-article")
    return 0


def _url_filename(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    tail = Path(parsed.path.rstrip("/")).name
    return slugify(f"{host} {tail}".strip(), fallback="link")[:60]


def cmd_add(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    ensure_dirs()
    raw = args.source
    forced_text = bool(args.text)

    if raw == "-":
        text = sys.stdin.read()
        if not text.strip():
            print("nothing on stdin", file=sys.stderr)
            return 1
        suffix = ".url" if looks_like_url(text) else ".txt"
        name = args.name or f"note-{local_stamp()}"
        destination = unique_path(INBOX, slugify(name), suffix)
        atomic_write_text(destination, text.strip() + "\n")
        print(destination)
        return 0

    if not forced_text and looks_like_url(raw):
        destination = unique_path(INBOX, slugify(args.name) if args.name else _url_filename(raw), ".url")
        atomic_write_text(destination, raw.strip() + "\n")
        print(destination)
        return 0

    candidate = Path(raw).expanduser()
    if not forced_text and candidate.exists():
        if candidate.is_dir():
            print(f"not a file: {candidate}", file=sys.stderr)
            return 1
        if candidate.resolve().parent == INBOX.resolve():
            print(candidate.resolve())
            return 0
        destination = unique_path(INBOX, slugify(candidate.stem), candidate.suffix)
        shutil.copy2(candidate, destination)
        print(destination)
        return 0

    suffix = ".url" if looks_like_url(raw.strip()) else ".txt"
    name = args.name or f"note-{local_stamp()}"
    destination = unique_path(INBOX, slugify(name), suffix)
    atomic_write_text(destination, raw.strip() + "\n")
    print(destination)
    return 0


def _lock_watcher() -> Any:
    if fcntl is None:
        return None
    lock_path = ROOT / ".beam.lock"
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit("another Beam watcher is already running in this folder")
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _inbox_files() -> list[Path]:
    if not INBOX.exists():
        return []
    files = []
    for path in sorted(INBOX.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in {".tmp", ".part", ".crdownload", ".swp"}:
            continue
        files.append(path)
    return files


def cmd_watch(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    ensure_dirs()
    processor = Processor(cfg)
    interval = float(args.interval or cfg["watch"].get("interval_seconds", 2.0))
    settle = float(cfg["watch"].get("settle_seconds", 1.2))

    if args.once:
        files = _inbox_files()
        if not files:
            print("inbox is empty")
            return 0
        failures = 0
        for path in files:
            if not processor.process(path):
                failures += 1
        return 1 if failures else 0

    lock_handle = _lock_watcher()
    print(f"Beam watching {INBOX} (every {interval:g}s). Press Ctrl-C to stop.")
    pending: dict[str, tuple[int, int]] = {}
    failures = 0
    try:
        while True:
            try:
                current = {path.name for path in _inbox_files()}
                for path in _inbox_files():
                    try:
                        stat = path.stat()
                    except FileNotFoundError:
                        continue
                    signature = (stat.st_size, stat.st_mtime_ns)
                    if pending.get(path.name) == signature and (time.time() - stat.st_mtime) >= settle:
                        pending.pop(path.name, None)
                        if not processor.process(path):
                            failures += 1
                    else:
                        pending[path.name] = signature
                for name in list(pending):
                    if name not in current:
                        pending.pop(name, None)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                processor.log(f"watcher loop error: {type(exc).__name__}: {exc}", "ERROR")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lock_handle.close()


def cmd_serve(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    from beamlib.server import run_server

    run_server(cfg, args.host, args.port, bool(args.open_browser))
    return 0


def cmd_process(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    ensure_dirs()
    processor = Processor(cfg)
    source = Path(args.path).expanduser().resolve()
    if not source.exists():
        print(f"file not found: {source}", file=sys.stderr)
        return 1
    if not source.is_file():
        print(f"not a file: {source}", file=sys.stderr)
        return 1
    if source.parent in {INBOX.resolve(), PROCESSING.resolve()}:
        return 0 if processor.process(source) else 1
    destination = unique_path(INBOX, slugify(source.stem), source.suffix)
    shutil.copy2(source, destination)
    print(f"copied to {destination}")
    return 0 if processor.process(destination) else 1


def cmd_index(cfg: dict[str, Any]) -> int:
    path = Processor(cfg).regenerate_index()
    print(path)
    return 0


def cmd_rebuild(cfg: dict[str, Any]) -> int:
    count = Processor(cfg).rebuild()
    print(f"re-rendered {count} page{'s' if count != 1 else ''}")
    return 0


def cmd_status(cfg: dict[str, Any]) -> int:
    state = status(cfg)
    print(f"Beam status \u2014 {ROOT}")
    print(f"  inbox        {state['inbox']} waiting")
    print(f"  processing   {state['processing']}")
    print(f"  collections  {state.get('collections', 0)}")
    print(f"  failed       {state['failed']}")
    print("  sorted")
    total = 0
    for key, count in state["categories"].items():
        total += count
        label = category_meta(cfg, key).get("label", key)
        print(f"    {label:<18} {count}")
    print(f"  total briefs {total}")
    index_path = ROOT / "index.html"
    print(f"  index        {index_path}{'' if index_path.exists() else ' (not built yet)'}")
    return 0


def cmd_doctor(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    ok = True
    print(f"Beam doctor \u2014 {ROOT}")
    try:
        version = api.api_version(cfg)
        print(f"  [ok] Ollama {version} at {cfg['api']['base_url']}")
    except Exception as exc:
        print(f"  [!!] Ollama unreachable: {exc}")
        ok = False

    models: list[str] = []
    try:
        models = api.list_models(cfg)
        print(f"  [ok] {len(models)} models available")
    except Exception as exc:
        print(f"  [!!] cannot list models: {exc}")
        ok = False

    configured = {
        "decision": cfg["models"]["decision"],
        "premium": cfg["models"]["premium"],
        "cheap": cfg["models"]["cheap"],
    }
    for role, model in configured.items():
        found = model in models
        print(f"  {'[ok]' if found else '[??]'} {role:<8} {model}" + ("" if found else " (not listed; cloud models can still respond)"))

    for tool in ("yt-dlp", "pdftotext"):
        found = shutil.which(tool) is not None
        print(f"  {'[ok]' if found else '[!!]'} {tool}" + ("" if found else " is not installed"))
        if not found:
            ok = False

    for label, directory in (("inbox", INBOX), ("sorted", SORTED), ("logs", LOGS)):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".beam-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(f"  [ok] {label} writable")
        except OSError as exc:
            print(f"  [!!] {label} is not writable: {exc}")
            ok = False

    try:
        prompts.load_prompt("classify")
        prompts.load_prompt("route")
        prompts.load_prompt("extract")
        prompts.load_prompt("collection")
        print("  [ok] prompt files present")
    except Exception as exc:
        print(f"  [!!] {exc}")
        ok = False

    if args.test:
        for role, model in configured.items():
            try:
                data, result = api.chat_json(
                    cfg,
                    model,
                    [{"role": "user", "content": 'Return {"ok": true} and nothing else.'}],
                    num_predict=300,
                    temperature=0,
                )
                print(f"  [ok] {role:<8} replied in {result['duration_ms']} ms: {json.dumps(data)[:80]}")
            except Exception as exc:
                print(f"  [!!] {role:<8} failed: {exc}")
                ok = False

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    cfg = load_config()
    if args.command == "init":
        return cmd_init(cfg)
    if args.command == "add":
        return cmd_add(cfg, args)
    if args.command == "watch":
        return cmd_watch(cfg, args)
    if args.command == "serve":
        return cmd_serve(cfg, args)
    if args.command == "process":
        return cmd_process(cfg, args)
    if args.command == "index":
        return cmd_index(cfg)
    if args.command == "rebuild":
        return cmd_rebuild(cfg)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "doctor":
        return cmd_doctor(cfg, args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
