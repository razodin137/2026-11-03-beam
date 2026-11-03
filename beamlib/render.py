"""Render self-contained, mobile-responsive HTML briefs and the library index."""

from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from typing import Any

from .config import ROOT, category_meta
from .util import human_date, human_duration

DEFAULT_ACCENT = "#2563eb"
COLLECTION_ACCENT = "#0f766e"


@lru_cache(maxsize=1)
def _theme() -> str:
    return (ROOT / "theme.css").read_text(encoding="utf-8")


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_accent(value: str) -> str:
    value = _text(value)
    return value if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value) else DEFAULT_ACCENT


def _safe_url(value: Any) -> str:
    value = _text(value)
    return value if re.fullmatch(r"https?://\S+", value, flags=re.IGNORECASE) else ""


def _paragraphs(value: Any) -> list[str]:
    text = _text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _source_label(kind: str) -> str:
    return {
        "youtube": "YouTube",
        "web": "Web article",
        "html": "Web page",
        "pdf": "PDF",
        "docx": "Document",
        "note": "Note",
        "text": "Text",
    }.get(kind, kind.title() if kind else "Source")


def _tier_label(tier: str) -> str:
    return {"cheap": "cheap", "premium": "premium"}.get(tier, tier or "model")


def _date(value: Any) -> str:
    return human_date(_text(value))


def _hms_to_seconds(value: str) -> int:
    total = 0
    for part in value.split(":"):
        try:
            total = total * 60 + int(part)
        except ValueError:
            return 0
    return total


def _warnings_html(warnings: list[str]) -> str:
    warnings = [warning for warning in warnings if warning]
    if not warnings:
        return ""
    items = "".join(f"<li>{esc(warning)}</li>" for warning in warnings)
    return f'<aside class="warnings" role="note"><strong>Heads up</strong><ul>{items}</ul></aside>'


