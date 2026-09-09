"""
Runs a prompt through a self-hosted open-weights model via the llama.cpp CLI, as a
drop-in replacement for the `claude -p` calls the reporter used to make. Built for a
GitHub Actions free runner: 2 CPU cores, ~7GB RAM, no persistent process between runs
-- so this shells out to `llama-cli` once per call rather than running a server.

Requires:
  - a llama.cpp CLI build (LLAMA_CLI env var, or bin/llama-cli on PATH)
  - a GGUF model file (LOCAL_MODEL_PATH env var)

    from local_llm import run_prompt
    text = run_prompt(system_prompt_text, user_content, n_predict=1400)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

DEFAULT_CTX = 24000     # covers a typical weekly digest (~15-20K tokens) plus output
DEFAULT_TEMP = "0.4"    # lower than llama.cpp's 0.8 default -- favors consistency for
                         # both the prose synthesis and the strict-JSON extraction call


def _resolve_llama_cli() -> str:
    path = os.environ.get("LLAMA_CLI")
    if path:
        return path
    # Fall back to a PATH lookup so this also works when the binary was apt/brew
    # installed rather than hand-placed by the workflow.
    return "llama-cli"


def _resolve_model() -> str:
    path = os.environ.get("LOCAL_MODEL_PATH")
    if not path:
        raise RuntimeError("LOCAL_MODEL_PATH is not set -- point it at the GGUF model file")
    return path


def run_prompt(system_prompt: str, user_content: str, n_predict: int, ctx: int = DEFAULT_CTX) -> str:
    """Runs one single-turn completion. Returns the model's raw text output.

    Writes the (potentially large) user content to a temp file and passes it via -f
    rather than -p, since a command-line argument has an OS-imposed length limit that
    a 15-20K token digest would exceed."""
    llama_cli = _resolve_llama_cli()
    model = _resolve_model()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sys.txt", delete=False, encoding="utf-8") as sf:
        sf.write(system_prompt)
        sys_path = sf.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".user.txt", delete=False, encoding="utf-8") as uf:
        uf.write(user_content)
        user_path = uf.name

    threads = os.environ.get("LOCAL_LLM_THREADS", "2")   # match the target runner's 2 cores

    cmd = [
        llama_cli, "-m", model,
        "-sysf", sys_path,
        "-f", user_path,
        "-st", "--no-display-prompt", "--simple-io",
        "-n", str(n_predict), "-c", str(ctx),
        "--temp", DEFAULT_TEMP,
        "-t", threads, "-tb", threads,
        "--no-warmup",
        # Quantized KV cache: measured ~8.9GB resident with the f16 default at a
        # 24K context on this model, which does not fit a free GitHub Actions
        # runner's ~7GB. q8_0 roughly halves that with negligible quality loss.
        # Requires flash attention, which q8_0/q4_0 KV cache depends on in llama.cpp.
        "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=int(os.environ.get("LOCAL_LLM_TIMEOUT", "2400")),   # 40 min ceiling
        )
    finally:
        os.unlink(sys_path)
        os.unlink(user_path)

    if result.returncode != 0:
        raise RuntimeError(f"llama-cli exited {result.returncode}: {result.stderr[-2000:]}")

    return result.stdout.strip()


if __name__ == "__main__":
    # Quick manual smoke test: python local_llm.py "system prompt" "user text"
    print(run_prompt(sys.argv[1], sys.argv[2], n_predict=200))
