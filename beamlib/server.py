"""Local web app + background job queue for Beam."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from . import api, collection, pipeline, render
from .acquire import extract_urls, looks_like_url
from .config import (
    COLLECTIONS,
    INBOX,
    LOGS,
    ROOT,
    ensure_dirs,
    load_config,
)
from .pipeline import Processor
from .util import atomic_write_text, now_iso, safe_filename, slugify, unique_path

WEB_DIR = ROOT / "web"
MAX_BODY_BYTES = 80 * 1024 * 1024
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_TABS = 200
APP_VERSION = "1.0.0"

mimetypes.add_type("text/html", ".html")
mimetypes.add_type("text/plain", ".txt")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/webp", ".webp")


@dataclass
class Job:
    id: str
    kind: str
    title: str = ""
    status: str = "queued"
    category: str = ""
    category_label: str = ""
    href: str = ""
    message: str = ""
    source_count: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "category": self.category,
            "category_label": self.category_label,
            "href": self.href,
            "message": self.message,
            "source_count": self.source_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _clean_links(values: list[str]) -> list[str]:
    links: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if not value:
            continue
        if looks_like_url(value):
            links.append(value)
        else:
            links.extend(extract_urls(value))
    return _dedupe([link for link in links if looks_like_url(link)])


def _link_stem(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    tail = Path(parsed.path.rstrip("/")).name
    return slugify(f"{host} {tail}".strip(), fallback="link")[:60]


class AppState:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.processor = Processor(cfg)
        self.queue: queue.PriorityQueue[tuple[int, int, dict[str, Any]]] = queue.PriorityQueue()
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.managed_paths: set[str] = set()
        self.pending_external: dict[str, tuple[int, int]] = {}
        self.seq = 0
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._worker_loop, name="beam-worker", daemon=True)
        self.recent_cache_time = 0.0
        self.recent_cache: list[dict[str, Any]] | None = None
        self.health_checked = 0.0
        self.health: dict[str, Any] = {"ok": False, "error": "not checked"}
        self._check_health()

    # ---------- lifecycle ----------
    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.queue.put_nowait((999, 10**9, {"type": "stop"}))
        except Exception:
            pass
        self.worker.join(timeout=5)

    # ---------- health & snapshots ----------
    def _check_health(self) -> None:
        now = time.time()
        if now - self.health_checked < 30:
            return
        self.health_checked = now
        try:
            self.health = {"ok": True, "version": api.api_version(self.cfg)}
        except Exception as exc:
            self.health = {"ok": False, "error": str(exc)}

    def _recent(self) -> list[dict[str, Any]]:
        now = time.time()
        if self.recent_cache is not None and now - self.recent_cache_time < 3:
            return self.recent_cache
        cards = []
        for item in pipeline.index_items(self.cfg):
            card = render.index_card(item, self.cfg)
            if not card:
                continue
            card["href"] = "/" + card["href"]
            card["cover"] = "/" + card["cover"] if card["cover"] else ""
            cards.append(card)
        cards.sort(key=lambda card: card["created_at"], reverse=True)
        self.recent_cache = cards[:8]
        self.recent_cache_time = now
        return self.recent_cache

    def _invalidate_recent(self) -> None:
        self.recent_cache = None
        self.recent_cache_time = 0.0

    def snapshot(self) -> dict[str, Any]:
        self._check_health()
        with self.lock:
            jobs = [job.to_dict() for job in self.jobs.values()]
        jobs.sort(key=lambda job: job["created_at"], reverse=True)
        queued = sum(1 for job in jobs if job["status"] == "queued")
        running = sum(1 for job in jobs if job["status"] == "running")
        counts = pipeline.status(self.cfg)
        return {
            "ok": True,
            "version": APP_VERSION,
            "health": self.health,
            "queue": {"queued": queued, "running": running, "active": queued + running},
            "jobs": jobs[:30],
            "recent": self._recent(),
            "counts": counts,
            "limits": {
                "max_collection_sources": int(self.cfg.get("collections", {}).get("max_sources", 12)),
                "max_tabs": MAX_TABS,
            },
        }

    # ---------- job creation ----------
    def _new_job(self, kind: str, title: str, path: Path | None = None, source_count: int = 0) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:10],
            kind=kind,
            title=title,
            path=str(path) if path else "",
            source_count=source_count,
        )
        with self.lock:
            self.jobs[job.id] = job
        return job

    def _enqueue(self, job: Job, item: dict[str, Any], priority: int) -> None:
        with self.lock:
            self.seq += 1
            sequence = self.seq
        self.queue.put((priority, sequence, {**item, "job_id": job.id}))

    def _update(self, job: Job, **fields: Any) -> None:
        with self.lock:
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = now_iso()
        if fields.get("href") is not None or fields.get("status") in {"done", "failed"}:
            self._invalidate_recent()

    def _queue_resource(self, path: Path, title: str, priority: int = 1) -> Job:
        job = self._new_job("resource", title=title, path=path)
        with self.lock:
            self.managed_paths.add(str(path))
        self._enqueue(job, {"type": "resource", "path": str(path)}, priority)
        return job

    # ---------- submissions ----------
    def submit_links(self, links: list[str], priority: int = 1) -> list[Job]:
        jobs = []
        for url in _clean_links(links):
            destination = unique_path(INBOX, _link_stem(url), ".url")
            atomic_write_text(destination, url + "\n")
            jobs.append(self._queue_resource(destination, url, priority))
        return jobs

    def submit_files(self, files: list[dict[str, Any]], priority: int = 1) -> list[Job]:
        prepared: list[tuple[str, bytes]] = []
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("Each file must include a name and data.")
            name = safe_filename(str(item.get("name") or "upload"))
            encoded = str(item.get("data") or "")
            if not encoded:
                raise ValueError(f"File {name} had no data.")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise ValueError(f"File {name} was not valid base64.") from exc
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError(f"File {name} is larger than the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.")
            prepared.append((name, raw))
        jobs = []
        for name, raw in prepared:
            suffix = Path(name).suffix.lower() or ".txt"
            destination = unique_path(INBOX, slugify(Path(name).stem), suffix)
            destination.write_bytes(raw)
            jobs.append(self._queue_resource(destination, Path(name).stem, priority))
        return jobs

    def submit_mixed(self, links: list[str], files: list[dict[str, Any]]) -> list[Job]:
        # Validate files before queueing anything, so a bad upload does not leave a partial batch.
        links = _clean_links(links)
        if not links and not files:
            raise ValueError("Add at least one link or file.")
        if files:
            # submit_files validates again; decode once here for an early failure.
            for item in files:
                encoded = str(item.get("data") or "") if isinstance(item, dict) else ""
                if not encoded:
                    raise ValueError("A dropped file had no data.")
                try:
                    base64.b64decode(encoded, validate=True)
                except Exception as exc:
                    raise ValueError("A dropped file was not valid base64.") from exc
        jobs = self.submit_links(links, priority=1) if links else []
        if files:
            jobs.extend(self.submit_files(files, priority=1))
        return jobs

    def submit_tabs(self, text: str) -> list[Job]:
        urls = _dedupe(extract_urls(text))
        if not urls:
            raise ValueError("No URLs found in that tab dump.")
        if len(urls) > MAX_TABS:
            raise ValueError(f"That is {len(urls)} tabs; the limit is {MAX_TABS} at a time.")
        return self.submit_links(urls, priority=2)

    def submit_collection(self, links: list[str], prompt: str) -> Job:
        urls = _clean_links(links)
        if not urls:
            raise ValueError("Add at least one source link.")
        maximum = int(self.cfg.get("collections", {}).get("max_sources", 12))
        if len(urls) > maximum:
            raise ValueError(f"Collections are limited to {maximum} sources.")
        job = self._new_job(
            "collection",
            title=f"Collection \u00b7 {len(urls)} source{'s' if len(urls) != 1 else ''}",
            source_count=len(urls),
        )
        self._enqueue(job, {"type": "collection", "urls": urls, "prompt": prompt}, priority=0)
        return job

    # ---------- worker ----------
    def _scan_inbox(self) -> None:
        try:
            entries = [
                path
                for path in INBOX.iterdir()
                if path.is_file()
                and not path.name.startswith(".")
                and path.suffix.lower() not in {".tmp", ".part", ".crdownload"}
            ]
        except OSError:
            return
        now = time.time()
        settle = float(self.cfg.get("watch", {}).get("settle_seconds", 1.2))
        live_names: set[str] = set()
        for path in entries:
            live_names.add(path.name)
            key = str(path)
            with self.lock:
                if key in self.managed_paths:
                    continue
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = self.pending_external.get(path.name)
            if previous == signature and (now - stat.st_mtime) >= settle:
                self.pending_external.pop(path.name, None)
                self._queue_resource(path, path.stem, priority=1)
            else:
                self.pending_external[path.name] = signature
        for name in list(self.pending_external):
            if name not in live_names:
                self.pending_external.pop(name, None)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            self._scan_inbox()
            try:
                _, _, item = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item.get("type") == "stop":
                break
            job = self.jobs.get(str(item.get("job_id")))
            if job is None:
                continue
            self._update(job, status="running", message="Starting\u2026")
            try:
                if item.get("type") == "resource":
                    self._run_resource(job, Path(str(item["path"])))
                elif item.get("type") == "collection":
                    self._run_collection(job, item)
            except Exception as exc:
                self._update(job, status="failed", message=f"{type(exc).__name__}: {exc}")
                self.processor.log(f"job {job.id} failed: {type(exc).__name__}: {exc}", "ERROR")
            finally:
                with self.lock:
                    self.managed_paths.discard(str(item.get("path", "")))
                self._invalidate_recent()

    def _run_resource(self, job: Job, path: Path) -> None:
        result = self.processor.process(
            path,
            on_progress=lambda message: self._update(job, message=message),
        )
        if not result:
            self._update(
                job,
                status="failed",
                message="Processing failed. Check failed/ and logs/beam.log for details.",
            )
            return
        href = _relative_url(result.get("html", ""))
        self._update(
            job,
            status="done",
            title=result.get("title") or job.title,
            href=href,
            category=str(result.get("category") or ""),
            category_label=str(result.get("category_label") or ""),
            message="",
        )

    def _run_collection(self, job: Job, item: dict[str, Any]) -> None:
        result = collection.build_collection(
            self.cfg,
            list(item.get("urls") or []),
            str(item.get("prompt") or ""),
            self.processor,
            on_progress=lambda message: self._update(job, message=message),
        )
        href = _relative_url(result.get("html", ""))
        self._update(
            job,
            status="done",
            title=result.get("title") or job.title,
            href=href,
            category="collection",
            category_label="Collection",
            message="",
        )


def _relative_url(path_value: str) -> str:
    if not path_value:
        return ""
    try:
        relative = Path(path_value).resolve().relative_to(ROOT.resolve())
        return "/" + relative.as_posix()
    except (ValueError, OSError):
        return ""


class BeamHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    server_version = f"Beam/{APP_VERSION}"

    @property
    def state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    # ---------- responses ----------
    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200, cache: str = "no-cache") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_error(404, "File not found.")
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        suffix = path.suffix.lower()
        cache = "public, max-age=3600" if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"} else "no-cache"
        self._send_bytes(body, content_type, cache=cache)

    def _send_error(self, status: int, message: str) -> None:
        if self.path.startswith("/api/"):
            self._send_json({"ok": False, "error": message}, status=status)
        else:
            self._send_bytes(message.encode("utf-8"), "text/plain; charset=utf-8", status=status)

    def _resolve(self, relative: str, allowed: list[str] | None = None) -> Path | None:
        root = ROOT.resolve()
        try:
            candidate = (root / relative).resolve()
        except (OSError, ValueError):
            return None
        if candidate != root and root not in candidate.parents:
            return None
        if allowed is not None:
            allowed_roots = [(root / prefix).resolve() for prefix in allowed]
            if not any(candidate == path or path in candidate.parents for path in allowed_roots):
                return None
        return candidate

    # ---------- GET ----------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/status":
            self._send_json(self.state.snapshot())
            return
        if path == "/api/health":
            self._send_json({"ok": True, "health": self.state.health, "version": APP_VERSION})
            return
        if path in {"/", "/app", "/app/"}:
            self._send_file(WEB_DIR / "index.html")
            return
        if path.startswith("/web/"):
            candidate = self._resolve(path.lstrip("/"), allowed=["web"])
            if candidate and candidate.is_file():
                self._send_file(candidate)
            else:
                self._send_error(404, "Not found.")
            return
        if path == "/theme.css":
            self._send_file(ROOT / "theme.css")
            return
        if path == "/index.html":
            self._send_file(ROOT / "index.html")
            return
        if path.startswith(("/sorted/", "/collections/")):
            relative = path.lstrip("/")
            candidate = self._resolve(relative, allowed=["sorted", "collections"])
            if candidate and candidate.is_dir():
                if not path.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", path + "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                candidate = candidate / "index.html"
            if candidate and candidate.is_file():
                self._send_file(candidate)
            else:
                self._send_error(404, "Not found.")
            return
        if path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", status=204)
            return
        self._send_error(404, "Not found.")

    # ---------- POST ----------
    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path != "/api/submit":
            self._send_error(404, "Not found.")
            return
        try:
            payload = self._read_json()
            result = self._handle_submit(payload)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        self._send_json(result)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length <= 0:
            raise ValueError("Empty request body.")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"Request is larger than the {MAX_BODY_BYTES // (1024 * 1024)} MB limit.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _handle_submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "").strip()
        if mode == "mixed":
            links = payload.get("links") if isinstance(payload.get("links"), list) else []
            links += extract_urls(str(payload.get("text") or ""))
            files = payload.get("files") if isinstance(payload.get("files"), list) else []
            jobs = self.state.submit_mixed([str(link) for link in links], files)
            return {"ok": True, "queued": len(jobs), "jobs": [job.to_dict() for job in jobs]}
        if mode == "tabs":
            text = str(payload.get("text") or "")
            jobs = self.state.submit_tabs(text)
            return {
                "ok": True,
                "queued": len(jobs),
                "urls": [job.title for job in jobs],
                "jobs": [job.to_dict() for job in jobs],
            }
        if mode == "collection":
            links = payload.get("links") if isinstance(payload.get("links"), list) else []
            links += extract_urls(str(payload.get("text") or ""))
            prompt = str(payload.get("prompt") or "").strip()
            job = self.state.submit_collection([str(link) for link in links], prompt)
            return {"ok": True, "queued": 1, "job": job.to_dict()}
        raise ValueError("Unknown submit mode.")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- logging ----------
    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/status"):
            return
        if self.command in {"GET", "HEAD"} and not self.path.startswith("/api/"):
            return
        print(f"{now_iso()} {self.command} {self.path}: {fmt % args}", flush=True)


def _acquire_lock() -> Any:
    if fcntl is None:
        return None
    handle = (ROOT / ".beam.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit("another Beam watcher or web app is already running in this folder")
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def run_server(cfg: dict[str, Any], host: str | None = None, port: int | None = None, open_browser: bool = False) -> None:
    ensure_dirs()
    host = host or str(cfg.get("server", {}).get("host", "127.0.0.1"))
    port = int(port or cfg.get("server", {}).get("port", 8765))
    lock_handle = _acquire_lock()
    state = AppState(cfg)
    try:
        server = BeamHTTPServer((host, port), RequestHandler)
    except OSError as exc:
        if lock_handle is not None:
            lock_handle.close()
        raise SystemExit(f"could not start Beam on {host}:{port}: {exc}") from exc
    server.app_state = state  # type: ignore[attr-defined]
    state.start()
    url = f"http://{host}:{port}/"
    print(f"Beam app: {url}")
    print(f"Watching: {INBOX}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping\u2026")
    finally:
        state.stop()
        server.server_close()
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lock_handle.close()
