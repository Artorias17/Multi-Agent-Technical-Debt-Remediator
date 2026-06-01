import json
import re
import os
from openai import OpenAI

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8192"))
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


def parse_json(text: str) -> dict:
    """
    Robustly extract JSON from an LLM response.
    Handles: thinking blocks (<think>...</think>), markdown fences, leading prose.
    """
    raw = text.strip()

    # Strip thinking blocks (Qwen3 and similar models)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

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
