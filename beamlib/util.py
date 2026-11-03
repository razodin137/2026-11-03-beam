"""Small, dependency-free helpers used across Beam."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(value: str, max_len: int = 64, fallback: str = "untitled") -> str:
    original = str(value or "")
    value = unicodedata.normalize("NFKD", original)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    if not value:
        digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
        value = f"{fallback}-{digest}"
    return value[:max_len].strip("-") or fallback


def safe_filename(value: str, max_len: int = 90) -> str:
    value = Path(str(value or "")).name
    value = value.replace("\x00", "").strip()
    value = re.sub(r"[^\w.\- ()\[\]]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:max_len] or "untitled"


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    suffix = suffix if suffix.startswith(".") or not suffix else f".{suffix}"
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    for index in range(2, 10000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free filename in {directory}")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def human_duration(seconds: float | int | None) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def human_date(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%b %d, %Y")
    except ValueError:
        pass
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%b %d, %Y", "%B %d, %Y"):
        try:
            parsed = datetime.strptime(text.replace("Z", "+0000") if fmt.endswith("%z") else text, fmt)
            return parsed.strftime("%b %d, %Y")
        except ValueError:
            continue
    return text[:10]


def smart_truncate(text: str, limit: int) -> tuple[str, bool]:
    """Keep the beginning and sample evenly through the rest, instead of
    chopping off everything after the first N characters."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    marker = "\n\n[… middle of source sampled …]\n\n"
    head_len = max(1, int(limit * 0.45))
    tail_budget = max(0, limit - head_len - len(marker))
    head = text[:head_len]
    rest = text[head_len:]
    if not rest:
        return head, True
    block = max(200, tail_budget // 12)
    span = max(block, len(rest) // 12)
    pieces: list[str] = []
    used = 0
    offset = 0
    while offset < len(rest) and used < tail_budget:
        piece = rest[offset : offset + block].strip()
        if piece:
            allowance = tail_budget - used
            if len(piece) > allowance:
                piece = piece[:allowance].rstrip()
            if piece:
                pieces.append(piece)
                used += len(piece)
        offset += span
    sampled = "\n\n".join(pieces)
    return head.rstrip() + marker + sampled, True


def normalize_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def quote_match(source_normalized: str, quote: str) -> tuple[float, bool]:
    """Return (share of quote shingles found in source, verified?).

    Five-word shingles make this tolerant of punctuation/whitespace changes
    while still catching quotes that were quietly paraphrased.
    """
    normalized = normalize_for_match(quote)
    if not normalized:
        return 0.0, False
    if normalized in source_normalized:
        return 1.0, True
    words = normalized.split()
    if len(words) < 5:
        return 0.0, False
    width = 5
    shingles = [" ".join(words[i : i + width]) for i in range(len(words) - width + 1)]
    if not shingles:
        return 0.0, False
    found = sum(1 for shingle in shingles if shingle in source_normalized)
    ratio = found / len(shingles)
    return round(ratio, 3), ratio >= 0.55


def _balanced_candidates(text: str) -> list[str]:
    """Find complete top-level JSON objects/arrays inside prose, in order."""
    objects: list[str] = []
    stack: list[str] = []
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            if not stack:
                start = index
            stack.append(char)
        elif char in "]}":
            if stack:
                opener = stack.pop()
                if (opener, char) not in {("[", "]"), ("{", "}")}:
                    stack = []
                    start = -1
                    continue
                if not stack and start != -1:
                    objects.append(text[start : index + 1])
                    start = -1
    return objects


def parse_json_loose(raw: str) -> Any | None:
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    # Prefer the last complete object: thinking models sometimes print an
    # example object in their reasoning before the real answer.
    candidates.extend(reversed(_balanced_candidates(text)))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                continue
    return None


def snippet(value: str, length: int = 240) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "…"