def render_page(meta: dict[str, Any], cfg: dict[str, Any], *, index_href: str = "../../index.html") -> str:
    content = meta.get("content") if isinstance(meta.get("content"), dict) else {}
    source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
    classification = meta.get("classification") if isinstance(meta.get("classification"), dict) else {}
    routing = meta.get("routing") if isinstance(meta.get("routing"), dict) else {}
    files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
    extracted = meta.get("extraction") if isinstance(meta.get("extraction"), dict) else {}

    category_key = _text(classification.get("category")) or "other"
    category = category_meta(cfg, category_key)
    accent = _safe_accent(category.get("accent", DEFAULT_ACCENT))
    label = _text(category.get("label")) or category_key.replace("-", " ").title()

    title = _text(content.get("title")) or _text(source.get("title")) or _text(classification.get("title")) or "Untitled brief"
    standfirst = _text(content.get("standfirst")) or _text(source.get("description"))
    language = _text(content.get("language")) or _text(source.get("language")) or _text(cfg.get("ui", {}).get("default_language")) or "en"
    reading_time = content.get("reading_time_minutes")
    try:
        reading_time = max(1, int(reading_time))
    except (TypeError, ValueError):
        reading_time = max(3, round(len(_text(content.get("summary"))) / 1000) + 3)

    summary_html = "".join(f"<p>{esc(paragraph)}</p>" for paragraph in _paragraphs(content.get("summary")))
    quotes = [quote for quote in _dicts(content.get("quotes")) if _text(quote.get("text"))]
    key_points = [point for point in _dicts(content.get("key_points")) if _text(point.get("label"))]
    sections = [section for section in _dicts(content.get("sections")) if _text(section.get("heading")) or _text(section.get("body"))]
    actions = _strings(content.get("actions"))
    tags = _strings(content.get("tags"))
    why = _text(content.get("why_it_matters"))
    source_url = _safe_url(source.get("url"))

    extra = source.get("extra") if isinstance(source.get("extra"), dict) else {}
    raw_chapters = extra.get("chapters") if isinstance(extra.get("chapters"), list) else []
    chapter_links: list[str] = []
    for raw_chapter in raw_chapters:
        match = re.match(r"^\[([0-9:]+)\]\s*(.+)$", str(raw_chapter).strip())
        if not match:
            continue
        stamp, name = match.groups()
        is_youtube = (
            bool(source_url)
            and (
                _text(source.get("site")).lower() == "youtube"
                or "youtube.com" in source_url
                or "youtu.be" in source_url
            )
        )
        if is_youtube:
            seconds = _hms_to_seconds(stamp)
            href = f"{source_url}{'&' if '?' in source_url else '?'}t={seconds}s"
            chapter_links.append(
                f'<a class="chapter" href="{esc(href)}" target="_blank" rel="noopener noreferrer">'
                f'<span class="chapter__time">{esc(stamp)}</span>{esc(name)}</a>'
            )
        else:
            chapter_links.append(
                f'<span class="chapter"><span class="chapter__time">{esc(stamp)}</span>{esc(name)}</span>'
            )
    chapters_html = ""
    if chapter_links:
        chapters_html = (
            '<section id="chapters" class="section">'
            '<div class="section__head"><span class="section__kicker">Chapters</span><h2>Where to jump in</h2></div>'
            f'<div class="chapters">{"".join(chapter_links)}</div>'
            "</section>"
        )

    byline_parts: list[str] = []
    byline_parts.append(f'<span class="chip">{esc(_source_label(_text(source.get("kind"))))}</span>')
    if source.get("author"):
        byline_parts.append(f'<span class="chip">{esc(source.get("author"))}</span>')
    elif source.get("site"):
        byline_parts.append(f'<span class="chip">{esc(source.get("site"))}</span>')
    if source.get("published"):
        byline_parts.append(f'<span class="chip">{esc(_date(source.get("published")))}</span>')
    if source.get("duration"):
        byline_parts.append(f'<span class="chip">{esc(human_duration(source.get("duration")))}</span>')
    byline_parts.append(f'<span class="chip">{reading_time} min read</span>')
    byline_parts.append(f'<span class="chip chip--tier">{esc(_tier_label(_text(routing.get("tier"))))} model</span>')

    byline = '<div class="byline">' + "".join(byline_parts) + "</div>"

    cover = _text(files.get("cover"))
    cover_src = ""
    if cover:
        cover_src = cover
    elif _safe_url(source.get("thumbnail")):
        cover_src = _safe_url(source.get("thumbnail"))
    cover_alt = _text(content.get("cover_alt")) or f"Cover image for {title}"
    if cover_src:
        media_html = (
            '<figure class="hero__media">'
            f'<img src="{esc(cover_src)}" alt="{esc(cover_alt)}" loading="eager" decoding="async">'
            "</figure>"
        )
    else:
        media_html = (
            '<div class="hero__art" aria-hidden="true">'
            '<span class="hero__art-ring"></span><span class="hero__art-ring hero__art-ring--2"></span>'
            '<span class="hero__art-mark">\u201c</span>'
            f'<span class="hero__art-label">{esc(label)}</span>'
            "</div>"
        )

    headings: list[tuple[str, str]] = []
    if summary_html:
        headings.append(("brief", "The brief"))
    if chapters_html:
        headings.append(("chapters", "Chapters"))
    if quotes:
        headings.append(("quotes", "The juicy bits"))
    if sections:
        headings.append(("detail", "In detail"))
    if why:
        headings.append(("why", "Why it matters"))
    if actions:
        headings.append(("next", "Next steps"))
    toc_items = "".join(f'<li><a href="#{anchor}">{esc(text)}</a></li>' for anchor, text in headings)
    toc = (
        '<aside class="toc" aria-label="On this page">'
        '<p class="toc__title">On this page</p>'
        f"<ol>{toc_items}</ol>"
        f'<div class="toc__more"><a href="{esc(index_href)}">\u2190 Library</a></div>'
        "</aside>"
    )

    summary_section = ""
    if summary_html:
        summary_section = (
            '<section id="brief" class="section">'
            '<div class="section__head"><span class="section__kicker">The brief</span></div>'
            f'<div class="summary">{summary_html}</div>'
            "</section>"
        )

    stats_html = ""
    if key_points:
        cards = []
        for point in key_points:
            value = _text(point.get("value"))
            label_text = _text(point.get("label"))
            detail = _text(point.get("detail"))
            cards.append(
                '<div class="stat">'
                f'<div class="stat__value">{esc(value or "\u2014")}</div>'
                f'<div class="stat__label">{esc(label_text)}</div>'
                + (f'<div class="stat__detail">{esc(detail)}</div>' if detail else "")
                + "</div>"
            )
        stats_html = f'<section class="stats" aria-label="Key figures">{"".join(cards)}</section>'

    quotes_html = ""
    if quotes:
        figures = []
        for quote in quotes[: int(cfg.get("limits", {}).get("max_quotes", 10))]:
            text = _text(quote.get("text"))
            speaker = _text(quote.get("speaker"))
            timestamp = _text(quote.get("timestamp"))
            context = _text(quote.get("context"))
            why_quote = _text(quote.get("why"))
            verified = bool(quote.get("verified"))
            meta_bits = []
            if speaker:
                meta_bits.append(f'<span class="who">{esc(speaker)}</span>')
            if timestamp:
                meta_bits.append(f'<span class="stamp">{esc(timestamp)}</span>')
            if context:
                meta_bits.append(f"<span>{esc(context)}</span>")
            if verified:
                meta_bits.append('<span class="verified" title="This wording was found in the source">\u2713 verified</span>')
            figures.append(
                '<figure class="quote">'
                f"<blockquote>{esc(text)}</blockquote>"
                f'<figcaption>{"".join(meta_bits)}</figcaption>'
                + (f'<p class="why">{esc(why_quote)}</p>' if why_quote else "")
                + "</figure>"
            )
        quotes_html = (
            '<section id="quotes" class="section">'
            '<div class="section__head"><span class="section__kicker">Direct quotes</span><h2>The juicy bits</h2></div>'
            f'<div class="quotes">{"".join(figures)}</div>'
            "</section>"
        )

    detail_html = ""
    if sections:
        blocks = []
        for index, section in enumerate(sections, start=1):
            heading = _text(section.get("heading"))
            body = "".join(f"<p>{esc(paragraph)}</p>" for paragraph in _paragraphs(section.get("body")))
            bullets = _strings(section.get("bullets"))
            bullets_html = ""
            if bullets:
                bullets_html = '<ul class="bullets">' + "".join(f"<li>{esc(item)}</li>" for item in bullets) + "</ul>"
            blocks.append(
                '<div class="deep__item">'
                f'<h3><span aria-hidden="true">{index:02d}. </span>{esc(heading or "Section")}</h3>'
                f'<div class="deep__body">{body}</div>{bullets_html}'
                "</div>"
            )
        detail_html = (
            '<section id="detail" class="section">'
            '<div class="section__head"><span class="section__kicker">In detail</span><h2>The full picture</h2></div>'
            f'<div class="deep">{"".join(blocks)}</div>'
            "</section>"
        )

    why_html = ""
    if why:
        why_html = (
            '<section id="why" class="callout">'
            '<span class="section__kicker">Why it matters</span>'
            f"<p>{esc(why)}</p>"
            "</section>"
        )

    actions_html = ""
    if actions:
        actions_html = (
            '<section id="next" class="section">'
            '<div class="section__head"><span class="section__kicker">Next steps</span><h2>What to do with this</h2></div>'
            '<ol class="actions">'
            + "".join(f"<li>{esc(action)}</li>" for action in actions)
            + "</ol></section>"
        )

    tags_html = ""
    if tags:
        tags_html = '<div class="tags">' + "".join(f'<span class="tag">#{esc(tag.lstrip("#"))}</span>' for tag in tags) + "</div>"

    warnings = list(source.get("warnings") or []) if isinstance(source.get("warnings"), list) else []
    if content.get("warning"):
        warnings.append(_text(content.get("warning")))
    if meta.get("truncated"):
        warnings.append("The source was longer than Beam's processing limit, so the middle was sampled.")
    warnings_html = _warnings_html([str(warning) for warning in warnings])
    warnings_html = warnings_html.replace('<aside class="warnings"', '<aside id="warnings" class="warnings"')

    source_name = _text(files.get("source"))
    reading_copy = _text(files.get("reading_copy"))
    if source_url:
        source_link = f'<a class="source__link" href="{esc(source_url)}" target="_blank" rel="noopener noreferrer">{esc(source_url)}</a>'
    elif source_name:
        source_link = f'<a class="source__link" href="{esc(source_name)}">{esc(source_name)}</a>'
    else:
        source_link = '<span class="source__link">No external source recorded.</span>'

    source_bits = [_source_label(_text(source.get("kind")))]
    if source.get("site"):
        source_bits.append(_text(source.get("site")))
    if source.get("author") and source.get("author") != source.get("site"):
        source_bits.append(_text(source.get("author")))
    if source.get("duration"):
        source_bits.append(human_duration(source.get("duration")))
    source_bits.append(f"generated {_date(meta.get('created_at'))}")
    if extracted.get("model"):
        source_bits.append(_text(extracted.get("model")))
    if meta.get("stats", {}).get("source_chars"):
        source_bits.append(f"{int(meta['stats']['source_chars']):,} source characters")
    decisions = _text(files.get("meta"))
    decisions_link = f' \u00b7 <a href="{esc(decisions)}">routing decisions</a>' if decisions else ""

    source_html = (
        '<footer class="source">'
        '<span class="source__label">Source</span>'
        f'<div class="source__row">{source_link}</div>'
        + (f'<p class="source__note">Local copy: <a href="{esc(source_name)}">{esc(source_name)}</a></p>' if source_name and source_url else "")
        + (f'<p class="source__note">Reading copy: <a href="{esc(reading_copy)}">{esc(reading_copy)}</a></p>' if reading_copy else "")
        + f'<p class="source__note">{esc(" \u00b7 ".join(source_bits))}{decisions_link}</p>'
        + "</footer>"
    )

    page_title = f"{title} \u2014 {label}"
    description = standfirst or f"A visual brief from {label}."
    site_name = _text(cfg.get("ui", {}).get("site_name")) or "Beam Library"
    hero_class = "hero hero--long" if len(title) > 58 else "hero"

    return f"""<!doctype html>
<html lang="{esc(language or "en")}" style="--accent: {accent}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(page_title)}</title>
<meta name="description" content="{esc(description)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:type" content="article">
<style>
{_theme()}
</style>
</head>
<body>
<div class="progress" aria-hidden="true"></div>
<a class="skip" href="#main">Skip to content</a>
<header class="{hero_class}">
  <div class="hero__inner">
    <div class="hero__copy">
      <span class="eyebrow">{esc(label)}</span>
      <h1>{esc(title)}</h1>
      <p class="standfirst">{esc(standfirst)}</p>
      {byline}
    </div>
    {media_html}
  </div>
</header>
<main id="main" class="page">
  {toc}
  <article class="article">
    {warnings_html}
    {summary_section}
    {chapters_html}
    {stats_html}
    {quotes_html}
    {detail_html}
    {why_html}
    {actions_html}
    {tags_html}
    {source_html}
  </article>
</main>
<footer class="site-foot">
  <div class="site-foot__inner">
    <span>{esc(site_name)} \u2014 generated by Beam from <code>inbox/</code></span>
    <span><a href="{esc(index_href)}">All briefs</a></span>
  </div>
</footer>
<script>
(function () {{
  var bar = document.querySelector('.progress');
  if (!bar) return;
  var update = function () {{
    var root = document.documentElement;
    var max = root.scrollHeight - root.clientHeight;
    bar.style.width = (max > 0 ? (root.scrollTop / max) * 100 : 0) + '%';
  }};
  window.addEventListener('scroll', update, {{ passive: true }});
  window.addEventListener('resize', update);
  update();
}})();
</script>
</body>
</html>
"""


