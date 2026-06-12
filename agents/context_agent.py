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


_PARAM_NODE_TYPES = {"parameters", "formal_parameters", "parameter_list"}
_RETURN_ANNOTATION_TYPES = {"type_annotation"}


def _extract_signature(node, source_bytes: bytes, name: str) -> str:
    """Build `name(params) return_type` from tree-sitter nodes; fallback to first line."""
    params_text = ""
    return_text = ""
    for child in node.children:
        if child.type in _PARAM_NODE_TYPES and not params_text:
            params_text = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
        elif child.type in _RETURN_ANNOTATION_TYPES and not return_text:
            return_text = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
    if params_text:
        sig = f"{name}{params_text}"
        if return_text:
            sig += f" {return_text}"
        return sig
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace").split("\n")[0].strip()


def signature_from_source(name: str, source: str, language_key: str) -> str:
    """Parse a function snippet and return its signature string."""
    cfg = _LANGUAGE_CONFIG.get(language_key)
    if cfg is None:
        return source.split("\n")[0].strip()
    source_bytes = source.encode("utf-8")
    from tree_sitter import Parser as _Parser
    tree = _Parser(cfg["language"]).parse(source_bytes)
    fn_node_types = cfg["function_nodes"]

    def walk(node):
        if node.type in fn_node_types and _node_name(node, source_bytes) == name:
            return node
        for child in node.children:
            result = walk(child)
            if result is not None:
                return result
        return None

    node = walk(tree.root_node)
    if node:
        return _extract_signature(node, source_bytes, name)
    return source.split("\n")[0].strip()


_IMPORT_NODE_TYPES: dict[str, set[str]] = {
    "javascript": {"import_statement"},
    "typescript": {"import_statement"},
    "python":     {"import_statement", "import_from_statement"},
    "java":       {"import_declaration"},
    "csharp":     {"using_directive"},
}


def _extract_import_block(
    language: str, tree=None, source_bytes: bytes | None = None
) -> str:
    """Return the import section at the top of the file as a single string."""
    if tree is None or source_bytes is None:
        return ""
    import_types = _IMPORT_NODE_TYPES.get(language, set())
    nodes = [c for c in tree.root_node.children if c.type in import_types]
    return "\n".join(
        source_bytes[n.start_byte:n.end_byte].decode("utf-8", errors="replace")
        for n in nodes
    )


def _extract_name_inventory(tree, source_bytes: bytes, language_key: str) -> dict[str, str]:
    """Return {name: signature_line} for all functions/methods/classes defined in the file."""
    cfg = _LANGUAGE_CONFIG.get(language_key)
    if cfg is None:
        return {}

    function_node_types = cfg["function_nodes"]
    class_node_types = {
        "java":       {"class_declaration", "interface_declaration", "enum_declaration"},
        "javascript": {"class_declaration"},
        "typescript": {"class_declaration", "interface_declaration"},
        "python":     {"class_definition"},
        "csharp":     {"class_declaration", "interface_declaration", "struct_declaration"},
    }.get(language_key, set())

    all_node_types = function_node_types | class_node_types
    seen: dict[str, str] = {}

    def walk(node):
        if node.type in all_node_types:
            name = _node_name(node, source_bytes)
            if name and name != "<anonymous>" and name not in seen:
                seen[name] = _extract_signature(node, source_bytes, name)
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
    seen: dict[tuple, dict] = {}

    def walk(node):
        if node.type in function_node_types:
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            if any(start_line <= ln <= end_line for ln in target_set):
                name = _node_name(node, source_bytes)
                key = (name, start_line)
                if key not in seen:
                    # For arrow functions, include the full const/let declaration
                    source_node = node
                    if node.type == "arrow_function":
                        parent = node.parent
                        if parent and parent.type == "variable_declarator":
                            gp = parent.parent
                            if gp and gp.type in ("lexical_declaration", "variable_declaration"):
                                source_node = gp
                    seen[key] = {
                        "name": name,
                        "start": source_node.start_point[0] + 1,
                        "end": source_node.end_point[0] + 1,
                        "source": source_bytes[source_node.start_byte:source_node.end_byte].decode(
                            "utf-8", errors="replace"
                        ),
                    }
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return list(seen.values())


def _fallback_window(source: str, target_lines: list[int], window: int = 5) -> list[dict]:
    lines = source.splitlines()
    if not target_lines:
        return []
    center = target_lines[0]
    start = max(1, center - window)
    end = min(len(lines), center + window)
    snippet = "\n".join(lines[start - 1 : end])
    return [{"name": "<snippet>", "start": start, "end": end, "source": snippet}]


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

    import_block = _extract_import_block(language or "", tree=tree, source_bytes=source_bytes)
    name_inventory = _extract_name_inventory(tree, source_bytes, language or "") if tree else []

    functions_to_process = _group_issues_by_function(all_issues, functions)

    # Issues outside every function boundary get a fallback line window
    matched_ids = {i.get("id") for g in functions_to_process for i in g["issues"]}
    unmatched = [
        i for i in all_issues
        if i.get("start_line") is not None and i.get("id") not in matched_ids
    ]
    for issue in unmatched:
        fb = _fallback_window(full_code, [int(issue["start_line"])])
        if fb:
            functions_to_process.append({"fn": fb[0], "issues": [issue]})
    if unmatched:
        functions_to_process.sort(key=lambda g: g["fn"]["start"], reverse=True)

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
