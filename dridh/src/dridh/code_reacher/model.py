"""Plain data produced by the language extractors (no database, no tree-sitter types)."""

from __future__ import annotations

from dataclasses import dataclass, field

ENTITY_TYPES = ("module", "class", "interface", "enum", "function", "method", "constructor")
RELATION_TYPES = ("CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS")


@dataclass
class Parameter:
    ordinal: int
    name: str
    type_name: str | None = None
    default: str | None = None


@dataclass
class Entity:
    entity_type: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str | None = None
    doc: str = ""
    parameters: list[Parameter] = field(default_factory=list)


@dataclass
class Relation:
    owner: int  # index into ParsedFile.entities
    relation_type: str
    target_name: str
    line: int


@dataclass
class Endpoint:
    owner: int | None
    method: str
    path: str
    framework: str
    line: int


@dataclass
class ParsedFile:
    path: str
    language: str
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    has_syntax_errors: bool = False
