"""
One place to talk to an LLM, so the crawler-side and FSD-side code share
retry/JSON-repair behaviour instead of each rolling their own.

Provider is chosen by config.LLM_PROVIDER:
  "anthropic" -> Claude via the anthropic SDK
  "groq"      -> Groq's OpenAI-compatible endpoint (the original setup)

Joining an FSD to a crawled UI graph is reasoning-heavy — matching prose
steps to screens, deciding which business rules are testable. Claude is
worth the switch for that; Groq stays available for cheap bulk passes.
"""
import json
import time
from typing import Any, Optional

import config


class LLMError(RuntimeError):
    pass


def extract_json(text: str, expect: str = "array") -> Any:
    """
    Pull JSON out of a model response that may be fenced or prefaced with
    prose. `expect` is "array" or "object" and decides which bracket pair we
    fall back to scanning for.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    open_c, close_c = ("[", "]") if expect == "array" else ("{", "}")
    start, end = text.find(open_c), text.rfind(close_c)
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise LLMError(f"No JSON {expect} found in response: {text[:200]!r}")


def _provider() -> str:
    return getattr(config, "LLM_PROVIDER", "groq").lower()


def get_client():
    provider = _provider()
    if provider == "anthropic":
        import anthropic
        key = getattr(config, "ANTHROPIC_API_KEY", None)
        if not key:
            raise LLMError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
        return anthropic.Anthropic(api_key=key)
    from openai import OpenAI
    key = getattr(config, "GROQ_API_KEY", None)
    if not key:
        raise LLMError("LLM_PROVIDER=groq but GROQ_API_KEY is not set.")
    return OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")


# Per-model output ceiling, read from the provider once per process. Models
# differ a lot — groq/compound caps completions at 8192 while
# llama-3.3-70b-versatile allows 32768 — and exceeding the cap is rejected
# outright rather than silently clamped by the API.
_LIMITS_CACHE: dict[str, Optional[int]] = {}


def model_max_output(client, model: str) -> Optional[int]:
    if model in _LIMITS_CACHE:
        return _LIMITS_CACHE[model]
    limit = None
    if _provider() != "anthropic":
        try:
            for m in client.models.list().data:
                if m.id == model:
                    limit = getattr(m, "max_completion_tokens", None)
                    break
        except Exception:  # noqa: BLE001 - listing is best-effort
            limit = None
    _LIMITS_CACHE[model] = limit
    return limit


def _rate_limit_kind(e: Exception) -> Optional[str]:
    """
    Groq reports two very different things as rate limits:
      - per-MINUTE (TPM): transient, and shrinking max_tokens makes the request
        fit, because the quota is charged on prompt + max_tokens.
      - per-DAY (TPD): waiting minutes cannot help (the message quotes hours),
        so retrying only burns time and hides the real problem.
    """
    msg = str(e).lower()
    if "per day" in msg or "tpd" in msg:
        return "daily"
    if "per minute" in msg or "tpm" in msg:
        return "minute"
    return None


def _is_retryable(e: Exception) -> bool:
    """
    Rate limits and transient server faults are worth retrying. A 400 telling
    us max_tokens is out of range will fail identically every time — retrying
    it just burns four rounds of backoff and buries the message that explains
    the fix.
    """
    status = getattr(e, "status_code", None) or getattr(e, "status", None)
    if status in (408, 409, 429, 500, 502, 503, 504):
        return True
    if status == 400:
        return False
    name = type(e).__name__.lower()
    if "ratelimit" in name or "timeout" in name or "connection" in name or "apistatus" in name:
        return True
    return status is None  # unknown/network-ish: worth one more go


def complete(client, system: str, user: str, model: Optional[str] = None,
             max_tokens: int = 8192, retries: int = 3) -> str:
    """Single completion, returning raw text. Retries transient errors only."""
    model = model or config.LLM_MODEL

    cap = model_max_output(client, model)
    if cap and max_tokens > cap:
        print(f"    note: max_tokens {max_tokens} exceeds {model}'s limit of {cap}; using {cap}.")
        max_tokens = cap

    last_err = None
    for attempt in range(retries):
        try:
            if _provider() == "anthropic":
                resp = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 - provider SDKs raise many types
            last_err = e
            kind = _rate_limit_kind(e)

            if kind == "daily":
                raise LLMError(
                    "Daily token quota exhausted for this model — waiting will not help "
                    "within this run. Switch models, upgrade the tier, or resume tomorrow.\n"
                    f"    {e}") from e

            if kind == "minute" and max_tokens > 1200:
                # The quota counts prompt + max_tokens, so asking for less
                # output is what actually makes the request fit.
                max_tokens = max(1200, max_tokens // 2)
                print(f"    Per-minute token limit hit; reducing max_tokens to {max_tokens} "
                      f"and retrying.")
                time.sleep(2)
                continue

            if not _is_retryable(e):
                # Print in full: the provider's message names the actual limit
                # or bad parameter, and truncating it hides the fix.
                raise LLMError(f"LLM call rejected (not retryable):\n    {e}") from e

            wait = 2 ** attempt
            print(f"    LLM call failed ({type(e).__name__}); retrying in {wait}s...\n"
                  f"      {str(e)[:400]}")
            time.sleep(wait)
    raise LLMError(f"LLM call failed after {retries} attempts:\n    {last_err}")


def complete_json(client, system: str, user: str, expect: str = "array",
                  model: Optional[str] = None, max_tokens: int = 8192,
                  retries: int = 3) -> Any:
    """
    Completion that must return JSON. A malformed response is retried with
    the parse error fed back, which recovers far more often than a blind retry.
    """
    last_err = None
    prompt = user
    for attempt in range(retries):
        text = complete(client, system, prompt, model=model, max_tokens=max_tokens, retries=retries)
        try:
            return extract_json(text, expect=expect)
        except (LLMError, json.JSONDecodeError) as e:
            last_err = e
            print(f"    Bad JSON from model (attempt {attempt + 1}): {str(e)[:120]}")
            prompt = (f"{user}\n\nYour previous reply could not be parsed as JSON "
                      f"({str(e)[:200]}). Reply with ONLY the raw JSON {expect}, no prose, "
                      f"no markdown fences, and ensure it is complete and balanced.")
    raise LLMError(f"Could not obtain valid JSON after {retries} attempts: {last_err}")
