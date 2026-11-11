"""Code Reacher: Tree-sitter code intelligence (Phase 14).

Heavy native packages (Tree-sitter, sqlite-vec, FastEmbed) are imported inside the functions that need them,
never at CLI start-up. Public entry points::

    from dridh.code_reacher.indexer import CodeIndexer     # incremental indexing
    from dridh.code_reacher.search import CodeSearch       # lookup, symbol, dependencies, endpoints, status
"""

from __future__ import annotations

__all__ = ["CodeIndexer", "CodeSearch"]


def __getattr__(name: str):  # lazy re-exports keep `import dridh.code_reacher` free of native imports
    if name == "CodeIndexer":
        from dridh.code_reacher.indexer import CodeIndexer

        return CodeIndexer
    if name == "CodeSearch":
        from dridh.code_reacher.search import CodeSearch

        return CodeSearch
    raise AttributeError(name)
