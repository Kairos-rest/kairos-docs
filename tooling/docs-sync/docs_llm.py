"""Small OpenAI-compatible client for the private Labestia model route.

The docs job runs on sophios-vps, where nginx exposes Labestia on loopback and
injects the Cloudflare service credentials upstream. The pipeline therefore
needs no model API secret of its own.
"""

from __future__ import annotations

import http.client
import json
import urllib.request

DEFAULT_API_URL = "http://127.0.0.1:8090/v1"
DEFAULT_MODEL = "qwen3.6:35b-a3b"


def build_chat_request(api_url: str, model: str, system: str, user: str,
                       user_agent: str) -> urllib.request.Request:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        # Labestia exposes Qwen's reasoning separately. Without this flag a
        # short completion can spend its entire budget reasoning and leave the
        # OpenAI-compatible `content` field empty.
        "think": False,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", user_agent)
    return req


def parse_chat_completion(raw: dict, log) -> dict:
    try:
        text = raw["choices"][0]["message"]["content"].strip()
    except (AttributeError, KeyError, IndexError, TypeError):
        log("Labestia response had no message content")
        return {}

    if text.startswith("```"):
        text = text.strip("`")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        log(f"Labestia returned no JSON object. Raw: {text[:300]}")
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log(f"Labestia returned malformed JSON. Raw: {text[start:start + 300]}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def chat_json(api_url: str, model: str, timeout: float, user_agent: str,
              system: str, user: str, log) -> dict:
    """Return a JSON object, or `{}` for an unusable successful response.

    Transport errors deliberately propagate so the caller can retain its
    cursor and retry the release range on the next cron tick.
    """
    req = build_chat_request(api_url, model, system, user, user_agent)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode())
    except (ValueError, http.client.HTTPException) as e:
        log(f"Labestia returned an unreadable response body: {e}")
        return {}
    return parse_chat_completion(raw, log)


def model_is_available(response: dict, model: str) -> bool:
    advertised = response.get("data") or response.get("models") or []
    names = {
        item.get("id") or item.get("name") or item.get("model")
        for item in advertised
        if isinstance(item, dict)
    }
    return model in names


def require_model(api_url: str, model: str, timeout: float,
                  user_agent: str) -> None:
    req = urllib.request.Request(f"{api_url.rstrip('/')}/models")
    req.add_header("User-Agent", user_agent)
    with urllib.request.urlopen(req, timeout=min(timeout, 30)) as resp:
        raw = json.loads(resp.read().decode())
    if not isinstance(raw, dict) or not model_is_available(raw, model):
        raise ValueError(f"model {model!r} is not advertised by {api_url.rstrip('/')}/models")
