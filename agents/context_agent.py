from pathlib import Path

import tree_sitter_java as tsjava
from state import PipelineState
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
import tree_sitter_python as tspy
import tree_sitter_c_sharp as tscs
from tree_sitter import Language, Parser

# ── Language registry ────────────────────────────────────────

_LANGUAGE_CONFIG: dict[str, dict] = {
    "java": {
        "language": Language(tsjava.language()),
        "function_nodes": {"method_declaration", "constructor_declaration"},
    },
    "javascript": {
        "language": Language(tsjs.language()),
        "function_nodes": {"function_declaration", "method_definition", "arrow_function"},
    },
    "typescript": {
        "language": Language(tsts.language_typescript()),
        "function_nodes": {"function_declaration", "method_definition", "arrow_function"},
    },
    "python": {
        "language": Language(tspy.language()),
        "function_nodes": {"function_definition"},
    },
    "csharp": {
        "language": Language(tscs.language()),
        "function_nodes": {"method_declaration", "constructor_declaration"},
    },
}

_EXT_TO_LANG: dict[str, str] = {
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".py": "python",
    ".cs": "csharp",
}

# ── Helpers ──────────────────────────────────────────────────

def _detect_language(file_path: str) -> str | None:
    return _EXT_TO_LANG.get(Path(file_path).suffix.lower())


def _extract_import_block(source: str, language: str) -> str:
    """Return the import section at the top of the file as a single string."""
    lines = source.splitlines()
    import_lines: list[str] = []

    if language == "python":
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                import_lines.append(line)
            elif stripped and not stripped.startswith("#") and import_lines:
                break
    elif language == "java":
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("import "):
                import_lines.append(line)
            elif stripped and not stripped.startswith("//") and not stripped.startswith("/*") and import_lines:
                break
    elif language in ("javascript", "typescript"):
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("import ") or "= require(" in stripped:
                import_lines.append(line)
            elif stripped and not stripped.startswith("//") and import_lines:
                break
    elif language == "csharp":
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("using "):
                import_lines.append(line)
            elif stripped and not stripped.startswith("//") and import_lines:
                break

    return "\n".join(import_lines)


def _extract_name_inventory(tree, source_bytes: bytes, language_key: str) -> list[str]:
    """Return deduplicated list of all function/method/class names defined in the file."""
    cfg = _LANGUAGE_CONFIG.get(language_key)
    if cfg is None:
        return []

    function_node_types = cfg["function_nodes"]
    # Also collect class/interface names
    class_node_types = {
        "java":       {"class_declaration", "interface_declaration", "enum_declaration"},
        "javascript": {"class_declaration"},
        "typescript": {"class_declaration", "interface_declaration"},
        "python":     {"class_definition"},
        "csharp":     {"class_declaration", "interface_declaration", "struct_declaration"},
    }.get(language_key, set())

    all_node_types = function_node_types | class_node_types
    seen: list[str] = []

    def walk(node):
        if node.type in all_node_types:
            for child in node.children:
                if child.type == "identifier":
                    name = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                    if name not in seen:
                        seen.append(name)
                    break
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return seen


def _node_name(node, source_bytes: bytes) -> str:
    for child in node.children:
        if child.type == "identifier":
            return source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
    # Arrow functions: name lives on the parent variable_declarator
    parent = node.parent
    if parent and parent.type == "variable_declarator":
        for child in parent.children:
            if child.type == "identifier":
                return source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
    return "<anonymous>"


_PADDING_LINES = 5


def _extract_functions_ts(
    source: str, language_key: str, target_lines: list[int]
) -> list[dict]:
    """Find function nodes enclosing each target line using tree-sitter."""
    cfg = _LANGUAGE_CONFIG[language_key]
    parser = Parser(cfg["language"])
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)

    target_set = set(target_lines)
    function_node_types = cfg["function_nodes"]
    seen: dict[str, dict] = {}

    def walk(node):
        if node.type in function_node_types:
            start_line = node.start_point[0] + 1   # tree-sitter is 0-indexed
            end_line = node.end_point[0] + 1
            if any(start_line <= ln <= end_line for ln in target_set):
                name = _node_name(node, source_bytes)
                key = (name, start_line)
                if key not in seen:
                    seen[key] = {
                        "name": name,
                        "start": start_line,
                        "end": end_line,
                        "source": source_bytes[node.start_byte:node.end_byte].decode(
                            "utf-8", errors="replace"
                        ),
                    }
        for child in node.children:
            walk(child)

    walk(tree.root_node)

    lines = source.splitlines()
    result = []
    for fn in seen.values():
        padded_start = max(1, fn["start"] - _PADDING_LINES)
        padded_end = min(len(lines), fn["end"] + _PADDING_LINES)
        result.append({
            **fn,
            "padded_source": "\n".join(lines[padded_start - 1 : padded_end]),
            "padded_start": padded_start,
        })
    return result


