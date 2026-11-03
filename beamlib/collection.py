"""Build one synthesized HTML page from several sources."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import acquire, api, prompts, render
from .config import COLLECTIONS, ensure_dirs
from .util import (
    atomic_write_text,
    human_duration,
    normalize_for_match,
    now_iso,
    quote_match,
    slugify,
    smart_truncate,
    unique_path,
)

Progress = Callable[[str], None]


class CollectionError(RuntimeError):
    pass


def _string(value: Any) -> str:
    if isinstance(value, list):
        return "\n\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _strings(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


def _source_id(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _verification_text(value: str) -> str:
    text = re.sub(r"\[\d{1,2}:\d{2}:\d{2}\]", " ", value or "")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    return normalize_for_match(text)


def build_collection(
    cfg: dict[str, Any],
    urls: list[str],
    request: str,
    processor: Any,
    on_progress: Progress | None = None,
) -> dict[str, Any]:
    started = time.time()
    ensure_dirs()

    def progress(message: str) -> None:
        processor.log(f"collection: {message}")
        if on_progress:
            on_progress(message)

    resources: list[acquire.Resource] = []
    warnings: list[str] = []
    for index, url in enumerate(urls, start=1):
        progress(f"fetching source {index}/{len(urls)} \u2014 {url}")
        try:
            resource = acquire.acquire_url(url, cfg)
        except Exception as exc:
            warnings.append(f"Could not read source {index} ({url}): {exc}")
            continue
        if not resource.text.strip():
            warnings.append(f"Source {index} ({url}) had no readable text.")
            continue
        resources.append(resource)

    if not resources:
        raise CollectionError("None of the sources could be read. Check the links and try again.")

    max_total = int(cfg.get("limits", {}).get("max_source_chars", 160_000))
    collection_cfg = cfg.get("collections", {})
    per_source = int(collection_cfg.get("per_source_chars", max(6000, max_total // max(1, len(resources)))))
    per_source = max(4000, min(per_source, max_total))
    max_quotes = int(cfg.get("limits", {}).get("max_quotes", 10))

    source_records: list[dict[str, Any]] = []
    blocks: list[str] = []
    truncated_any = False
    for index, resource in enumerate(resources, start=1):
        text, truncated = smart_truncate(resource.text, per_source)
        truncated_any = truncated_any or truncated
        source_id = str(index)
        record = {
            "id": source_id,
            "title": resource.title or resource.url or f"Source {index}",
            "url": resource.url or urls[index - 1],
            "kind": resource.kind,
            "author": resource.author,
            "site": resource.site,
            "published": resource.published,
            "duration": resource.duration,
            "thumbnail": resource.thumbnail,
            "warnings": list(resource.warnings),
            "extra": resource.extra,
            "language": resource.language,
            "text": resource.text,
        }
        source_records.append(record)
        heading_bits = [
            f"===== SOURCE {source_id} =====",
            f"URL: {record['url']}",
            f"Title: {record['title']}",
            f"Author: {record['author'] or '(unknown)'}",
            f"Site: {record['site'] or '(unknown)'}",
            f"Published: {record['published'] or '(unknown)'}",
        ]
        if record["duration"]:
            heading_bits.append(f"Duration: {human_duration(record['duration'])}")
        blocks.append("\n".join(heading_bits) + "\n---\n" + text)

    combined = "\n\n".join(blocks)
    request_text = request.strip() or "Build a one-page summary of these sources, with the strongest quotes and a clear synthesis."

    premium_model = str(cfg["models"]["premium"])
    cheap_model = str(cfg["models"]["cheap"])
    collection_model = str(collection_cfg.get("model", "premium")).lower()
    if collection_model == "auto":
        synthetic = acquire.Resource(
            kind="collection",
            title=f"{len(resources)} sources",
            description=request_text,
            text=combined,
            language=resources[0].language or "en",
        )
        routing = processor.route(synthetic, {"category": "research"})
        progress(f"routing chose the {routing['tier']} model")
    else:
        tier = "cheap" if collection_model == "cheap" else "premium"
        routing = {
            "tier": tier,
            "model": cheap_model if tier == "cheap" else premium_model,
            "reason": "Collection mode uses the configured multi-source model.",
            "effort": "high",
            "decider": str(cfg["models"]["decision"]),
            "usage": {},
        }
    progress(f"writing the collection brief with {routing['model']}")

    prompt = prompts.render(
        "collection",
        REQUEST=request_text,
        SOURCES=combined,
        MAX_QUOTES=max_quotes,
    )
    data, usage = api.chat_json(
        cfg,
        str(routing["model"]),
        [{"role": "user", "content": prompt}],
        num_predict=int(cfg["models"].get("extract_num_predict", 8000)),
        temperature=float(cfg["models"].get("temperature", 0.2)),
    )
    if not isinstance(data, dict):
        data = {}

    content = _normalize_content(data, source_records, max_quotes)

    slug = slugify(_string(data.get("slug")) or content["title"] or "collection")
    base = COLLECTIONS / slug
    index = 2
    while base.exists():
        base = COLLECTIONS / f"{slug}-{index}"
        index += 1
    base.mkdir(parents=True, exist_ok=True)

    sources_dir = base / "sources"
    sources_dir.mkdir(exist_ok=True)
    for record in source_records:
        reading_copy = unique_path(sources_dir, f"{int(record['id']):02d}-{slugify(record['title'])}", ".txt")
        snapshot = record["text"]
        if len(snapshot) > 1_000_000:
            snapshot = snapshot[:1_000_000] + "\n\n[reading copy truncated at 1,000,000 characters]"
        header = (
            f"URL: {record['url']}\n"
            f"Title: {record['title']}\n"
            f"Author: {record['author']}\n"
            f"Site: {record['site']}\n"
            f"Published: {record['published']}\n"
            f"Kind: {record['kind']}\n"
            "---\n\n"
        )
        atomic_write_text(reading_copy, header + snapshot)
        content["sources"][int(record["id"]) - 1]["reading_copy"] = f"sources/{reading_copy.name}"

    cover_name = ""
    for record in source_records:
        if not record["thumbnail"]:
            continue
        cover_name = acquire.download_image(record["thumbnail"], cfg, base / "hero")
        if not cover_name and record["kind"] == "youtube" and record["extra"].get("video_id"):
            fallback = f"https://i.ytimg.com/vi/{record['extra']['video_id']}/hqdefault.jpg"
            cover_name = acquire.download_image(fallback, cfg, base / "hero")
        if cover_name:
            break

    verified_count = sum(1 for quote in content["quotes"] if quote.get("verified"))
    total_quotes = len(content["quotes"])
    if truncated_any:
        warnings.append("One or more sources were longer than the per-source budget and were sampled.")
    if content.get("warning"):
        warnings.append(content["warning"])
    if not total_quotes:
        warnings.append("No verbatim quotes were extracted; the collection is based on the model's synthesis.")
    elif verified_count == 0:
        warnings.append("None of the extracted quotes matched their sources verbatim; check the originals before sharing.")
    elif verified_count < total_quotes:
        warnings.append(f"{total_quotes - verified_count} quote(s) could not be matched verbatim; only verified quotes are marked.")

    used_chars = sum(len(smart_truncate(record["text"], per_source)[0]) for record in source_records)
    meta: dict[str, Any] = {
        "schema": 1,
        "kind": "collection",
        "id": base.name,
        "created_at": now_iso(),
        "request": request_text,
        "source": {
            "kind": "collection",
            "title": content["title"],
            "url": "",
            "description": request_text,
            "warnings": warnings,
            "extra": {"urls": [record["url"] for record in source_records]},
        },
        "classification": {
            "category": "collection",
            "label": "Collection",
            "confidence": 1.0,
            "rationale": "Multi-source summary requested by the user.",
            "model": str(cfg["models"]["decision"]),
            "usage": {},
        },
        "routing": routing,
        "extraction": {
            "model": usage.get("model", routing["model"]),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "duration_ms": usage.get("duration_ms", 0),
        },
        "files": {
            "source": "",
            "html": "index.html",
            "meta": "meta.json",
            "cover": cover_name,
            "reading_copy": "",
        },
        "stats": {
            "source_chars": sum(len(record["text"]) for record in source_records),
            "used_chars": used_chars,
            "truncated": truncated_any,
            "sources": len(source_records),
            "quotes": total_quotes,
            "quotes_verified": verified_count,
        },
        "content": content,
        "truncated": truncated_any,
        "total_ms": int((time.time() - started) * 1000),
    }

    atomic_write_text(base / "index.html", render.render_collection(meta, cfg, index_href="../../index.html"))
    atomic_write_text(base / "meta.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    processor.regenerate_index()
    progress(
        f"built collections/{base.name}/index.html "
        f"({len(source_records)} sources, {total_quotes} quotes, {verified_count} verified)"
    )
    return {
        "ok": True,
        "kind": "collection",
        "id": base.name,
        "meta": meta,
        "html": str(base / "index.html"),
        "title": content["title"],
        "cover": cover_name,
    }


def _normalize_content(data: dict[str, Any], source_records: list[dict[str, Any]], max_quotes: int) -> dict[str, Any]:
    model_sources: dict[str, dict[str, Any]] = {}
    raw_sources = data.get("sources")
    if isinstance(raw_sources, list):
        for index, item in enumerate(raw_sources, start=1):
            if isinstance(item, dict):
                source_id = _source_id(item.get("id")) or str(index)
                model_sources[source_id] = item
        # If the model omitted IDs entirely, fall back to positional matching.
        if not any(_source_id(item.get("id")) for item in raw_sources if isinstance(item, dict)):
            model_sources = {str(index): item for index, item in enumerate(raw_sources, start=1) if isinstance(item, dict)}

    content_sources: list[dict[str, Any]] = []
    for record in source_records:
        model_source = model_sources.get(record["id"], {})
        content_sources.append(
            {
                "id": record["id"],
                "title": _string(model_source.get("title")) or record["title"],
                "url": record["url"],
                "kind": record["kind"],
                "author": record["author"],
                "site": record["site"],
                "published": record["published"],
                "duration": record["duration"],
                "role": _string(model_source.get("role")),
                "key_points": _strings(model_source.get("key_points"), 5),
                "reading_copy": "",
                "warnings": record["warnings"],
            }
        )

    themes: list[dict[str, Any]] = []
    for item in data.get("themes") if isinstance(data.get("themes"), list) else []:
        if not isinstance(item, dict):
            continue
        heading = _string(item.get("heading"))
        body = _string(item.get("body"))
        if not heading and not body:
            continue
        themes.append(
            {
                "heading": heading or "Theme",
                "body": body,
                "source_ids": [_source_id(value) for value in item.get("source_ids", []) if _source_id(value)]
                if isinstance(item.get("source_ids"), list)
                else [],
                "bullets": _strings(item.get("bullets"), 8),
            }
        )
        if len(themes) >= 5:
            break

    comparison: list[dict[str, Any]] = []
    for item in data.get("comparison") if isinstance(data.get("comparison"), list) else []:
        if not isinstance(item, dict):
            continue
        entries = []
        for entry in item.get("entries", []) if isinstance(item.get("entries"), list) else []:
            if not isinstance(entry, dict):
                continue
            source_id = _source_id(entry.get("source_id"))
            value = _string(entry.get("value"))
            if not source_id and not value:
                continue
            entries.append({"source_id": source_id, "value": value, "note": _string(entry.get("note"))})
        if entries:
            comparison.append({"dimension": _string(item.get("dimension")) or "Dimension", "entries": entries})
        if len(comparison) >= 5:
            break

    normalized = {record["id"]: _verification_text(record["text"]) for record in source_records}
    source_titles = {record["id"]: record["title"] for record in source_records}
    combined_normalized = " ".join(normalized.values())

    quotes: list[dict[str, Any]] = []
    for item in data.get("quotes") if isinstance(data.get("quotes"), list) else []:
        if not isinstance(item, dict):
            continue
        text = _string(item.get("text"))
        if not text:
            continue
        source_id = _source_id(item.get("source_id"))
        source_title_hint = _string(item.get("source_title"))
        if source_id not in normalized and source_title_hint:
            for candidate_id, title in source_titles.items():
                if source_title_hint.lower() in title.lower() or title.lower() in source_title_hint.lower():
                    source_id = candidate_id
                    break
        if source_id in normalized:
            ratio, verified = quote_match(normalized[source_id], text)
        else:
            best_id = ""
            best_ratio = 0.0
            for candidate_id, normalized_text in normalized.items():
                candidate_ratio, candidate_verified = quote_match(normalized_text, text)
                if candidate_ratio > best_ratio:
                    best_ratio = candidate_ratio
                    best_id = candidate_id if candidate_verified else ""
            if best_id:
                source_id = best_id
                ratio, verified = best_ratio, True
            else:
                ratio, verified = quote_match(combined_normalized, text)
        quote = {
            "text": text,
            "source_id": source_id,
            "source_title": source_titles.get(source_id, source_title_hint),
            "context": _string(item.get("context")),
            "why": _string(item.get("why")),
            "timestamp": _string(item.get("timestamp")),
            "verified": verified,
            "match_ratio": ratio,
        }
        if source_id in normalized and next(
            (record for record in source_records if record["id"] == source_id and record["kind"] != "youtube"), None
        ):
            quote["timestamp"] = ""
        quotes.append(quote)
        if len(quotes) >= max_quotes:
            break

    actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    try:
        reading_time = max(1, int(data.get("reading_time_minutes") or 0))
    except (TypeError, ValueError):
        reading_time = 0
    if not reading_time:
        reading_time = max(5, round(len(_string(data.get("summary"))) / 1000) + 5)

    return {
        "title": _string(data.get("title"))[:120] or "Collection summary",
        "standfirst": _string(data.get("standfirst"))[:300],
        "summary": _string(data.get("summary")),
        "sources": content_sources,
        "themes": themes,
        "comparison": comparison,
        "quotes": quotes,
        "why_it_matters": _string(data.get("why_it_matters")),
        "actions": _strings(actions, 6),
        "tags": [str(tag).strip().lstrip("#") for tag in tags if str(tag).strip()][:10],
        "reading_time_minutes": reading_time,
        "language": _string(data.get("language")) or (source_records[0].get("language") if source_records else "en") or "en",
        "warning": _string(data.get("warning")),
    }
