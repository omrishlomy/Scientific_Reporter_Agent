"""
Runs a prompt through a large open-weights model hosted by Groq's free-tier API, as
the replacement for the `claude -p` calls the reporter used to make.

This replaces an earlier self-hosted approach (llama.cpp CPU inference on a free
GitHub Actions runner). That was measured, not assumed, to be a dead end: a 7B model
ran at 2.2-2.5 tokens/sec on 2 throttled CPU cores, took ~41 minutes for a single
synthesis call, used ~8.3GB RAM against a free runner's ~7GB budget even after
quantizing the KV cache, and the extraction call's JSON never finished within a sane
token budget. A bigger (~30GB) model would need proportionally MORE compute per
token, making CPU-only inference slower still, not faster -- more RAM alone does not
fix a compute-bound bottleneck. Groq runs the model on its own fast hardware instead,
which fixes the speed and memory problems at once, keeps the model genuinely
open-weight and large (openai/gpt-oss-120b, 120B params -- bigger than the original
~30GB target), and stays on Groq's free tier.

Needs GROQ_API_KEY set (a GitHub Actions secret in production; a local env var for
manual testing). Get a free key at https://console.groq.com/keys -- this is an account
Claude cannot create on your behalf.

    from local_llm import run_prompt
    text = run_prompt(system_prompt_text, user_content, max_tokens=1400)
"""

from __future__ import annotations

import os

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"
DEFAULT_TEMPERATURE = 0.4   # favors consistency for both prose synthesis and strict-JSON extraction


def run_prompt(system_prompt: str, user_content: str, max_tokens: int,
                temperature: float = DEFAULT_TEMPERATURE, json_schema: dict | None = None) -> str:
    """Runs one single-turn chat completion. Returns the model's text response.

    json_schema, when given, is passed as a strict `response_format` -- gpt-oss-120b
    supports constrained decoding, which makes the response STRUCTURALLY guaranteed
    to match the schema (right field names, right types, right nesting). This is a
    real fix, not a mitigation, for the schema-drift problem hit repeatedly with
    prompt-only instructions (both Claude and the retired local Qwen model
    occasionally invented their own field names despite explicit instructions not
    to). Pass a plain JSON Schema object (properties/required/additionalProperties);
    this wraps it in the request shape Groq expects.

    Raises RuntimeError on any non-2xx response or missing API key, so a caller's
    existing try/except + retry logic (see run_reporter_cloud.py) keeps working
    unchanged from the llama.cpp-based version."""
    api_key = os.environ.get("GROQ_API_KEY")
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
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": json_schema},
        }

    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=120,   # a real API call, not CPU inference -- should complete in seconds
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Groq API error {resp.status_code}: {resp.text[:1000]}")

    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


if __name__ == "__main__":
    import sys
    print(run_prompt(sys.argv[1], sys.argv[2], max_tokens=200))
