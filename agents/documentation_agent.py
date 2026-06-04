import re
import textwrap
from pathlib import Path

from state import PipelineState

import tree_sitter_java as tsjava
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
import tree_sitter_python as tspy
import tree_sitter_c_sharp as tscs
from tree_sitter import Language, Parser

from agents.llm import get_client, complete, debug_response, debug_request

# ── Language parsers (reuse same registry pattern as context_agent) ──

_PARSERS: dict[str, Parser] = {
    "java":       Parser(Language(tsjava.language())),
    "javascript": Parser(Language(tsjs.language())),
    "typescript": Parser(Language(tsts.language_typescript())),
    "python":     Parser(Language(tspy.language())),
    "csharp":     Parser(Language(tscs.language())),
}

_FUNCTION_NODES: dict[str, set[str]] = {
    "java":       {"method_declaration", "constructor_declaration"},
    "javascript": {"function_declaration", "method_definition", "arrow_function"},
    "typescript": {"function_declaration", "method_definition", "arrow_function"},
    "python":     {"function_definition"},
    "csharp":     {"method_declaration", "constructor_declaration"},
}

# ── Docstring formatters ─────────────────────────────────────

def _format_docstring(language: str, entry: dict) -> str:
    desc = entry.get("description", "")
    params = entry.get("parameters", [])
    returns = entry.get("returns", "void")

    if language in ("java", "javascript", "typescript"):
        lines = ["/**", f" * {desc}", " *"]
        for p in params:
            lines.append(f" * @param {p.get('name', '_')} {p.get('purpose', '')}")
        if returns not in ("void", "None", "nothing", ""):
            lines.append(f" * @returns {returns}")
        lines.append(" */")
        return "\n".join(lines)

    if language == "csharp":
        lines = [f"/// <summary>", f"/// {desc}", "/// </summary>"]
        for p in params:
            lines.append(f"/// <param name=\"{p.get('name', '_')}\">{p.get('purpose', '')}</param>")
        if returns not in ("void", "None", "nothing", ""):
            lines.append(f"/// <returns>{returns}</returns>")
        return "\n".join(lines)

    if language == "python":
        lines = [f'"""', desc, ""]
        if params:
            lines.append("Args:")
            for p in params:
                lines.append(f"    {p.get('name', '_')}: {p.get('purpose', '')}")
        if returns not in ("None", "nothing", ""):
            lines.append("")
            lines.append("Returns:")
            lines.append(f"    {returns}")
        lines.append('"""')
        return "\n".join(lines)

    # fallback: block comment
    return f"/* {desc} */"


def _find_function_node(tree, source_bytes: bytes, name: str, fn_node_types: set[str]):
    """Walk the tree and return the node for the named function, or None."""
    def walk(node):
        if node.type in fn_node_types:
            for child in node.children:
                if child.type == "identifier":
                    node_name = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                    if node_name == name:
                        return node
            parent = node.parent
            if parent and parent.type == "variable_declarator":
                for child in parent.children:
                    if child.type == "identifier":
                        node_name = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        if node_name == name:
                            return node
        for child in node.children:
            result = walk(child)
            if result is not None:
                return result
        return None
    return walk(tree.root_node)


def _has_existing_docstring(node, source_bytes: bytes, language: str) -> tuple[bool, int, int]:
    """
    Check if the node is immediately preceded by a docstring/comment.
    Returns (found, start_byte, end_byte) of the comment to replace.
    """
    # Look at the previous sibling in the parent's children list
    parent = node.parent
    if parent is None:
        return False, 0, 0

    siblings = parent.children
    idx = next((i for i, c in enumerate(siblings) if c == node), None)
    if idx is None or idx == 0:
        return False, 0, 0

    prev = siblings[idx - 1]
    if language == "python" and node.type == "function_definition":
        # Python docstring is the first expression_statement child inside the body
        body = next((c for c in node.children if c.type == "block"), None)
        if body and body.children:
            first = body.children[0]
            if first.type == "expression_statement":
                inner = first.children[0] if first.children else None
                if inner and inner.type == "string":
                    return True, inner.start_byte, inner.end_byte
        return False, 0, 0

    if prev.type in ("block_comment", "line_comment", "comment"):
        return True, prev.start_byte, prev.end_byte

    return False, 0, 0


def _insert_docstring(source: str, language: str, fn_name: str, docstring: str) -> str:
    """Insert or replace a docstring for the named function."""
    parser = _PARSERS.get(language)
    if parser is None:
        return source

    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    fn_node_types = _FUNCTION_NODES.get(language, set())
    node = _find_function_node(tree, source_bytes, fn_name, fn_node_types)

    if node is None:
        return source   # function not found; leave untouched

    has_doc, doc_start, doc_end = _has_existing_docstring(node, source_bytes, language)

    if language == "python":
        # Docstring goes inside the function body, after the def line
        body = next((c for c in node.children if c.type == "block"), None)
        if body is None:
            return source
        indent = " " * (node.start_point[1] + 4)
        indented_doc = textwrap.indent(docstring, indent)
        if has_doc:
            # Replace existing docstring inside body
            new_bytes = source_bytes[:doc_start] + indented_doc.encode("utf-8") + source_bytes[doc_end:]
        else:
            # Insert after the colon / first child of block
            insert_at = body.start_byte + 1   # just after the opening {
            # Find first newline after block start
            nl = source_bytes.find(b"\n", body.start_byte)
            insert_at = nl + 1 if nl != -1 else body.start_byte + 1
            new_bytes = source_bytes[:insert_at] + (indent + docstring + "\n").encode("utf-8") + source_bytes[insert_at:]
        return new_bytes.decode("utf-8", errors="replace")

    # Java / JS / TS / C# — docstring precedes the function
    indent = " " * node.start_point[1]
    indented_doc = textwrap.indent(docstring, indent)

    if has_doc:
        new_bytes = source_bytes[:doc_start] + indented_doc.encode("utf-8") + source_bytes[doc_end:]
    else:
        insert_at = node.start_byte
        new_bytes = source_bytes[:insert_at] + (indented_doc + "\n" + indent).encode("utf-8") + source_bytes[insert_at:]

    return new_bytes.decode("utf-8", errors="replace")


