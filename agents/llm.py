import os
from openai import OpenAI

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
_DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


def get_client() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("OLLAMA_API_KEY", ""),
    )


def debug_response(label: str, content: str) -> None:
    if _DEBUG:
        print(f"[DEBUG:{label}]\n{content}\n[/DEBUG]")
