"""
Runs a prompt through openai/gpt-oss-120b on Groq's free tier -- the replacement for
the `claude -p` calls the reporter used to make, and for an earlier self-hosted
llama.cpp attempt that was measured infeasible on a CPU-only runner (2.2-2.5 tok/s,
41 min per call, over the RAM budget).

RATE LIMITS ARE THE BINDING CONSTRAINT. Groq's free tier for this model allows 30 RPM,
8K tokens/minute and 200K tokens/day. A single request larger than the per-minute budget
is rejected however long you wait. The first Railway deploy sent a full ~13K-token
digest and both the brief and the infographic silently failed on every run. So:
  - callers must send compact input (see digest_compact.py), not the raw digest;
  - every call reserves its estimated tokens in a shared rolling 60s window and waits
    if the budget is spent, so back-to-back calls pace themselves instead of 429ing;
  - 429s honour retry-after, 5xx and network errors back off and retry;
  - a truncated response (finish_reason "length") raises, instead of returning half a
    JSON document that fails to parse somewhere downstream with a confusing error.

Needs GROQ_API_KEY. GROQ_TPM overrides the per-minute budget (e.g. on a paid tier).

    from local_llm import run_prompt
    text = run_prompt(system_prompt_text, user_content, max_tokens=1400)
"""

from __future__ import annotations

import collections
import os
import threading
import time

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"
DEFAULT_TEMPERATURE = 0.4   # favours consistency for both prose synthesis and strict-JSON extraction

TPM_BUDGET = int(os.environ.get("GROQ_TPM", "8000"))
TPM_SAFETY = 0.85          # the token estimate below is approximate; keep headroom
CHARS_PER_TOKEN = 3.5      # conservative for scientific English -- underestimating is what trips 429s

_window: collections.deque = collections.deque()   # (monotonic time, tokens)
_lock = threading.Lock()   # the Railway service runs scheduled and on-demand reports concurrently


class RequestTooLarge(RuntimeError):
    """The request alone exceeds the per-minute budget, so no amount of waiting helps."""


def estimate_tokens(*texts: str) -> int:
    return int(sum(len(t) for t in texts) / CHARS_PER_TOKEN) + 1


def budget() -> int:
    return int(TPM_BUDGET * TPM_SAFETY)


def _reserve(tokens: int) -> None:
    if tokens > budget():
        raise RequestTooLarge(
            f"request needs ~{tokens} tokens but the per-minute budget is {budget()}; "
            "shrink the input")
    while True:
        with _lock:
            now = time.monotonic()
            while _window and now - _window[0][0] >= 60:
                _window.popleft()
            if sum(t for _, t in _window) + tokens <= budget():
                _window.append((now, tokens))
                return
            wait = 60 - (now - _window[0][0]) + 0.5
        time.sleep(max(wait, 0.5))


def run_prompt(system_prompt: str, user_content: str, max_tokens: int,
               temperature: float = DEFAULT_TEMPERATURE, json_schema: dict | None = None,
               reasoning_effort: str | None = "low", max_retries: int = 3) -> str:
    """Runs one single-turn chat completion and returns the model's text.

    json_schema, when given, is sent as a strict response_format: gpt-oss-120b supports
    constrained decoding, so the output is structurally guaranteed to match the schema.

    reasoning_effort: gpt-oss is a reasoning model and its reasoning tokens are generated
    as part of the completion, so they compete with the answer for max_tokens and for the
    per-minute budget. "low" for mechanical extraction, "medium" for real synthesis."""
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set -- create a free key at https://console.groq.com/keys")

    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": json_schema},
        }

    _reserve(estimate_tokens(system_prompt, user_content) + max_tokens)

    for attempt in range(max_retries + 1):
        last = attempt == max_retries
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body, timeout=120)
        except requests.RequestException as e:
            if last:
                raise RuntimeError(f"Groq request failed: {e}")
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 429 and not last:
            try:
                retry_after = float(resp.headers.get("retry-after") or 0)
            except ValueError:
                retry_after = 0
            time.sleep(min(retry_after or 20, 65) + 0.5)
            continue
        if resp.status_code >= 500 and not last:
            time.sleep(2 ** attempt)
            continue
        if (resp.status_code == 400 and "reasoning" in resp.text.lower()
                and "reasoning_effort" in body and not last):
            # Defensive: if Groq ever rejects reasoning_effort for this model or in
            # combination with structured output, degrade rather than fail the report.
            body.pop("reasoning_effort")
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Groq API error {resp.status_code}: {resp.text[:500]}")

        choice = resp.json()["choices"][0]
        content = (choice.get("message", {}).get("content") or "").strip()
        if choice.get("finish_reason") == "length":
            raise RuntimeError(
                f"Groq output truncated at max_completion_tokens={max_tokens} "
                "(reasoning tokens count toward it) -- raise the budget or shrink the input")
        if not content:
            raise RuntimeError("Groq returned an empty response")
        return content

    raise RuntimeError("Groq request failed after retries")


if __name__ == "__main__":
    import sys
    print(run_prompt(sys.argv[1], sys.argv[2], max_tokens=200))