def _fallback_window(source: str, target_lines: list[int], window: int = 50) -> list[dict]:
    lines = source.splitlines()
    if not target_lines:
        return []
    center = target_lines[0]
    start = max(1, center - window)
    end = min(len(lines), center + window)
    snippet = "\n".join(lines[start - 1 : end])
    return [{"name": "<snippet>", "start": start, "end": end, "source": snippet,
             "padded_source": snippet, "padded_start": start}]


# ── Helpers ──────────────────────────────────────────────────

def _group_issues_by_function(issues: list[dict], functions: list[dict]) -> list[dict]:
    """
    For each function, collect the issues whose start_line falls within it.
    Returns a list of {fn, issues} dicts sorted descending by fn start line
    (bottom of file first, so reverse-order patching avoids line number drift).
    Issues that don't fall in any named function are dropped — they have no
    actionable function context for the LLM.
    """
    groups: dict[int, dict] = {fn["start"]: {"fn": fn, "issues": []} for fn in functions}

    for issue in issues:
        line = issue.get("start_line")
        if line is None:
            continue
        for fn in functions:
            if fn["start"] <= line <= fn["end"]:
                groups[fn["start"]]["issues"].append(issue)
                break

    return sorted(
        [g for g in groups.values() if g["issues"]],
        key=lambda g: g["fn"]["start"],
        reverse=True,
    )


# ── Node ─────────────────────────────────────────────────────

def context_node(state: PipelineState) -> dict:
    # SonarQube component format: "project_key:module:path/to/File.ext"
    # Take everything after the last colon as the repo-relative file path.
    component = state["current_file"]
    parts = component.split(":")
    file_path = parts[-1]

    repo_path = state["repo_path"]
    abs_path = Path(repo_path) / file_path

    print(f"[Context] Reading {file_path}")

    try:
        full_code = abs_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(f"[Context] File not found: {abs_path} — skipping chunk")
        return {
            "functions_to_process": [],
            "current_code": "",
            "context": {
                "file_path": file_path,
                "language": "unknown",
                "full_code": "",
                "functions": [],
                "import_block": "",
                "name_inventory": [],
            },
            "last_event": "context_ready",
        }

    language = _detect_language(file_path)
    all_issues = state["current_issues"]
    target_lines = [
        int(i["start_line"])
        for i in all_issues
        if i.get("start_line") is not None
    ]

    functions: list[dict] = []
    source_bytes = full_code.encode("utf-8")
    tree = None

    if language and language in _LANGUAGE_CONFIG:
        try:
            cfg = _LANGUAGE_CONFIG[language]
            from tree_sitter import Parser as _Parser
            _parser = _Parser(cfg["language"])
            tree = _parser.parse(source_bytes)
            if target_lines:
                functions = _extract_functions_ts(full_code, language, target_lines)
        except Exception as exc:
            print(f"[Context] tree-sitter failed ({exc}), using line window fallback")

    if not functions and target_lines:
        functions = _fallback_window(full_code, target_lines)

    import_block = _extract_import_block(full_code, language or "")
    name_inventory = _extract_name_inventory(tree, source_bytes, language or "") if tree else []

    functions_to_process = _group_issues_by_function(all_issues, functions)
    print(f"[Context] {len(functions_to_process)} function group(s) to process in {file_path}")

    # Seed context with the first function (index 0, which is bottom-most)
    first = functions_to_process[0] if functions_to_process else None
    context = {
        "file_path": file_path,
        "language": language or "unknown",
        "full_code": full_code,
        "functions": [first["fn"]] if first else [],
        "import_block": import_block,
        "name_inventory": name_inventory,
    }

    return {
        "functions_to_process": functions_to_process,
        "current_code": full_code,
        "function_index": 0,
        "current_issues": first["issues"] if first else [],
        "context": context,
        "last_event": "context_ready",
    }
