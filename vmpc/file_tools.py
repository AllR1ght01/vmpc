"""Small, explicit filesystem tools used by the desktop agent and CLI.

The existing :mod:`vmpc.context` module is intentionally a prompt attachment
feature.  These functions are operational tools: they return structured data,
do not follow directory symlinks, cap expensive reads, and write atomically.
They are kept dependency-free so providers and future frontends can reuse them.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


MAX_READ_BYTES = 2_000_000
MAX_RESULTS = 200
SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", ".gradle", "__pycache__", "node_modules", "build", "dist", "out", "target"}
)


class FileToolError(ValueError):
    """A user-correctable filesystem-tool error."""


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line: int
    text: str


def _path(value: Any) -> Path:
    raw = str(value or "").strip().strip("\"'`")
    if not raw:
        raise FileToolError("path is required")
    try:
        return Path(raw).expanduser().resolve()
    except OSError as exc:
        raise FileToolError(f"cannot resolve path: {exc}") from exc


def _check_file(path: Path) -> None:
    if not path.exists():
        raise FileToolError(f"{path} does not exist")
    if not path.is_file():
        raise FileToolError(f"{path} is not a file")


def list_directory(path: Any, *, max_entries: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """List one directory without following symlinks."""
    target = _path(path)
    if not target.exists():
        raise FileToolError(f"{target} does not exist")
    if not target.is_dir():
        raise FileToolError(f"{target} is not a directory")
    if max_entries < 1:
        raise FileToolError("max_entries must be positive")

    output: list[dict[str, Any]] = []
    try:
        entries = sorted(target.iterdir(), key=lambda item: (item.name.casefold(), item.name))
    except OSError as exc:
        raise FileToolError(f"cannot list {target}: {exc}") from exc
    for entry in entries[:max_entries]:
        try:
            is_link = entry.is_symlink()
            kind = "symlink" if is_link else ("directory" if entry.is_dir() else "file")
            stat = entry.lstat()
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            size = None if kind != "file" else stat.st_size
        except OSError:
            kind = "unreadable"
            modified = ""
            size = None
        output.append({
            "name": entry.name,
            "path": str(entry),
            "type": kind,
            "size": size,
            "modified": modified,
        })
    return output


def read_file(path: Any, *, start_line: int = 1, max_lines: int | None = None) -> dict[str, Any]:
    """Read a UTF-8 text file and return its content plus useful metadata."""
    target = _path(path)
    _check_file(target)
    if start_line < 1:
        raise FileToolError("start_line must be at least 1")
    if max_lines is not None and max_lines < 1:
        raise FileToolError("max_lines must be positive")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise FileToolError(f"cannot stat {target}: {exc}") from exc
    if size > MAX_READ_BYTES:
        raise FileToolError(f"{target} is too large ({size} bytes; limit is {MAX_READ_BYTES})")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise FileToolError(f"cannot read {target}: {exc}") from exc
    if b"\x00" in raw[:8192]:
        raise FileToolError(f"{target} looks like a binary file")
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    first = start_line - 1
    selected = lines[first:] if max_lines is None else lines[first : first + max_lines]
    return {
        "path": str(target),
        "content": "".join(selected),
        "start_line": start_line,
        "end_line": first + len(selected),
        "total_lines": len(lines),
        "truncated": first + len(selected) < len(lines),
    }


def write_file(
    path: Any,
    content: str,
    *,
    overwrite: bool = False,
    create_parents: bool = False,
) -> dict[str, Any]:
    """Write text atomically.

    Existing files require ``overwrite=True``.  A temporary sibling is flushed
    and replaced, so an interrupted write cannot leave a half-written target.
    """
    target = _path(path)
    existed = target.exists()
    if target.exists() and not target.is_file():
        raise FileToolError(f"{target} is not a file")
    if target.exists() and not overwrite:
        raise FileToolError(f"{target} already exists; pass overwrite=true to replace it")
    parent = target.parent
    if not parent.exists():
        if not create_parents:
            raise FileToolError(f"parent directory does not exist: {parent}")
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FileToolError(f"cannot create parent directory {parent}: {exc}") from exc
    if not isinstance(content, str):
        raise FileToolError("content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_READ_BYTES:
        raise FileToolError(f"content is too large (limit is {MAX_READ_BYTES} bytes)")
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(parent))
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        temp_name = None
    except OSError as exc:
        raise FileToolError(f"cannot write {target}: {exc}") from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return {"path": str(target), "bytes": len(encoded), "created": not existed}


def _iter_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise FileToolError(f"{root} is not a file or directory")
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        for name in sorted(names):
            path = Path(current) / name
            if not path.is_symlink():
                yield path


def find_files(root: Any, pattern: str = "*", *, max_results: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Find files below *root* using a glob against relative and base names."""
    base = _path(root)
    if not base.exists():
        raise FileToolError(f"{base} does not exist")
    if not pattern:
        raise FileToolError("pattern is required")
    if max_results < 1:
        raise FileToolError("max_results must be positive")
    output: list[dict[str, str]] = []
    for path in _iter_files(base):
        relative = path.name if base.is_file() else path.relative_to(base).as_posix()
        if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern):
            output.append({"path": str(path), "relative": relative})
            if len(output) >= max_results:
                break
    return output


