from agents.llm import get_client, complete, debug_response, debug_request, parse_json
from state import PipelineState

_SYSTEM = (
    "You are a {language} code analyst. "
    "Describe the given function concisely. "
    "Respond ONLY with valid JSON — no prose, no markdown fences."
)

_USER = """\
Describe the following function.

Return a JSON object with this exact shape:
{{
  "description": "<one or two sentences explaining what the function does>",
  "parameters": [
    {{"name": "<param name>", "type": "<type or 'unknown'>", "purpose": "<brief purpose>"}}
  ],
  "returns": "<what the function returns, or 'void' / 'None'>"
}}

Function source:
{source}
"""


def _describe_function(client, language: str, fn: dict) -> dict:
    messages = [
        {"role": "system", "content": _SYSTEM.format(language=language)},
        {"role": "user", "content": _USER.format(source=fn["source"])},
    ]
    debug_request("Summarizer", messages)
    resp = complete(client, messages, temperature=0.1)
    raw = resp.choices[0].message.content.strip()
    debug_response("Summarizer", raw)

    try:
        result = parse_json(raw)
    except Exception:
        result = {"description": raw, "parameters": [], "returns": "unknown"}

    result["function_name"] = fn["name"]
    return result


def summarizer_node(state: PipelineState) -> dict:
    ctx = state["context"]
    language = ctx.get("language", "unknown")
    functions = ctx.get("functions", [])

    if not functions:
        print("[Summarizer] No functions to describe — returning empty summary")
        return {"summary": []}

    client = get_client()
    fn = functions[0]
    print(f"[Summarizer] Describing function: {fn['name']}")
    return {"summary": [_describe_function(client, language, fn)]}
