"""Gemini API wrapper built on the modern google-genai SDK.

Resilience model (tuned for production / model-overload weather):
  * Pinned model (GEMINI_MODEL, default gemini-2.5-flash) — never an alias like
    *-latest, which can change under you. A fallback model (GEMINI_FALLBACK_MODEL,
    default gemini-2.5-flash-lite) is used when overload persists.
  * Multiple API keys are round-robined per call to spread load.
  * Error-aware retries (exponential backoff + jitter, env-tunable):
      429 / quota / rate-limit  -> rotate to the next key, then back off
      503 / 5xx / overload      -> SAME key (rotating doesn't help backend load),
                                   back off; after 3 strikes cool down and switch
                                   to the fallback model
      400 / 401 / 403           -> not transient, raise immediately
  * Always requests JSON; tolerates code fences / stray text around the JSON.

Env knobs (all optional): GEMINI_MODEL, GEMINI_FALLBACK_MODEL,
GEMINI_MIN_SECONDS_BETWEEN_CALLS, GEMINI_MAX_RETRIES, GEMINI_RETRY_BASE_SECONDS,
GEMINI_RETRY_MAX_SECONDS, GEMINI_RETRY_JITTER, GEMINI_COOLDOWN_AFTER_503_SECONDS.
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
        self._fallback_model = (os.environ.get("GEMINI_FALLBACK_MODEL")
                                or settings.get("gemini_fallback_model") or "gemini-2.5-flash-lite")
        self._min_between = _env_float("GEMINI_MIN_SECONDS_BETWEEN_CALLS",
                                       settings.get("sleep_between_calls_seconds", 25))
        self._max_retries = _env_int("GEMINI_MAX_RETRIES", 5)
        self._retry_base = _env_float("GEMINI_RETRY_BASE_SECONDS", 20)
        self._retry_max = _env_float("GEMINI_RETRY_MAX_SECONDS", 300)
        self._jitter = _env_bool("GEMINI_RETRY_JITTER", True)
        self._cooldown_503 = _env_float("GEMINI_COOLDOWN_AFTER_503_SECONDS", 180)
        logger.info("Gemini: %d key(s) | model=%s fallback=%s | min_gap=%.0fs retries=%d",
                    len(self._clients), self._model, self._fallback_model,
                    self._min_between, self._max_retries)

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
        overload_streak = 0
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
                if code == 429:                           # quota / rate limit
                    key_idx += 1                          # rotate key
                    logger.warning("Gemini 429 quota; rotating key, backoff (attempt %d/%d)",
                                   attempt, self._max_retries)
                    self._backoff(attempt)
                else:                                     # 503 / 5xx overload
                    overload_streak += 1
                    if overload_streak >= 3 and self._fallback_model and not switched:
                        switched = True
                        model = self._fallback_model
                        logger.warning("Gemini %s persists; cooling down %ds, then fallback model %s",
                                       code, int(self._cooldown_503), model)
                        time.sleep(self._cooldown_503)
                    else:
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