def search_files(
    root: Any,
    query: str,
    *,
    pattern: str = "*",
    case_sensitive: bool = False,
    regex: bool = False,
    max_results: int = MAX_RESULTS,
) -> list[dict[str, Any]]:
    """Search text files recursively and return path, line and matching text."""
    if not query:
        raise FileToolError("query is required")
    if max_results < 1:
        raise FileToolError("max_results must be positive")
    expression = None
    if regex:
        try:
            expression = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise FileToolError(f"invalid regular expression: {exc}") from exc
    needle = query if case_sensitive else query.casefold()
    base = _path(root)
    if not base.exists():
        raise FileToolError(f"{base} does not exist")
    output: list[dict[str, Any]] = []
    for path in _iter_files(base):
        if len(output) >= max_results:
            break
        if not (fnmatch.fnmatch(path.name, pattern) or (base.is_dir() and fnmatch.fnmatch(path.relative_to(base).as_posix(), pattern))):
            continue
        try:
            if path.stat().st_size > MAX_READ_BYTES:
                continue
            raw = path.read_bytes()
            if b"\x00" in raw[:8192]:
                continue
            lines = raw.decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, 1):
            matched = bool(expression.search(line)) if expression else needle in (line if case_sensitive else line.casefold())
            if matched:
                output.append(asdict(SearchMatch(str(path), line_number, line)))
                if len(output) >= max_results:
                    break
    return output


def tool_specs() -> list[dict[str, Any]]:
    """OpenAI-compatible declarations for the local filesystem tools."""
    return [
        {"type": "function", "function": {"name": "list_directory", "description": "List the immediate children of a directory without following symlinks", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "max_entries": {"type": "integer", "minimum": 1}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 text file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "max_lines": {"type": "integer", "minimum": 1}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "write_file", "description": "Write a UTF-8 text file; overwrite must be explicit", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean"}, "create_parents": {"type": "boolean"}}, "required": ["path", "content"]}}},
        {"type": "function", "function": {"name": "find_files", "description": "Find files recursively by glob", "parameters": {"type": "object", "properties": {"root": {"type": "string"}, "pattern": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1}}, "required": ["root"]}}},
        {"type": "function", "function": {"name": "search_files", "description": "Search text files recursively", "parameters": {"type": "object", "properties": {"root": {"type": "string"}, "query": {"type": "string"}, "pattern": {"type": "string"}, "case_sensitive": {"type": "boolean"}, "regex": {"type": "boolean"}, "max_results": {"type": "integer", "minimum": 1}}, "required": ["root", "query"]}}},
    ]


def anthropic_tool_specs() -> list[dict[str, Any]]:
    """Anthropic tool declarations derived from the same local tool contract."""
    output: list[dict[str, Any]] = []
    for item in tool_specs():
        function = item["function"]
        output.append({
            "name": function["name"],
            "description": function["description"],
            "input_schema": function["parameters"],
        })
    return output


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one declared tool and return a JSON-safe result."""
    functions = {
        "list_directory": list_directory,
        "read_file": read_file,
        "write_file": write_file,
        "find_files": find_files,
        "search_files": search_files,
    }
    if name not in functions:
        raise FileToolError(f"unknown file tool: {name}")
    try:
        result = functions[name](**arguments)
    except TypeError as exc:
        raise FileToolError(f"invalid arguments for {name}: {exc}") from exc
    return result if isinstance(result, dict) else {"items": result}


__all__ = [
    "FileToolError",
    "anthropic_tool_specs",
    "execute_tool",
    "find_files",
    "list_directory",
    "read_file",
    "search_files",
    "tool_specs",
    "write_file",
]
