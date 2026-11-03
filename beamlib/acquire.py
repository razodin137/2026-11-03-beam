"""Turn a dropped file, link, or note into a clean source bundle."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .util import human_date, human_duration

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Beam/1.0"
)
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
SKIP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "template", "iframe",
    "nav", "footer", "aside", "form", "button", "select", "option", "input",
    "textarea", "dialog",
}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "header", "footer", "nav",
    "ul", "ol", "li", "table", "tr", "td", "th", "figure", "figcaption",
    "blockquote", "pre", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".py", ".js", ".ts", ".tsx",
    ".jsx", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".rb", ".php", ".swift", ".sql", ".sh", ".bash", ".zsh", ".fish", ".css",
    ".scss", ".sass", ".less", ".xml", ".tex", ".r", ".lua", ".vim",
}


class AcquireError(RuntimeError):
    pass


@dataclass
class Resource:
    kind: str = "text"
    title: str = ""
    url: str = ""
    author: str = ""
    published: str = ""
    description: str = ""
    text: str = ""
    thumbnail: str = ""
    duration: int = 0
    site: str = ""
    language: str = "en"
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def preview(self, limit: int) -> str:
        lines = [
            f"Source type: {self.kind}",
            f"Title: {self.title or '(unknown)'}",
        ]
        if self.url:
            lines.append(f"URL: {self.url}")
        if self.author:
            lines.append(f"Author / channel: {self.author}")
        if self.site:
            lines.append(f"Site: {self.site}")
        if self.published:
            lines.append(f"Published: {self.published}")
        if self.duration:
            lines.append(f"Duration: {human_duration(self.duration)}")
        if self.description:
            lines.append(f"Description: {self.description[:1200]}")
        body = self.text[:limit].strip()
        if body:
            lines.append("\nCONTENT:\n" + body)
        if len(self.text) > limit:
            lines.append(f"\n[… {len(self.text) - limit:,} more characters omitted from this preview …]")
        return "\n".join(lines)


def looks_like_url(value: str) -> bool:
    value = value.strip()
    return bool(re.fullmatch(r"https?://\S+", value, flags=re.IGNORECASE))


def extract_urls(value: str) -> list[str]:
    return [match.rstrip(".,;:!?") for match in URL_RE.findall(value or "")]


def is_youtube(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host in {"youtu.be", "www.youtu.be"} or host.endswith("youtube.com") or host.endswith("youtube-nocookie.com")


def _decode(data: bytes, content_type: str = "") -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    charset = ""
    match = re.search(r"charset=([\w-]+)", content_type or "", flags=re.IGNORECASE)
    if match:
        charset = match.group(1)
    encodings = [charset, "utf-8", "cp1252", "latin-1"] if charset else ["utf-8", "cp1252", "latin-1"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def fetch_url(url: str, cfg: dict[str, Any], *, max_bytes: int | None = None) -> tuple[bytes, str, str, bool]:
    max_bytes = int(max_bytes or cfg.get("limits", {}).get("max_fetch_bytes", 8_000_000))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            content_type = response.headers.get("Content-Type", "")
            final_url = response.geturl()
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise AcquireError(f"HTTP {exc.code} while fetching {url}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise AcquireError(f"could not fetch {url}: {exc}") from exc
    truncated = len(data) > max_bytes
    return (data[:max_bytes], content_type, final_url, truncated)


class PageParser(HTMLParser):
    """A tolerant reader that keeps headings, lists, and prose while dropping chrome."""

    def __init__(self) -> None:
        try:
            super().__init__(convert_charrefs=True)
        except TypeError:
            super().__init__()
            self.convert_charrefs = True
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.language = ""
        self.body_parts: list[str] = []
        self.article_parts: list[str] = []
        self._in_title = False
        self._skip = 0
        self._article = 0
        self._main = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {key.lower(): (value or "") for key, value in attrs}
        if tag == "html" and attrs_map.get("lang"):
            self.language = attrs_map["lang"].split("-")[0].lower()
        if tag == "meta":
            key = attrs_map.get("property") or attrs_map.get("name")
            content = attrs_map.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key.lower(), content)
        elif tag == "link":
            rel = attrs_map.get("rel", "").lower()
            if "canonical" in rel and attrs_map.get("href"):
                self.canonical = attrs_map["href"]
        elif tag == "title":
            self._in_title = True

        if tag in SKIP_TAGS:
            if tag not in VOID_TAGS:
                self._skip += 1
            return
        if tag == "article":
            self._article += 1
        elif tag == "main":
            self._main += 1

        if tag in HEADING_TAGS:
            self._append("\n\n" + ("#" * int(tag[1])) + " ")
        elif tag == "li":
            self._append("\n- ")
        elif tag == "br":
            self._append("\n")
        elif tag == "blockquote":
            self._append("\n> ")
        elif tag in BLOCK_TAGS:
            self._append("\n\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Void/self-closing tags never hold text, so they must not change skip depth.
        tag = tag.lower()
        attrs_map = {key.lower(): (value or "") for key, value in attrs}
        if tag in SKIP_TAGS:
            return
        if tag == "meta":
            key = attrs_map.get("property") or attrs_map.get("name")
            content = attrs_map.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key.lower(), content)
        elif tag.lower() == "link":
            rel = attrs_map.get("rel", "").lower()
            if "canonical" in rel and attrs_map.get("href"):
                self.canonical = attrs_map["href"]
        elif tag.lower() == "br":
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS and tag not in VOID_TAGS and self._skip:
            self._skip -= 1
            return
        if tag == "title":
            self._in_title = False
        elif tag == "article":
            self._article = max(0, self._article - 1)
        elif tag == "main":
            self._main = max(0, self._main - 1)
        if tag in BLOCK_TAGS:
            self._append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip or not data:
            return
        self._append(data)

    def _append(self, text: str) -> None:
        self.body_parts.append(text)
        if self._article or self._main:
            self.article_parts.append(text)


def clean_extracted(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_html(data: bytes, base_url: str = "", content_type: str = "") -> Resource:
    text = _decode(data, content_type)
    parser = PageParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # A malformed page should still yield whatever was parsed before the error.
        pass

    title = (
        parser.meta.get("og:title")
        or parser.meta.get("twitter:title")
        or " ".join(parser.title_parts)
        or ""
    ).strip()
    description = (
        parser.meta.get("og:description")
        or parser.meta.get("twitter:description")
        or parser.meta.get("description")
        or ""
    ).strip()
    article_text = clean_extracted("".join(parser.article_parts))
    body_text = clean_extracted("".join(parser.body_parts))
    chosen = article_text if len(article_text) > max(500, len(body_text) * 0.35) else body_text

    image = parser.meta.get("og:image") or parser.meta.get("twitter:image") or ""
    if image and base_url:
        image = urllib.parse.urljoin(base_url, image)

    resource = Resource(
        kind="web" if base_url else "html",
        title=title,
        url=base_url,
        description=description,
        text=chosen,
        thumbnail=image,
        site=parser.meta.get("og:site_name", ""),
        author=parser.meta.get("author") or parser.meta.get("article:author") or "",
        published=human_date(
            parser.meta.get("article:published_time")
            or parser.meta.get("date")
            or parser.meta.get("og:published_time")
            or ""
        ),
        language=parser.language or "en",
    )
    if base_url and len(chosen) < 900:
        resource.warnings.append("Very little body text was extracted; the page may require JavaScript.")
    if not chosen:
        resource.warnings.append("No readable body text was found on the page.")
    return resource


def _run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AcquireError(f"required tool not installed: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AcquireError(f"{command[0]} timed out after {timeout}s") from exc


def _pick_subtitle(meta: dict[str, Any]) -> tuple[str, bool]:
    manual = meta.get("subtitles") or {}
    auto = meta.get("automatic_captions") or {}

    def choose(tracks: dict[str, Any]) -> str:
        for wanted in ("en", "en-US", "en-GB", "en-orig"):
            if wanted in tracks:
                return wanted
        for key in tracks:
            if str(key).lower().startswith("en"):
                return str(key)
        return str(next(iter(tracks))) if tracks else ""

    manual_lang = ""
    if manual:
        manual_lang = choose(manual)
        if manual_lang:
            return manual_lang, True
    if auto:
        auto_lang = choose(auto)
        if auto_lang:
            return auto_lang, False
    return "", False


def parse_vtt(content: str) -> list[tuple[str, str]]:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if "-->" in line:
            match = re.match(r"(\d{1,2}:\d{2}:\d{2})[.,]\d{3}\s+-->", line)
            timestamp = match.group(1) if match else ""
            index += 1
            parts: list[str] = []
            while index < len(lines) and lines[index].strip():
                piece = lines[index].strip()
                if not piece.upper().startswith(("NOTE", "STYLE", "REGION")):
                    piece = re.sub(r"<[^>]+>", "", piece)
                    piece = re.sub(r"\{\\[^}]*\}", "", piece)
                    piece = html.unescape(piece).strip()
                    if piece:
                        parts.append(piece)
                index += 1
            text = " ".join(parts).strip()
            if text:
                cues.append((timestamp, text))
        index += 1

    merged: list[tuple[str, str]] = []
    for timestamp, text in cues:
        if not merged:
            merged.append((timestamp, text))
            continue
        previous_timestamp, previous = merged[-1]
        if text == previous:
            continue
        if text.startswith(previous):
            merged[-1] = (previous_timestamp, text)
            continue
        if previous.endswith(text):
            continue
        merged.append((timestamp, text))
    return merged


def _timestamp_seconds(value: str) -> int:
    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _compact_timestamp(value: str) -> str:
    parts = value.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return value
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    if len(numbers) == 2:
        return value
    return value


def format_transcript(cues: list[tuple[str, str]], target_chars: int = 460, max_span: int = 40) -> str:
    if not cues:
        return ""
    paragraphs: list[str] = []
    buffer: list[str] = []
    start_stamp = _compact_timestamp(cues[0][0])
    start_seconds = _timestamp_seconds(cues[0][0])
    length = 0
    for timestamp, text in cues:
        if not buffer:
            start_stamp = _compact_timestamp(timestamp)
            start_seconds = _timestamp_seconds(timestamp)
        buffer.append(text)
        length += len(text) + 1
        current = _timestamp_seconds(timestamp)
        if length >= target_chars or (current - start_seconds) >= max_span:
            paragraphs.append(f"[{start_stamp}] " + " ".join(buffer))
            buffer = []
            length = 0
    if buffer:
        paragraphs.append(f"[{start_stamp}] " + " ".join(buffer))
    return "\n\n".join(paragraphs)


def acquire_youtube(url: str, cfg: dict[str, Any]) -> Resource:
    if not shutil.which("yt-dlp"):
        raise AcquireError("yt-dlp is required for YouTube links (install it with your package manager)")

    metadata_proc = _run(
        ["yt-dlp", "-J", "--no-warnings", "--no-playlist", url],
        timeout=150,
    )
    if metadata_proc.returncode != 0:
        detail = (metadata_proc.stderr or "").strip().splitlines()
        raise AcquireError("yt-dlp could not read this video" + (f": {detail[-1]}" if detail else ""))
    try:
        meta = json.loads(metadata_proc.stdout)
    except json.JSONDecodeError as exc:
        raise AcquireError(f"yt-dlp returned unreadable metadata: {exc}") from exc

    title = str(meta.get("title") or "").strip()
    author = str(meta.get("uploader") or meta.get("channel") or "").strip()
    duration = int(meta.get("duration") or 0)
    upload_date = human_date(str(meta.get("upload_date") or ""))
    thumbnail = str(meta.get("thumbnail") or "")
    description = str(meta.get("description") or "").strip()
    language = str(meta.get("language") or "en")

    chapters = []
    for chapter in meta.get("chapters") or []:
        start = int(float(chapter.get("start_time") or 0))
        stamp = f"{start // 3600:02d}:{(start % 3600) // 60:02d}:{start % 60:02d}" if start >= 3600 else f"{start // 60:02d}:{start % 60:02d}"
        chapters.append(f"[{stamp}] {chapter.get('title', '').strip()}")

    transcript = ""
    warnings: list[str] = []
    subtitle_lang, is_manual = _pick_subtitle(meta)
    if subtitle_lang:
        with tempfile.TemporaryDirectory(prefix="beam-subs-") as tmp:
            command = [
                "yt-dlp",
                "--skip-download",
                "--no-warnings",
                "--no-playlist",
                "--sub-langs", subtitle_lang,
                "--sub-format", "vtt",
                "-o", str(Path(tmp) / "%(id)s.%(ext)s"),
            ]
            command.append("--write-subs" if is_manual else "--write-auto-subs")
            command.append(url)
            sub_proc = _run(command, timeout=180)
            vtt_files = sorted(Path(tmp).glob("*.vtt"), key=lambda path: path.stat().st_size, reverse=True)
            if vtt_files:
                transcript = format_transcript(parse_vtt(vtt_files[0].read_text(encoding="utf-8", errors="replace")))
            elif sub_proc.returncode != 0:
                detail = (sub_proc.stderr or "").strip().splitlines()
                warnings.append("Could not download captions" + (f": {detail[-1]}" if detail else ""))
    if not transcript:
        warnings.append("No caption track was available; the page is based on the video description only.")

    parts = []
    if description:
        parts.append("## Description\n\n" + description)
    if chapters:
        parts.append("## Chapters\n\n" + "\n".join(chapters))
    if transcript:
        parts.append("## Transcript (verbatim captions)\n\n" + transcript)
    text = "\n\n".join(parts).strip()

    return Resource(
        kind="youtube",
        title=title,
        url=str(meta.get("webpage_url") or url),
        author=author,
        published=upload_date,
        description=description[:2000],
        text=text,
        thumbnail=thumbnail,
        duration=duration,
        site="YouTube",
        language=language,
        warnings=warnings,
        extra={
            "video_id": meta.get("id", ""),
            "chapters": chapters,
            "view_count": meta.get("view_count"),
            "subtitle_language": subtitle_lang,
            "subtitle_source": "manual" if is_manual else ("auto" if subtitle_lang else ""),
        },
    )


def _pdf_text(path: Path) -> str:
    if not shutil.which("pdftotext"):
        raise AcquireError("pdftotext is required for PDFs (install poppler-utils)")
    proc = _run(["pdftotext", "-layout", str(path), "-"], timeout=120)
    if proc.returncode != 0:
        raise AcquireError("pdftotext could not read this PDF")
    return proc.stdout.strip()


def _docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise AcquireError(f"could not read DOCX file: {exc}") from exc
    xml = re.sub(r"</w:p>", "\n\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml).strip()


def _local_text(path: Path) -> str:
    data = path.read_bytes()
    return _decode(data).strip()


def acquire_url(url: str, cfg: dict[str, Any]) -> Resource:
    if is_youtube(url):
        return acquire_youtube(url, cfg)

    data, content_type, final_url, truncated = fetch_url(url, cfg)
    parsed_path = urllib.parse.urlparse(final_url or url).path.lower()
    if "application/pdf" in content_type.lower() or parsed_path.endswith(".pdf"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(data)
            handle.flush()
            text = _pdf_text(Path(handle.name))
        resource = Resource(
            kind="pdf",
            title=Path(parsed_path).stem.replace("-", " ").replace("_", " ").strip(),
            url=final_url or url,
            text=text,
            site=urllib.parse.urlparse(final_url or url).netloc,
        )
        if truncated:
            resource.warnings.append("The PDF download was truncated at the fetch limit.")
        return resource

    if "text/plain" in content_type.lower() and "html" not in content_type.lower():
        text = _decode(data, content_type)
        resource = Resource(kind="text", url=final_url or url, text=text, site=urllib.parse.urlparse(final_url or url).netloc)
        if truncated:
            resource.warnings.append("The response was truncated at the fetch limit.")
        return resource

    resource = parse_html(data, base_url=final_url or url, content_type=content_type)
    if truncated:
        resource.warnings.append("The page was truncated at the fetch limit.")
    return resource


def acquire_file(path: Path, cfg: dict[str, Any]) -> Resource:
    suffix = path.suffix.lower()
    if suffix in {".url", ".webloc", ".desktop"}:
        raw = _local_text(path)
        urls = extract_urls(raw)
        if not urls:
            raise AcquireError(f"no URL found in {path.name}")
        return acquire_url(urls[0], cfg)

    if suffix in {".html", ".htm"}:
        return parse_html(path.read_bytes(), base_url="")

    if suffix == ".pdf":
        return Resource(
            kind="pdf",
            title=path.stem.replace("-", " ").replace("_", " ").strip(),
            text=_pdf_text(path),
        )

    if suffix == ".docx":
        return Resource(kind="docx", title=path.stem.replace("-", " ").replace("_", " ").strip(), text=_docx_text(path))

    if suffix in TEXT_SUFFIXES or suffix == "":
        text = _local_text(path)
    else:
        # Unknown binary: only accept it if it decodes to mostly printable text.
        raw = path.read_bytes()
        text = _decode(raw)
        printable = sum(1 for char in text[:4000] if char.isprintable() or char in "\n\t\r")
        if text and printable / max(1, len(text[:4000])) < 0.85:
            raise AcquireError(f"unsupported binary file type: {suffix or 'no extension'}")
        if not text:
            raise AcquireError(f"could not extract text from {path.name}")

    stripped = text.strip()
    if looks_like_url(stripped):
        return acquire_url(stripped, cfg)

    title = path.stem.replace("-", " ").replace("_", " ").strip()
    return Resource(
        kind="text" if suffix != ".md" else "note",
        title=title,
        text=stripped,
        extra={"filename": path.name},
    )


def acquire(path: Path, cfg: dict[str, Any]) -> Resource:
    if not path.exists():
        raise AcquireError(f"file not found: {path}")
    if not path.is_file():
        raise AcquireError(f"not a file: {path}")
    try:
        return acquire_file(path, cfg)
    except AcquireError:
        raise
    except Exception as exc:  # keep the watcher alive on any single bad input
        raise AcquireError(f"unexpected error while reading {path.name}: {exc}") from exc


def download_image(url: str, cfg: dict[str, Any], destination_stem: Path) -> str:
    """Download a cover image beside the page. Returns the local filename or ''."""
    if not url:
        return ""
    max_bytes = int(cfg.get("limits", {}).get("max_image_bytes", 2_500_000))
    try:
        data, content_type, _, _ = fetch_url(url, cfg, max_bytes=max_bytes)
    except AcquireError:
        return ""
    lowered = content_type.lower()
    extension = ""
    if "image/png" in lowered:
        extension = ".png"
    elif "image/webp" in lowered:
        extension = ".webp"
    elif "image/gif" in lowered:
        extension = ".gif"
    elif "image/jpeg" in lowered or "image/jpg" in lowered:
        extension = ".jpg"
    else:
        guessed = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if guessed in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            extension = ".jpg" if guessed == ".jpeg" else guessed
    if not extension or not data:
        return ""
    output = destination_stem.with_name(destination_stem.name + "-cover" + extension)
    output.write_bytes(data)
    return output.name
