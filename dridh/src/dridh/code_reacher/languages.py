"""Language registry. Tree-sitter and the grammars are imported on first use, never at CLI start-up (D-P0-09)."""

from __future__ import annotations

from functools import cache
from pathlib import PurePosixPath
from typing import Any

EXTENSIONS = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
}
SUPPORTED_LANGUAGES = ("python", "typescript", "tsx", "javascript", "java")


def language_for(path: str) -> str | None:
    return EXTENSIONS.get(PurePosixPath(path).suffix.lower())


@cache
def parser_for(language: str) -> Any:
    """A Tree-sitter parser for ``language`` (imports the native packages lazily)."""
    from tree_sitter import Language, Parser

    if language == "python":
        import tree_sitter_python as grammar

        pointer = grammar.language()
    elif language == "java":
        import tree_sitter_java as grammar

        pointer = grammar.language()
    elif language == "typescript":
        import tree_sitter_typescript as grammar

        pointer = grammar.language_typescript()
    elif language == "tsx":
        import tree_sitter_typescript as grammar

        pointer = grammar.language_tsx()
    elif language == "javascript":
        import tree_sitter_javascript as grammar

        pointer = grammar.language()
    else:
        raise ValueError(f"unsupported language: {language}")
    return Parser(Language(pointer))