def render_collection(meta: dict[str, Any], cfg: dict[str, Any], *, index_href: str = "../../index.html") -> str:
    content = meta.get("content") if isinstance(meta.get("content"), dict) else {}
    routing = meta.get("routing") if isinstance(meta.get("routing"), dict) else {}
    files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
    extracted = meta.get("extraction") if isinstance(meta.get("extraction"), dict) else {}

    sources = [source for source in _dicts(content.get("sources")) if _text(source.get("id"))]
    themes = [
        theme
        for theme in _dicts(content.get("themes"))
        if _text(theme.get("heading")) or _text(theme.get("body"))
    ]
    comparison = [row for row in _dicts(content.get("comparison")) if _text(row.get("dimension"))]
    quotes = [quote for quote in _dicts(content.get("quotes")) if _text(quote.get("text"))]
    actions = _strings(content.get("actions"))
    tags = _strings(content.get("tags"))

    title = _text(content.get("title")) or "Collection summary"
    standfirst = _text(content.get("standfirst"))
    language = _text(content.get("language")) or "en"
    why = _text(content.get("why_it_matters"))
    summary_html = "".join(f"<p>{esc(paragraph)}</p>" for paragraph in _paragraphs(content.get("summary")))
    reading_time = content.get("reading_time_minutes")
    try:
        reading_time = max(1, int(reading_time))
    except (TypeError, ValueError):
        reading_time = 6
    source_count = len(sources)

    source_titles = {_text(source.get("id")): _text(source.get("title")) or f"Source {_text(source.get('id'))}" for source in sources}
    source_ids = [_text(source.get("id")) for source in sources]
    source_urls = {_text(source.get("id")): _safe_url(source.get("url")) for source in sources}
    cover = _text(files.get("cover"))
    cover_src = cover or _safe_url(meta.get("cover"))
    cover_alt = _text(content.get("cover_alt")) or f"Cover image for {title}"
    if cover_src:
        media_html = (
            '<figure class="hero__media">'
            f'<img src="{esc(cover_src)}" alt="{esc(cover_alt)}" loading="eager" decoding="async">'
            "</figure>"
        )
    else:
        media_html = (
            '<div class="hero__art" aria-hidden="true">'
            '<span class="hero__art-ring"></span><span class="hero__art-ring hero__art-ring--2"></span>'
            '<span class="hero__art-mark">\u201c</span>'
            '<span class="hero__art-label">Collection</span>'
            "</div>"
        )

    byline_parts = [
        '<span class="chip chip--collection">Collection</span>',
        f'<span class="chip">{source_count} source{"s" if source_count != 1 else ""}</span>',
        f'<span class="chip">{reading_time} min read</span>',
    ]
    if _date(meta.get("created_at")):
        byline_parts.append(f'<span class="chip">{esc(_date(meta.get("created_at")))}</span>')
    byline_parts.append(f'<span class="chip chip--tier">{esc(_tier_label(_text(routing.get("tier"))))} model</span>')
    byline = '<div class="byline">' + "".join(byline_parts) + "</div>"

    headings = [
        item
        for item in (
            (("brief", "The brief") if summary_html else None),
            (("sources", "The sources") if sources else None),
            (("themes", "The synthesis") if themes else None),
            (("comparison", "At a glance") if comparison else None),
            (("quotes", "The juicy bits") if quotes else None),
            (("why", "Why it matters") if why else None),
            (("next", "Next steps") if actions else None),
        )
        if item
    ]
    toc_items = "".join(f'<li><a href="#{anchor}">{esc(label)}</a></li>' for anchor, label in headings)
    toc = (
        '<aside class="toc" aria-label="On this page">'
        '<p class="toc__title">On this page</p>'
        f"<ol>{toc_items}</ol>"
        f'<div class="toc__more"><a href="{esc(index_href)}">\u2190 Library</a></div>'
        "</aside>"
    )

    summary_section = ""
    if summary_html:
        summary_section = (
            '<section id="brief" class="section">'
            '<div class="section__head"><span class="section__kicker">The brief</span></div>'
            f'<div class="summary">{summary_html}</div>'
            "</section>"
        )

    source_cards = []
    for source in sources:
        source_id = _text(source.get("id"))
        source_title = _text(source.get("title")) or f"Source {source_id}"
        source_url = _safe_url(source.get("url"))
        title_html = (
            f'<a href="{esc(source_url)}" target="_blank" rel="noopener noreferrer">{esc(source_title)}</a>'
            if source_url
            else esc(source_title)
        )
        meta_bits = [_source_label(_text(source.get("kind")))]
        for field in ("site", "author"):
            value = _text(source.get(field))
            if value and value not in meta_bits:
                meta_bits.append(value)
        if source.get("published"):
            meta_bits.append(_date(source.get("published")))
        if source.get("duration"):
            meta_bits.append(human_duration(source.get("duration")))
        points = _strings(source.get("key_points"))
        points_html = (
            '<ul class="source-card__points">' + "".join(f"<li>{esc(point)}</li>" for point in points) + "</ul>"
            if points
            else ""
        )
        role = _text(source.get("role"))
        role_html = f'<p class="source-card__role">{esc(role)}</p>' if role else ""
        reading_copy = _text(source.get("reading_copy"))
        read_html = (
            f'<a class="source-card__read" href="{esc(reading_copy)}">Reading copy \u2197</a>'
            if reading_copy
            else ""
        )
        source_cards.append(
            f'<article class="source-card" id="source-{esc(source_id)}">'
            f'<div class="source-card__top"><span class="source-card__index">Source {esc(source_id)}</span></div>'
            f"<h3>{title_html}</h3>"
            f'<p class="source-card__meta">{esc(" \u00b7 ".join(meta_bits))}</p>'
            f"{role_html}{points_html}{read_html}"
            "</article>"
        )
    sources_html = ""
    if source_cards:
        sources_html = (
            '<section id="sources" class="section">'
            '<div class="section__head"><span class="section__kicker">The sources</span><h2>What is in this collection</h2></div>'
            f'<div class="sources-grid">{"".join(source_cards)}</div>'
            "</section>"
        )

    theme_blocks = []
    for index, theme in enumerate(themes, start=1):
        heading = _text(theme.get("heading")) or "Theme"
        body = "".join(f"<p>{esc(paragraph)}</p>" for paragraph in _paragraphs(theme.get("body")))
        bullets = _strings(theme.get("bullets"))
        bullets_html = (
            '<ul class="bullets">' + "".join(f"<li>{esc(bullet)}</li>" for bullet in bullets) + "</ul>"
            if bullets
            else ""
        )
        refs = []
        raw_refs = theme.get("source_ids") if isinstance(theme.get("source_ids"), list) else []
        for source_id in raw_refs:
            source_id = _text(source_id)
            if source_id in source_titles:
                refs.append(f'<a class="ref" href="#source-{esc(source_id)}">Source {esc(source_id)}</a>')
        refs_html = f'<div class="refs">{"".join(refs)}</div>' if refs else ""
        theme_blocks.append(
            '<div class="deep__item">'
            f'<h3><span aria-hidden="true">{index:02d}. </span>{esc(heading)}</h3>'
            f'<div class="deep__body">{body}</div>{bullets_html}{refs_html}'
            "</div>"
        )
    themes_html = ""
    if theme_blocks:
        themes_html = (
            '<section id="themes" class="section">'
            '<div class="section__head"><span class="section__kicker">The synthesis</span><h2>What the sources say together</h2></div>'
            f'<div class="deep">{"".join(theme_blocks)}</div>'
            "</section>"
        )

    comparison_html = ""
    if comparison:
        header_cells = "".join(
            f'<th><a href="#source-{esc(source_id)}">Source {esc(source_id)}</a></th>' for source_id in source_ids
        )
        rows = []
        for row in comparison:
            entries = {}
            raw_entries = row.get("entries") if isinstance(row.get("entries"), list) else []
            for entry in _dicts(raw_entries):
                entries[_text(entry.get("source_id"))] = entry
            cells = []
            for source_id in source_ids:
                entry = entries.get(source_id, {})
                value = _text(entry.get("value")) or "\u2014"
                note = _text(entry.get("note"))
                cells.append(
                    f'<td><strong>{esc(value)}</strong>'
                    + (f'<span class="comparison__note">{esc(note)}</span>' if note else "")
                    + "</td>"
                )
            rows.append(f'<tr><th scope="row">{esc(_text(row.get("dimension")))}</th>{"".join(cells)}</tr>')
        comparison_html = (
            '<section id="comparison" class="section">'
            '<div class="section__head"><span class="section__kicker">At a glance</span><h2>How they compare</h2></div>'
            '<div class="comparison-wrap"><table class="comparison">'
            f'<thead><tr><th class="comparison__dimension">Dimension</th>{header_cells}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            "</table></div>"
            "</section>"
        )

    quote_cards = []
    for quote in quotes[: int(cfg.get("limits", {}).get("max_quotes", 10))]:
        text = _text(quote.get("text"))
        source_id = _text(quote.get("source_id"))
        source_title = _text(quote.get("source_title")) or source_titles.get(source_id) or ""
        timestamp = _text(quote.get("timestamp"))
        context = _text(quote.get("context"))
        why_quote = _text(quote.get("why"))
        verified = bool(quote.get("verified"))
        meta_bits = []
        if source_id:
            meta_bits.append(f'<a class="who" href="#source-{esc(source_id)}">Source {esc(source_id)}</a>')
        elif source_title:
            meta_bits.append(f'<span class="who">{esc(source_title)}</span>')
        if timestamp:
            meta_bits.append(f'<span class="stamp">{esc(timestamp)}</span>')
        if context:
            meta_bits.append(f"<span>{esc(context)}</span>")
        if verified:
            meta_bits.append('<span class="verified" title="This wording was found in the source">\u2713 verified</span>')
        quote_cards.append(
            '<figure class="quote">'
            f"<blockquote>{esc(text)}</blockquote>"
            f'<figcaption>{"".join(meta_bits)}</figcaption>'
            + (f'<p class="why">{esc(why_quote)}</p>' if why_quote else "")
            + "</figure>"
        )
    quotes_html = ""
    if quote_cards:
        quotes_html = (
            '<section id="quotes" class="section">'
            '<div class="section__head"><span class="section__kicker">Direct quotes</span><h2>The juicy bits</h2></div>'
            f'<div class="quotes">{"".join(quote_cards)}</div>'
            "</section>"
        )

    why_html = ""
    if why:
        why_html = (
            '<section id="why" class="callout">'
            '<span class="section__kicker">Why it matters</span>'
            f"<p>{esc(why)}</p>"
            "</section>"
        )

    actions_html = ""
    if actions:
        actions_html = (
            '<section id="next" class="section">'
            '<div class="section__head"><span class="section__kicker">Next steps</span><h2>What to do with this</h2></div>'
            '<ol class="actions">'
            + "".join(f"<li>{esc(action)}</li>" for action in actions)
            + "</ol></section>"
        )

    tags_html = ""
    if tags:
        tags_html = '<div class="tags">' + "".join(f'<span class="tag">#{esc(tag.lstrip("#"))}</span>' for tag in tags) + "</div>"

    warnings = list(meta.get("source", {}).get("warnings") or []) if isinstance(meta.get("source"), dict) else []
    warnings_html = _warnings_html([str(warning) for warning in warnings])
    warnings_html = warnings_html.replace('<aside class="warnings"', '<aside id="warnings" class="warnings"')

    decisions = _text(files.get("meta"))
    decisions_link = f' \u00b7 <a href="{esc(decisions)}">routing decisions</a>' if decisions else ""
    source_bits = ["Collection", f"{source_count} sources", f"generated {_date(meta.get('created_at'))}"]
    if extracted.get("model"):
        source_bits.append(_text(extracted.get("model")))
    source_html = (
        '<footer class="source">'
        '<span class="source__label">Collection</span>'
        f'<p class="source__note">{esc(" \u00b7 ".join(source_bits))}{decisions_link}</p>'
        "</footer>"
    )

    site_name = _text(cfg.get("ui", {}).get("site_name")) or "Beam Library"
    page_title = f"{title} \u2014 Collection"
    hero_class = "hero hero--long" if len(title) > 58 else "hero"

    return f"""<!doctype html>
<html lang="{esc(language or "en")}" style="--accent: {COLLECTION_ACCENT}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(page_title)}</title>
<meta name="description" content="{esc(standfirst)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(standfirst)}">
<meta property="og:type" content="article">
<style>
{_theme()}
</style>
</head>
<body>
<div class="progress" aria-hidden="true"></div>
<a class="skip" href="#main">Skip to content</a>
<header class="{hero_class}">
  <div class="hero__inner">
    <div class="hero__copy">
      <span class="eyebrow">Collection</span>
      <h1>{esc(title)}</h1>
      <p class="standfirst">{esc(standfirst)}</p>
      {byline}
    </div>
    {media_html}
  </div>
</header>
<main id="main" class="page">
  {toc}
  <article class="article">
    {warnings_html}
    {summary_section}
    {sources_html}
    {themes_html}
    {comparison_html}
    {quotes_html}
    {why_html}
    {actions_html}
    {tags_html}
    {source_html}
  </article>
</main>
<footer class="site-foot">
  <div class="site-foot__inner">
    <span>{esc(site_name)} \u2014 a collection of {source_count} source{"s" if source_count != 1 else ""}</span>
    <span><a href="{esc(index_href)}">All briefs</a></span>
  </div>
</footer>
<script>
(function () {{
  var bar = document.querySelector('.progress');
  if (!bar) return;
  var update = function () {{
    var root = document.documentElement;
    var max = root.scrollHeight - root.clientHeight;
    bar.style.width = (max > 0 ? (root.scrollTop / max) * 100 : 0) + '%';
  }};
  window.addEventListener('scroll', update, {{ passive: true }});
  window.addEventListener('resize', update);
  update();
}})();
</script>
</body>
</html>
"""


