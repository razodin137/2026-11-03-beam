"""The Beam processing pipeline: claim → acquire → classify → route → extract → render."""

from __future__ import annotations

import json
import re
import shutil
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from . import acquire, api, prompts, render
from .config import (
    COLLECTIONS,
    FAILED,
    INBOX,
    LOGS,
    PROCESSING,
    ROOT,
    SORTED,
    category_keys,
    category_meta,
    ensure_dirs,
    sorted_dir,
)
from .util import (
    atomic_write_text,
    local_stamp,
    normalize_for_match,
    now_iso,
    quote_match,
    slugify,
    smart_truncate,
    unique_path,
)

PREMIUM_CATEGORIES = {"research", "code", "finance", "health", "energy"}


def _usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Keep token counts, not the whole raw completion, in saved metadata."""
    return {key: value for key, value in usage.items() if key != "content"}


class Processor:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        ensure_dirs()

    # ---------- logging ----------
    def log(self, message: str, level: str = "INFO") -> None:
        line = f"{now_iso()} {level:<5} {message}"
        print(line, flush=True)
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / "beam.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # ---------- model calls ----------
    def _categories_text(self) -> str:
        lines = []
        for key, meta in self.cfg["categories"].items():
            lines.append(f"- {key}: {meta.get('label')} \u2014 {meta.get('blurb')}")
        return "\n".join(lines)

    def classify(self, resource: acquire.Resource) -> dict[str, Any]:
        cfg = self.cfg
        model = str(cfg["models"]["decision"])
        preview = resource.preview(int(cfg["limits"]["preview_chars"]))
        prompt = prompts.render("classify", CATEGORIES=self._categories_text(), SOURCE=preview)
        data, usage = api.chat_json(
            cfg,
            model,
            [{"role": "user", "content": prompt}],
            num_predict=int(cfg["models"].get("decision_num_predict", 500)),
            temperature=0,
        )
        if not isinstance(data, dict):
            data = {}

        category = str(data.get("category") or "").strip().lower()
        if category not in cfg["categories"]:
            for key in cfg["categories"]:
                if key in category:
                    category = key
                    break
            else:
                category = "other"
        title = str(data.get("title") or resource.title or "Untitled brief").strip()[:120]
        slug = slugify(str(data.get("slug") or title or resource.title or "brief"))
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.0

        return {
            "category": category,
            "category_label": category_meta(cfg, category).get("label", category),
            "title": title,
            "slug": slug,
            "confidence": confidence,
            "rationale": str(data.get("rationale") or "").strip(),
            "model": model,
            "usage": _usage(usage),
        }

    def route(self, resource: acquire.Resource, classification: dict[str, Any]) -> dict[str, Any]:
        cfg = self.cfg
        decision_model = str(cfg["models"]["decision"])
        premium = str(cfg["models"]["premium"])
        cheap = str(cfg["models"]["cheap"])
        preview = resource.preview(int(cfg["limits"]["route_preview_chars"]))
        prompt = prompts.render(
            "route",
            CHEAP_MODEL=cheap,
            PREMIUM_MODEL=premium,
            SOURCE=f"Classified as: {classification['category']}\n\n{preview}",
        )
        data, usage = api.chat_json(
            cfg,
            decision_model,
            [{"role": "user", "content": prompt}],
            num_predict=int(cfg["models"].get("decision_num_predict", 500)),
            temperature=0,
        )
        if not isinstance(data, dict):
            data = {}

        tier = str(data.get("tier") or "").strip().lower()
        if tier not in {"cheap", "premium"}:
            tier = self._heuristic_tier(resource, classification)
        effort = str(data.get("effort") or "").strip().lower()
        if effort not in {"low", "medium", "high"}:
            effort = "low" if tier == "cheap" else "high"

        return {
            "tier": tier,
            "model": cheap if tier == "cheap" else premium,
            "reason": str(data.get("reason") or "").strip(),
            "effort": effort,
            "decider": decision_model,
            "usage": _usage(usage),
        }

    @staticmethod
    def _heuristic_tier(resource: acquire.Resource, classification: dict[str, Any]) -> str:
        if resource.kind in {"youtube", "pdf", "docx"}:
            return "premium"
        if len(resource.text) > 14_000:
            return "premium"
        if classification.get("category") in PREMIUM_CATEGORIES and len(resource.text) > 3_500:
            return "premium"
        return "cheap"

    def extract(self, resource: acquire.Resource, routing: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        cfg = self.cfg
        model = str(routing["model"])
        source = resource.preview(int(cfg["limits"]["max_source_chars"]))
        prompt = prompts.render(
            "extract",
            MAX_QUOTES=int(cfg["limits"].get("max_quotes", 10)),
            SOURCE=source,
        )
        data, usage = api.chat_json(
            cfg,
            model,
            [{"role": "user", "content": prompt}],
            num_predict=int(cfg["models"].get("extract_num_predict", 8000)),
            temperature=float(cfg["models"].get("temperature", 0.2)),
        )
        return self._normalize_extraction(data, resource), usage

    def _normalize_extraction(self, data: Any, resource: acquire.Resource) -> dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        max_quotes = int(self.cfg["limits"].get("max_quotes", 10))

        def string(value: Any) -> str:
            if isinstance(value, list):
                return "\n\n".join(str(item).strip() for item in value if str(item).strip())
            return str(value or "").strip()

        quotes: list[dict[str, Any]] = []
        raw_quotes = data.get("quotes")
        if isinstance(raw_quotes, list):
            for item in raw_quotes:
                if not isinstance(item, dict):
                    continue
                text = string(item.get("text"))
                if not text:
                    continue
                timestamp = string(item.get("timestamp"))
                if resource.kind != "youtube":
                    timestamp = ""
                quotes.append(
                    {
                        "text": text,
                        "context": string(item.get("context")),
                        "speaker": string(item.get("speaker")) or resource.author or resource.site,
                        "timestamp": timestamp,
                        "why": string(item.get("why")),
                    }
                )
                if len(quotes) >= max_quotes:
                    break

        key_points: list[dict[str, str]] = []
        raw_points = data.get("key_points")
        if isinstance(raw_points, list):
            for item in raw_points:
                if not isinstance(item, dict):
                    continue
                label = string(item.get("label"))
                value = string(item.get("value"))
                if not label and not value:
                    continue
                key_points.append({"label": label, "value": value, "detail": string(item.get("detail"))})
                if len(key_points) >= 6:
                    break

        sections: list[dict[str, Any]] = []
        raw_sections = data.get("sections")
        if isinstance(raw_sections, list):
            for item in raw_sections:
                if not isinstance(item, dict):
                    continue
                heading = string(item.get("heading"))
                body = string(item.get("body"))
                if not heading and not body:
                    continue
                bullets = item.get("bullets")
                if not isinstance(bullets, list):
                    bullets = []
                sections.append(
                    {
                        "heading": heading or "Details",
                        "body": body,
                        "bullets": [str(bullet).strip() for bullet in bullets if str(bullet).strip()][:8],
                    }
                )
                if len(sections) >= 6:
                    break

        actions = data.get("actions")
        if not isinstance(actions, list):
            actions = []
        tags = data.get("tags")
        if not isinstance(tags, list):
            tags = []

        try:
            reading_time = max(1, int(data.get("reading_time_minutes") or 0))
        except (TypeError, ValueError):
            reading_time = 0
        if not reading_time:
            reading_time = max(3, round(len(string(data.get("summary"))) / 1000) + 3)

        return {
            "title": string(data.get("title"))[:120] or resource.title or "Untitled brief",
            "standfirst": string(data.get("standfirst"))[:300] or resource.description[:220],
            "summary": string(data.get("summary")),
            "quotes": quotes,
            "key_points": key_points,
            "sections": sections,
            "why_it_matters": string(data.get("why_it_matters")),
            "actions": [str(action).strip() for action in actions if str(action).strip()][:6],
            "tags": [str(tag).strip().lstrip("#") for tag in tags if str(tag).strip()][:10],
            "reading_time_minutes": reading_time,
            "language": string(data.get("language")) or resource.language or "en",
            "cover_alt": string(data.get("cover_alt")),
            "warning": string(data.get("warning")),
        }

    @staticmethod
    def verify_quotes(content: dict[str, Any], source_text: str) -> None:
        verification_source = re.sub(r"\[\d{1,2}:\d{2}:\d{2}\]", " ", source_text or "")
        verification_source = re.sub(r"^#{1,6}\s*", "", verification_source, flags=re.MULTILINE)
        normalized = normalize_for_match(verification_source)
        for quote in content.get("quotes", []):
            ratio, verified = quote_match(normalized, quote.get("text", ""))
            quote["verified"] = verified
            quote["match_ratio"] = ratio

    # ---------- filesystem ----------
    def claim(self, path: Path) -> Path:
        source = Path(path)
        if source.parent.resolve() == PROCESSING.resolve():
            return source
        ensure_dirs()
        destination = unique_path(PROCESSING, slugify(source.stem), source.suffix)
        shutil.move(str(source), destination)
        return destination

    def move_to_failed(self, claimed: Path, exc: BaseException) -> None:
        try:
            destination = unique_path(FAILED, f"{slugify(claimed.stem)}-{local_stamp()}", claimed.suffix)
            if claimed.exists():
                shutil.move(str(claimed), destination)
            else:
                destination = FAILED / f"{slugify(claimed.stem)}-{local_stamp()}.missing"
            (destination.with_name(destination.name + ".error.txt")).write_text(
                f"{now_iso()} {type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
                encoding="utf-8",
            )
        except Exception:
            self.log("could not move failed item; see logs/beam.log", "ERROR")

    def regenerate_index(self) -> Path:
        ensure_dirs()
        output = ROOT / "index.html"
        atomic_write_text(output, render.render_index(index_items(self.cfg), self.cfg))
        return output

    def rebuild(self) -> int:
        """Re-render every saved brief after a theme or template change."""
        ensure_dirs()
        count = 0
        for meta_path in sorted(SORTED.glob("*/*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
            html_name = files.get("html")
            if not html_name or not isinstance(meta.get("content"), dict):
                continue
            try:
                page = render.render_page(meta, self.cfg, index_href="../../index.html")
            except Exception as exc:
                self.log(f"could not re-render {meta_path.name}: {type(exc).__name__}: {exc}", "ERROR")
                continue
            atomic_write_text(meta_path.parent / html_name, page)
            count += 1
        for meta_path in sorted(COLLECTIONS.glob("*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
            html_name = files.get("html", "index.html")
            if not isinstance(meta.get("content"), dict):
                continue
            try:
                page = render.render_collection(meta, self.cfg, index_href="../../index.html")
            except Exception as exc:
                self.log(f"could not re-render {meta_path.parent.name}: {type(exc).__name__}: {exc}", "ERROR")
                continue
            atomic_write_text(meta_path.parent / html_name, page)
            count += 1
        self.regenerate_index()
        return count

    # ---------- main ----------
    def process(self, path: Path, on_progress: Callable[[str], None] | None = None) -> dict[str, Any] | None:
        started = time.time()
        claimed = self.claim(path)
        self.log(f"claimed {claimed.name}")

        def note(message: str) -> None:
            if on_progress:
                try:
                    on_progress(message)
                except Exception:
                    pass

        try:
            note("Reading the source\u2026")
            resource = acquire.acquire(claimed, self.cfg)
            if not resource.text.strip():
                raise acquire.AcquireError("no readable text could be extracted from this source")

            self.log(f"acquired {resource.kind} source ({len(resource.text):,} chars): {resource.title or claimed.name}")
            note("Classifying\u2026")
            classification = self.classify(resource)
            self.log(
                f"classified as {classification['category']} "
                f"(confidence {classification['confidence']:.2f}): {resource.title or claimed.name}"
            )
            note("Choosing a model\u2026")
            routing = self.route(resource, classification)
            self.log(f"routing chose {routing['tier']} model: {routing['model']}")

            used_text, truncated = smart_truncate(resource.text, int(self.cfg["limits"]["max_source_chars"]))
            model_resource = replace(resource, text=used_text)
            note(f"Extracting with {routing['model']}\u2026")
            content, extraction_usage = self.extract(model_resource, routing)
            self.verify_quotes(content, resource.text)
            note("Rendering the brief\u2026")

            slug = slugify(classification.get("slug") or content.get("title") or resource.title or claimed.stem)
            category = classification["category"]
            destination_dir = sorted_dir(self.cfg, category)

            suffix = claimed.suffix.lower() or ".txt"
            stem = slug
            source_stem = f"{stem}-source" if suffix in {".html", ".htm", ".json"} else stem
            index = 2
            while (
                any((destination_dir / f"{stem}{name}").exists() for name in {suffix, ".html", ".json"})
                or (destination_dir / f"{source_stem}{suffix}").exists()
            ):
                stem = f"{slug}-{index}"
                source_stem = f"{stem}-source" if suffix in {".html", ".htm", ".json"} else stem
                index += 1

            source_path = destination_dir / f"{source_stem}{suffix}"
            html_path = destination_dir / f"{stem}.html"
            meta_path = destination_dir / f"{stem}.json"

            reading_copy_name = ""
            duplicate_text_source = suffix in {".txt", ".md", ".markdown", ".rst", ".csv", ".json"} and resource.kind in {"text", "note"}
            if not duplicate_text_source:
                reading_copy_path = destination_dir / f"{stem}-reading-copy.txt"
                snapshot = resource.text
                if len(snapshot) > 1_000_000:
                    snapshot = snapshot[:1_000_000] + "\n\n[reading copy truncated at 1,000,000 characters]"
                atomic_write_text(reading_copy_path, snapshot)
                reading_copy_name = reading_copy_path.name

            cover_name = ""
            if resource.thumbnail:
                cover_name = acquire.download_image(resource.thumbnail, self.cfg, destination_dir / stem)
                if not cover_name and resource.kind == "youtube" and resource.extra.get("video_id"):
                    fallback = f"https://i.ytimg.com/vi/{resource.extra['video_id']}/hqdefault.jpg"
                    cover_name = acquire.download_image(fallback, self.cfg, destination_dir / stem)

            verified_count = sum(1 for quote in content.get("quotes", []) if quote.get("verified"))
            total_quotes = len(content.get("quotes", []))
            if not total_quotes:
                resource.warnings.append("No verbatim quotes were extracted; this brief is based on the model's summary of the source.")
            elif verified_count == 0:
                resource.warnings.append("None of the extracted quotes matched the source verbatim; check the original before sharing.")
            elif verified_count < total_quotes:
                resource.warnings.append(
                    f"{total_quotes - verified_count} extracted quote(s) could not be matched verbatim; only verified quotes are marked."
                )
            meta: dict[str, Any] = {
                "schema": 1,
                "id": stem,
                "created_at": now_iso(),
                "source": {
                    "kind": resource.kind,
                    "title": resource.title,
                    "url": resource.url,
                    "author": resource.author,
                    "published": resource.published,
                    "site": resource.site,
                    "duration": resource.duration,
                    "language": resource.language,
                    "thumbnail": resource.thumbnail,
                    "warnings": resource.warnings,
                    "extra": resource.extra,
                },
                "classification": {
                    "category": category,
                    "label": classification["category_label"],
                    "confidence": classification["confidence"],
                    "rationale": classification["rationale"],
                    "model": classification["model"],
                    "usage": classification["usage"],
                },
                "routing": {
                    "tier": routing["tier"],
                    "model": routing["model"],
                    "reason": routing["reason"],
                    "effort": routing["effort"],
                    "decider": routing["decider"],
                    "usage": routing["usage"],
                },
                "extraction": {
                    "model": extraction_usage.get("model", routing["model"]),
                    "prompt_tokens": extraction_usage.get("prompt_tokens", 0),
                    "completion_tokens": extraction_usage.get("completion_tokens", 0),
                    "duration_ms": extraction_usage.get("duration_ms", 0),
                },
                "files": {
                    "source": source_path.name,
                    "html": html_path.name,
                    "meta": meta_path.name,
                    "cover": cover_name,
                    "reading_copy": reading_copy_name,
                },
                "stats": {
                    "source_chars": len(resource.text),
                    "used_chars": len(used_text),
                    "truncated": truncated,
                    "quotes": len(content.get("quotes", [])),
                    "quotes_verified": verified_count,
                },
                "content": content,
                "truncated": truncated,
                "total_ms": int((time.time() - started) * 1000),
            }

            atomic_write_text(html_path, render.render_page(meta, self.cfg, index_href="../../index.html"))
            atomic_write_text(meta_path, json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
            shutil.move(str(claimed), source_path)
            self.regenerate_index()

            self.log(
                f"sorted → sorted/{category}/{html_path.name} "
                f"({len(content.get('quotes', []))} quotes, {verified_count} verified, {meta['total_ms']} ms)",
                "DONE",
            )
            return {
                "ok": True,
                "kind": "resource",
                "id": stem,
                "meta": meta,
                "html": str(html_path),
                "category": category,
                "category_label": classification["category_label"],
                "title": content.get("title") or resource.title or stem,
                "cover": cover_name,
            }
        except Exception as exc:
            self.log(f"failed {claimed.name}: {type(exc).__name__}: {exc}", "ERROR")
            self.move_to_failed(claimed, exc)
            try:
                self.regenerate_index()
            except Exception:
                pass
            return None


def index_items(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    ensure_dirs()
    for meta_path in sorted(SORTED.glob("*/*.json")):
        if meta_path.name.startswith("."):
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
        html_name = files.get("html")
        if not html_name or not (meta_path.parent / html_name).exists():
            continue
        items.append(meta)
    for meta_path in sorted(COLLECTIONS.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("kind") != "collection":
            continue
        files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
        html_name = files.get("html", "index.html")
        if not (meta_path.parent / html_name).exists():
            continue
        items.append(meta)
    return items


def status(cfg: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    counts: dict[str, int] = {}
    for key in category_keys(cfg):
        directory = SORTED / key
        counts[key] = len(list(directory.glob("*.html"))) if directory.exists() else 0
    collections_count = len([path for path in COLLECTIONS.glob("*/index.html")]) if COLLECTIONS.exists() else 0
    return {
        "inbox": len([path for path in INBOX.iterdir() if path.is_file() and not path.name.startswith(".")]),
        "processing": len([path for path in PROCESSING.iterdir() if path.is_file() and not path.name.startswith(".")]),
        "collections": collections_count,
        "failed": len(
            [
                path
                for path in FAILED.iterdir()
                if path.is_file() and not path.name.startswith(".") and not path.name.endswith(".error.txt")
            ]
        )
        if FAILED.exists()
        else 0,
        "categories": counts,
    }
