"""Gemini API wrapper built on the modern google-genai SDK.

Resilience model (tuned for model-overload weather):
  * Pinned primary model (GEMINI_MODEL, default gemini-2.5-flash) — never an alias
    like *-latest. A fallback model (GEMINI_FALLBACK_MODEL or GEMINI_MODEL_FALLBACK,
    default gemini-2.5-flash-lite) is used when the primary stays overloaded.
  * Multiple API keys round-robined per call to spread load.
  * Error-aware retries (exponential backoff + jitter, env-tunable):
      503 / 5xx  -> model OVERLOAD: keep the SAME key, back off; after 2 consecutive
                    503s on the current model, switch to the fallback model. If both
                    fail, raise (the caller skips just that article and continues).
      429        -> quota / rate-limit: rotate to the next key + back off; if ALL
                    keys are 429, sleep a longer cooldown instead of looping fast.
      400/401/403-> not transient, raise immediately.
  * Always requests JSON; tolerates code fences / stray text around the JSON.

Env knobs (all optional): GEMINI_MODEL, GEMINI_FALLBACK_MODEL / GEMINI_MODEL_FALLBACK,
GEMINI_MIN_SECONDS_BETWEEN_CALLS, GEMINI_MAX_RETRIES, GEMINI_RETRY_BASE_SECONDS,
GEMINI_RETRY_MAX_SECONDS, GEMINI_RETRY_JITTER, GEMINI_COOLDOWN_AFTER_429_SECONDS.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from core import load_settings

logger = logging.getLogger(__name__)

NON_RETRYABLE_CODES = (400, 401, 403)  # bad request / auth — never transient
DEFAULT_FALLBACK_MODEL = "gemini-2.5-flash-lite"


class GeminiError(RuntimeError):
    """Raised when Gemini cannot produce usable JSON (config, API, or parsing)."""


class GeminiClient:
    """google.genai wrapper with key rotation, backoff and a fallback model."""

    def __init__(self, api_keys: list[str] | None = None, model: str | None = None) -> None:
        settings = load_settings()
        keys = api_keys or _load_keys()
        if not keys:
            raise GeminiError("no Gemini API key set (GEMINI_API_KEY / GEMINI_API_KEY_2 ...)")
        self._clients = [genai.Client(api_key=k) for k in keys]
        self._idx = 0

        self._model = (model or os.environ.get("GEMINI_MODEL")
                       or settings.get("gemini_model") or "gemini-2.5-flash")
        # Fallback model: accept EITHER env spelling; never end up empty (defaults to lite).
        self._fallback_model = (
            os.environ.get("GEMINI_FALLBACK_MODEL")
            or os.environ.get("GEMINI_MODEL_FALLBACK")
            or settings.get("gemini_fallback_model")
            or DEFAULT_FALLBACK_MODEL
        ).strip()

        self._min_between = _env_float("GEMINI_MIN_SECONDS_BETWEEN_CALLS",
                                       settings.get("sleep_between_calls_seconds", 30))
        self._max_retries = _env_int("GEMINI_MAX_RETRIES", 5)
        self._retry_base = _env_float("GEMINI_RETRY_BASE_SECONDS", 30)
        self._retry_max = _env_float("GEMINI_RETRY_MAX_SECONDS", 300)
        self._jitter = _env_bool("GEMINI_RETRY_JITTER", True)
        self._cooldown_429 = _env_float("GEMINI_COOLDOWN_AFTER_429_SECONDS", 90)

        logger.info(
            "Gemini ready | keys=%d | primary=%s | fallback=%s | min_gap=%.0fs | "
            "retries=%d | backoff=%.0f-%.0fs | jitter=%s | 429_cooldown=%.0fs",
            len(self._clients), self._model, self._fallback_model, self._min_between,
            self._max_retries, self._retry_base, self._retry_max, self._jitter,
            self._cooldown_429,
        )

    def generate_json(self, prompt: str, system: str | None = None) -> dict:
        """Call Gemini and return its response parsed as a JSON object."""
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            system_instruction=system,
        )
        return self._parse_json(self._call_with_retry(prompt, config))

    def _call_with_retry(self, prompt: str, config: types.GenerateContentConfig) -> str:
        """Invoke the model with error-aware retries, key rotation and fallback."""
        model = self._model
        switched = False
        overload_streak = 0          # consecutive 503/5xx on the current model
        rate_streak = 0              # consecutive 429s (across key rotations)
        key_idx = self._idx          # round-robin a starting key for THIS call
        self._idx += 1

        for attempt in range(1, self._max_retries + 2):  # 1 .. max_retries (+1 final)
            time.sleep(self._min_between)                 # min gap between calls
            client = self._clients[key_idx % len(self._clients)]
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                return resp.text or ""
            except genai_errors.APIError as exc:
                code = getattr(exc, "code", None)
                if code in NON_RETRYABLE_CODES or attempt > self._max_retries:
                    raise

                if code == 429:                              # quota / rate limit
                    overload_streak = 0
                    rate_streak += 1
                    key_idx += 1                             # rotate to the next key
                    if rate_streak >= len(self._clients):    # every key is limited
                        logger.warning("Gemini 429 on all %d key(s); cooling down %ds",
                                       len(self._clients), int(self._cooldown_429))
                        time.sleep(self._cooldown_429)
                        rate_streak = 0
                    else:
                        logger.warning("Gemini 429 quota; rotating key, backoff (attempt %d/%d)",
                                       attempt, self._max_retries)
                        self._backoff(attempt)
                else:                                        # 503 / 5xx — model overload
                    rate_streak = 0
                    overload_streak += 1
                    # Two strikes on the current model -> move to the fallback model
                    # (NOT a key rotation: a 503 is backend capacity, not the key).
                    if overload_streak >= 2 and self._fallback_model and not switched:
                        switched = True
                        overload_streak = 0
                        model = self._fallback_model
                        logger.warning("Gemini %s x2 on primary; switching to fallback model %s",
                                       code, model)
                    logger.warning("Gemini %s overload; backoff same key (attempt %d/%d, model=%s)",
                                   code, attempt, self._max_retries, model)
                    self._backoff(attempt)
        raise GeminiError("exhausted Gemini retries")  # unreachable safeguard

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff with optional jitter: base * 2^(attempt-1), capped."""
        delay = min(self._retry_max, self._retry_base * (2 ** (attempt - 1)))
        if self._jitter:
            delay += random.uniform(0, 15)
        logger.info("Gemini backoff: sleeping %.0fs", delay)
        time.sleep(delay)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Parse the model's JSON, tolerating code fences and stray surrounding text."""
        text = _strip_code_fences(raw)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Salvage: decode the first balanced JSON value and ignore trailing/leading junk.
        starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if starts:
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[min(starts):])
                return obj
            except (json.JSONDecodeError, ValueError):
                pass
        raise GeminiError(f"Gemini returned non-JSON output:\n{raw}")


def _load_keys() -> list[str]:
    """Collect Gemini API keys from env for round-robin rotation.

    Accepts a comma-separated GEMINI_API_KEYS and/or GEMINI_API_KEY[_2.._5].
    """
    keys: list[str] = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
                 "GEMINI_API_KEY_4", "GEMINI_API_KEY_5"):
        value = os.environ.get(name, "").strip()
        if value:
            keys.append(value)
    seen: set[str] = set()
    return [k for k in keys if not (k in seen or seen.add(k))]  # dedupe, keep order


def _env_float(name: str, default) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _strip_code_fences(raw: str) -> str:
    """Remove a surrounding ```...``` / ```json block if the model added one."""
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):       # drop opening ``` or ```json
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):  # drop closing fence
        lines = lines[:-1]
    return "\n".join(lines).strip()