def _index_category(cfg: dict[str, Any], key: str) -> dict[str, str]:
    if key == "collection":
        return {"label": "Collections", "accent": COLLECTION_ACCENT}
    return category_meta(cfg, key)


def index_card(item: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    classification = item.get("classification") if isinstance(item.get("classification"), dict) else {}
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    files = item.get("files") if isinstance(item.get("files"), dict) else {}
    routing = item.get("routing") if isinstance(item.get("routing"), dict) else {}
    html_file = _text(files.get("html"))
    if not html_file:
        return None
    is_collection = item.get("kind") == "collection" or _text(classification.get("category")) == "collection"
    title = _text(content.get("title")) or _text(source.get("title")) or _text(classification.get("title")) or "Untitled brief"
    standfirst = _text(content.get("standfirst")) or _text(source.get("description"))
    tags = _strings(content.get("tags"))
    reading_time = content.get("reading_time_minutes")
    try:
        reading_time = max(1, int(reading_time))
    except (TypeError, ValueError):
        reading_time = 3
    cover_file = _text(files.get("cover"))

    if is_collection:
        category_key = "collection"
        category = _index_category(cfg, category_key)
        item_id = _text(item.get("id")) or html_file.rsplit(".", 1)[0]
        href = f"collections/{item_id}/{html_file}"
        cover = f"collections/{item_id}/{cover_file}" if cover_file else ""
        source_count = len(content.get("sources")) if isinstance(content.get("sources"), list) else 0
        source_label = f"Collection \u00b7 {source_count} source{'s' if source_count != 1 else ''}" if source_count else "Collection"
        source_site = ""
    else:
        category_key = _text(classification.get("category")) or "other"
        category = category_meta(cfg, category_key)
        href = f"sorted/{category_key}/{html_file}"
        cover = f"sorted/{category_key}/{cover_file}" if cover_file else ""
        source_label = _source_label(_text(source.get("kind")))
        source_site = _text(source.get("author")) or _text(source.get("site"))

    search = " ".join(
        [
            title,
            standfirst,
            category_key,
            _text(category.get("label")),
            _text(source.get("site")),
            _text(source.get("author")),
            "collection" if is_collection else "",
        ]
        + tags
    ).lower()
    return {
        "title": title,
        "standfirst": standfirst,
        "category": category_key,
        "label": _text(category.get("label")) or category_key,
        "accent": _safe_accent(category.get("accent", DEFAULT_ACCENT)),
        "href": href,
        "cover": cover,
        "source_label": source_label,
        "source_site": source_site,
        "reading_time": reading_time,
        "tier": _text(routing.get("tier")),
        "created_at": _text(item.get("created_at")),
        "search": search,
        "kind": "collection" if is_collection else "brief",
    }


def render_index(items: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    site_name = _text(cfg.get("ui", {}).get("site_name")) or "Beam Library"
    prepared: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        card = index_card(item, cfg)
        if card:
            prepared.append(card)
    prepared.sort(key=lambda item: item["created_at"], reverse=True)

    present_categories = []
    for item in prepared:
        if item["category"] not in present_categories:
            present_categories.append(item["category"])
    present_categories.sort(key=lambda key: _text(_index_category(cfg, key).get("label")))

    total = len(prepared)
    category_count = len(present_categories)
    last_updated = _date(prepared[0]["created_at"]) if prepared else ""
    stats = (
        f'<div class="index-stat"><strong>{total}</strong>brief{"s" if total != 1 else ""}</div>'
        f'<div class="index-stat"><strong>{category_count}</strong>categor{"ies" if category_count != 1 else "y"} in use</div>'
        + (f'<div class="index-stat"><strong>{esc(last_updated)}</strong>last updated</div>' if last_updated else "")
    )

    filter_buttons = ['<button type="button" data-filter="all" aria-pressed="true">All</button>']
    for key in present_categories:
        label = _text(_index_category(cfg, key).get("label")) or key
        filter_buttons.append(
            f'<button type="button" data-filter="{esc(key)}" aria-pressed="false">{esc(label)}</button>'
        )

    cards = []
    for item in prepared:
        if item["cover"]:
            media = (
                f'<img src="{esc(item["cover"])}" alt="" loading="lazy" decoding="async">'
            )
        else:
            media = f'<div class="card__fallback" aria-hidden="true">{esc(item["label"][:1])}</div>'
        tier = f'<span class="tier">{esc(item["tier"])}</span>' if item["tier"] else ""
        site = (
            f" \u00b7 {esc(item['source_site'])}"
            if item["source_site"] and item["source_site"] != item["source_label"]
            else ""
        )
        cards.append(
            f'<a class="card" href="{esc(item["href"])}" data-category="{esc(item["category"])}" data-search="{esc(item["search"])}" style="--accent: {item["accent"]}">'
            f'<div class="card__media">{media}<span class="card__cat">{esc(item["label"])}</span></div>'
            '<div class="card__body">'
            f'<h2>{esc(item["title"])}</h2>'
            f'<p>{esc(item["standfirst"])}</p>'
            '<div class="card__meta">'
            f'<span>{esc(item["source_label"])}{site}</span>'
            f'<span>{item["reading_time"]} min read</span>'
            f"{tier}"
            "</div></div></a>"
        )

    if prepared:
        content_html = (
            '<div class="toolbar">'
            f'<div class="filters">{"".join(filter_buttons)}</div>'
            '<label class="visually-hidden" for="search" hidden>Search briefs</label>'
            '<input class="search" id="search" type="search" placeholder="Search briefs\u2026" autocomplete="off">'
            "</div>"
            '<p class="result-count" id="result-count"></p>'
            f'<div class="cards">{"".join(cards)}</div>'
        )
    else:
        content_html = (
            '<div class="empty">'
            "<h2>Nothing sorted yet</h2>"
            "<p>Drop a link, PDF, note, or text file into <code>inbox/</code>, then run <code>./beam.py watch</code>.</p>"
            "<p>Beam classifies it, chooses a model, and builds a visual brief beside the source.</p>"
            "</div>"
        )

    search_script = """
(function () {
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-filter]'));
  var search = document.getElementById('search');
  var count = document.getElementById('result-count');
  if (!cards.length || !search || !count) return;
  var active = 'all';
  function apply() {
    var query = (search.value || '').toLowerCase().trim();
    var visible = 0;
    cards.forEach(function (card) {
      var ok = (active === 'all' || card.dataset.category === active) &&
        (!query || (card.dataset.search || '').indexOf(query) !== -1);
      card.hidden = !ok;
      if (ok) visible += 1;
    });
    count.textContent = visible + (visible === 1 ? ' brief' : ' briefs');
  }
  buttons.forEach(function (button) {
    button.addEventListener('click', function () {
      active = button.dataset.filter;
      buttons.forEach(function (other) { other.setAttribute('aria-pressed', String(other === button)); });
      apply();
    });
  });
  search.addEventListener('input', apply);
  apply();
})();
"""

    return f"""<!doctype html>
<html lang="{esc(_text(cfg.get("ui", {}).get("default_language")) or "en")}" style="--accent: {DEFAULT_ACCENT}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(site_name)}</title>
<meta name="description" content="Visual briefs from videos, articles, papers, and notes.">
<style>
{_theme()}
</style>
</head>
<body>
<header class="index-hero">
  <div class="index-hero__inner">
    <span class="eyebrow">Beam</span>
    <h1>{esc(site_name)}</h1>
    <p>Visual briefs from videos, articles, papers, and notes \u2014 the juiciest quotes and ideas, surfaced.</p>
    <div class="index-stats">{stats}</div>
  </div>
</header>
<main class="index-main">
  {content_html}
</main>
<footer class="site-foot">
  <div class="site-foot__inner">
    <span>Drop something into <code>inbox/</code> \u00b7 generated by Beam</span>
    <span>{total} brief{"s" if total != 1 else ""}</span>
  </div>
</footer>
<script>{search_script}</script>
</body>
</html>
"""
