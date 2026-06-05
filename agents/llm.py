import json
import re
import os
from openai import OpenAI

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8192"))
NUM_CTX = int(os.environ.get("NUM_CTX", "32768"))
_DEBUG_LEVEL = (
    int(os.environ.get("DEBUG", "0"))
    if os.environ.get("DEBUG", "").isdigit()
    else (1 if os.environ.get("DEBUG", "").lower() in ("true", "yes") else 0)
)


def get_client() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("OLLAMA_API_KEY", ""),
    )


def complete(client: OpenAI, messages: list[dict], temperature: float = 0.1, **kwargs):
    kwargs.setdefault("max_tokens", MAX_TOKENS)
    kwargs.setdefault("extra_body", {"num_ctx": NUM_CTX})
    return client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=temperature,
        **kwargs,
    )


def debug_response(label: str, content: str) -> None:
    if _DEBUG_LEVEL >= 1:
        print(f"[DEBUG:{label}]\n{content}\n[/DEBUG]")


def debug_request(label: str, messages: list[dict]) -> None:
    if _DEBUG_LEVEL >= 2:
        body = "\n---\n".join(
            f"[{m['role'].upper()}]\n{m['content']}" for m in messages
        )
        print(f"[DEBUG:{label}:REQUEST]\n{body}\n[/DEBUG]")


def strip_thinking(text: str) -> str:
    """Remove thinking block content. Handles both <think>...</think> and
    responses where the API strips the opening tag but keeps </think>."""
    if "</think>" in text:
        return text.split("</think>", 1)[-1].strip()
    return text.strip()


def parse_json(text: str) -> dict:
    """
    Robustly extract JSON from an LLM response.
    Handles: thinking blocks, markdown fences, leading prose.
    """
    raw = strip_thinking(text)

    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Last resort: find first { ... } block
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group())

    raise json.JSONDecodeError("No JSON object found", raw, 0)
