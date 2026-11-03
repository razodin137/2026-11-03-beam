"""Minimal Ollama HTTP client (native /api/chat, JSON mode)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .util import parse_json_loose


class OllamaError(RuntimeError):
    pass


def _base(cfg: dict[str, Any]) -> str:
    return str(cfg.get("api", {}).get("base_url", "http://127.0.0.1:11434")).rstrip("/")


def _timeout(cfg: dict[str, Any]) -> float:
    return float(cfg.get("api", {}).get("timeout_seconds", 600))


def api_version(cfg: dict[str, Any]) -> str:
    url = f"{_base(cfg)}/api/version"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload.get("version", "unknown"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise OllamaError(f"cannot reach Ollama at {_base(cfg)}: {exc}") from exc


def list_models(cfg: dict[str, Any]) -> list[str]:
    url = f"{_base(cfg)}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise OllamaError(f"cannot list Ollama models: {exc}") from exc
    return sorted(str(item.get("name", "")) for item in payload.get("models", []))


def _post(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{_base(cfg)}/api/chat"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=_timeout(cfg)) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("error"):
                raise OllamaError(str(data["error"]))
            return data
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:800]
            except Exception:
                pass
            last_error = OllamaError(f"Ollama HTTP {exc.code}: {detail or exc.reason}")
            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = OllamaError(f"cannot reach Ollama at {_base(cfg)}: {exc.reason}")
        except (TimeoutError, json.JSONDecodeError) as exc:
            last_error = OllamaError(f"bad response from Ollama: {exc}")
        if attempt < 3:
            time.sleep(2 * attempt)
    raise last_error or OllamaError("Ollama request failed")


def chat(
    cfg: dict[str, Any],
    model: str,
    messages: list[dict[str, str]],
    *,
    json_mode: bool = True,
    num_predict: int | None = None,
    temperature: float | None = None,
    think: bool | None = None,
) -> dict[str, Any]:
    models_cfg = cfg.get("models", {})
    if num_predict is None:
        num_predict = int(models_cfg.get("extract_num_predict", 8000))
    if temperature is None:
        temperature = float(models_cfg.get("temperature", 0.2))
    if think is None:
        think = models_cfg.get("think", False)

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": int(num_predict)},
    }
    if json_mode:
        payload["format"] = "json"
    if think is not None:
        payload["think"] = bool(think)

    try:
        data = _post(cfg, payload)
    except OllamaError as exc:
        # Older models/servers may reject the `think` flag. Retry once without it.
        if think is not None and "think" in str(exc).lower():
            payload.pop("think", None)
            data = _post(cfg, payload)
        else:
            raise

    message = data.get("message") or {}
    content = str(message.get("content") or "")
    return {
        "content": content,
        "model": str(data.get("model") or model),
        "prompt_tokens": int(data.get("prompt_eval_count") or 0),
        "completion_tokens": int(data.get("eval_count") or 0),
        "duration_ms": int(float(data.get("total_duration") or 0) / 1_000_000),
        "done_reason": str(data.get("done_reason") or ""),
    }


def chat_json(
    cfg: dict[str, Any],
    model: str,
    messages: list[dict[str, str]],
    *,
    num_predict: int | None = None,
    temperature: float | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Ask for JSON, parse it, and make one repair attempt if needed."""
    result = chat(
        cfg,
        model,
        messages,
        json_mode=True,
        num_predict=num_predict,
        temperature=temperature,
    )
    parsed = parse_json_loose(result["content"])
    if parsed is not None:
        return parsed, result

    # Reasoning models can spend the whole budget thinking and get cut off
    # before the actual JSON. Give them a second, larger chance.
    if result.get("done_reason") == "length":
        base = int(num_predict or cfg.get("models", {}).get("extract_num_predict", 8000))
        bigger = min(max(base * 2, 4000), 16000)
        retried = chat(
            cfg,
            model,
            messages,
            json_mode=True,
            num_predict=bigger,
            temperature=temperature,
        )
        parsed = parse_json_loose(retried["content"])
        if parsed is not None:
            retried["prompt_tokens"] += result["prompt_tokens"]
            retried["completion_tokens"] += result["completion_tokens"]
            retried["duration_ms"] += result["duration_ms"]
            return parsed, retried
        result = retried

    repair_messages = list(messages) + [
        {"role": "assistant", "content": result["content"]},
        {
            "role": "user",
            "content": "Your previous reply was not valid JSON. Return only the JSON object, with no markdown fences or commentary.",
        },
    ]
    repaired = chat(
        cfg,
        model,
        repair_messages,
        json_mode=True,
        num_predict=num_predict,
        temperature=0,
    )
    parsed = parse_json_loose(repaired["content"])
    if parsed is None:
        raise OllamaError(f"{model} did not return parseable JSON: {repaired['content'][:300]}")
    repaired["prompt_tokens"] += result["prompt_tokens"]
    repaired["completion_tokens"] += result["completion_tokens"]
    repaired["duration_ms"] += result["duration_ms"]
    return parsed, repaired