# ── LLM helpers ──────────────────────────────────────────────

_DOCSTRING_SYSTEM = (
    "You are a {language} documentation expert. "
    "Respond ONLY with valid JSON — no prose, no markdown fences."
)

_DOCSTRING_USER = """\
Generate a docstring for this {language} function.
Return JSON: {{"description": "...", "parameters": [...], "returns": "..."}}

Function:
{source}
"""

_CHANGELOG_SYSTEM = "You produce concise conventional-commit messages. No prose."
_CHANGELOG_USER = """\
Write a single conventional-commit message for this fix.
Format: fix(<filename>): <one sentence>

File: {filename}
Issues resolved: {issues}
Function summary: {description}
"""


def _generate_helper_docstring(client, language: str, fn_name: str, source: str) -> dict:
    messages = [
        {"role": "system", "content": _DOCSTRING_SYSTEM.format(language=language)},
        {"role": "user", "content": _DOCSTRING_USER.format(language=language, source=source)},
    ]
    debug_request("Documentation/docstring", messages)
    resp = complete(client, messages, temperature=0.1)
    raw = resp.choices[0].message.content.strip()
    debug_response("Documentation/docstring", raw)
    try:
        from agents.llm import parse_json
        result = parse_json(raw)
    except Exception:
        result = {"description": raw, "parameters": [], "returns": "unknown"}
    result["function_name"] = fn_name
    return result


def _generate_changelog(client, filename: str, issues: list[dict], description: str) -> str:
    issue_text = "; ".join(
        f"{i.get('rule', '?')}: {i.get('action_message', '')}" for i in issues
    )
    messages = [
        {"role": "system", "content": _CHANGELOG_SYSTEM},
        {"role": "user", "content": _CHANGELOG_USER.format(
            filename=filename,
            issues=issue_text,
            description=description,
        )},
    ]
    debug_request("Documentation/changelog", messages)
    resp = complete(client, messages, temperature=0.1)
    content = resp.choices[0].message.content.strip()
    debug_response("Documentation/changelog", content)
    return content


# ── Node ─────────────────────────────────────────────────────

def documentation_node(state: PipelineState) -> dict:
    ctx = state["context"]
    language = ctx.get("language", "unknown")
    file_path = ctx["file_path"]
    repo_path = state["repo_path"]
    abs_path = Path(repo_path) / file_path
    issues = state["current_issues"]
    summary: list[dict] = state.get("summary") or []
    new_functions: list[str] = state.get("new_functions") or []
    remediation_status = state.get("remediation_status", "passed")

    client = get_client()
    primary_description = summary[0]["description"] if summary else ""

    # Patch is already on disk (written by validation agent).
    # Read it as the base for docstring insertion.
    current_source = abs_path.read_text(encoding="utf-8", errors="replace")

    # ── Insert docstrings (only when a real fix was applied) ──
    if summary and remediation_status == "passed":
        documented_source = current_source

        primary_fn = summary[0]["function_name"]
        print(f"[Documentation] Inserting docstring for `{primary_fn}`")
        docstring = _format_docstring(language, summary[0])
        documented_source = _insert_docstring(documented_source, language, primary_fn, docstring)

        if new_functions:
            parser = _PARSERS.get(language)
            fn_node_types = _FUNCTION_NODES.get(language, set())
            for fn_name in new_functions:
                if parser:
                    src_bytes = documented_source.encode("utf-8")
                    tree = parser.parse(src_bytes)
                    node = _find_function_node(tree, src_bytes, fn_name, fn_node_types)
                    fn_src = src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""
                else:
                    fn_src = ""
                if fn_src:
                    print(f"[Documentation] Inserting docstring for helper `{fn_name}`")
                    helper_entry = _generate_helper_docstring(client, language, fn_name, fn_src)
                    helper_docstring = _format_docstring(language, helper_entry)
                    documented_source = _insert_docstring(documented_source, language, fn_name, helper_docstring)

        from agents.validation_agent import _syntax_check
        syntax_ok, syntax_err = _syntax_check(documented_source, language)
        if syntax_ok:
            abs_path.write_text(documented_source, encoding="utf-8")
            print(f"[Documentation] Written with docstrings: {file_path}")
        else:
            print(f"[Documentation] Docstring syntax check failed ({syntax_err}) — patch preserved, docstring skipped")

    # ── Generate commit message ───────────────────────────────
    commit_message = _generate_changelog(
        client,
        filename=Path(file_path).name,
        issues=issues,
        description=primary_description,
    )
    print(f"[Documentation] Commit message: {commit_message}")

    # ── Build approved entry ─────────────────────────────────
    rule_keys = list({i.get("rule") for i in issues if i.get("rule")})
    approved_item = {
        "patch": {
            "rule_key": rule_keys,
            "target_file": file_path,
            "issues": issues,
        },
        "changelog_entry": commit_message,
    }

    return {
        "commit_message": commit_message,
        "approved": [approved_item],
        "last_event": "documentation_done",
    }
