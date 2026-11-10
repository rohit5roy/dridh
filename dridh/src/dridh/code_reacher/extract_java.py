"""Java extraction: types, methods/constructors with parameters, imports, calls, heritage and Spring Boot endpoints."""

from __future__ import annotations

from typing import Any

from dridh.code_reacher._common import dotted, end_line, join_path, line, squash, string_value, text
from dridh.code_reacher.languages import parser_for
from dridh.code_reacher.model import Endpoint, Entity, Parameter, ParsedFile, Relation

_TYPES = {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
          "record_declaration": "class", "annotation_type_declaration": "interface"}
_MAPPINGS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH"}


def _annotations(node: Any) -> list[Any]:
    modifiers = next((child for child in node.children if child.type == "modifiers"), None)
    return [c for c in (modifiers.children if modifiers is not None else []) if c.type in ("annotation", "marker_annotation")]


def _annotation_name(annotation: Any) -> str:
    return text(annotation.child_by_field_name("name")).rsplit(".", 1)[-1]


def _annotation_values(annotation: Any) -> tuple[list[str], list[str]]:
    """(paths, http methods) from ``@XMapping("/a")``, ``@XMapping(value = {"/a","/b"}, method = RequestMethod.GET)``."""
    paths: list[str] = []
    methods: list[str] = []
    arguments = annotation.child_by_field_name("arguments")
    for argument in (arguments.named_children if arguments is not None else []):
        key, value = None, argument
        if argument.type == "element_value_pair":
            key, value = text(argument.child_by_field_name("key")), argument.child_by_field_name("value")
        items = value.named_children if value is not None and value.type == "element_value_array_initializer" else [value]
        for item in items:
            if item is None:
                continue
            if key in (None, "value", "path"):
                literal = string_value(item)
                if literal is not None:
                    paths.append(literal)
            elif key == "method":
                methods.append(text(item).rsplit(".", 1)[-1].upper())
    return paths, methods


def _parameters(node: Any) -> list[Parameter]:
    result: list[Parameter] = []
    if node is None:
        return result
    for child in node.named_children:
        if child.type in ("formal_parameter", "spread_parameter"):
            name = text(child.child_by_field_name("name")) or next((text(c) for c in child.named_children if c.type == "variable_declarator"), "")
            kind = child.child_by_field_name("type") or next((c for c in child.named_children if c.type not in ("modifiers", "variable_declarator")), None)
            result.append(Parameter(len(result), ("..." if child.type == "spread_parameter" else "") + name, squash(text(kind), 120) if kind is not None else None))
    return result


def extract(source: bytes, path: str) -> ParsedFile:
    tree = parser_for("java").parse(source)
    parsed = ParsedFile(path=path, language="java", has_syntax_errors=bool(tree.root_node.has_error))
    package = ""
    for child in tree.root_node.children:
        if child.type == "package_declaration":
            package = next((text(c) for c in child.named_children if c.type in ("scoped_identifier", "identifier")), "")
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    # The file's module entity is named by its path so it never collides with the type of the same name.
    parsed.entities.append(Entity("module", stem, path, 1, end_line(tree.root_node)))

    def heritage(node: Any, index: int) -> None:
        for child in node.children:
            if child.type == "superclass":
                for item in child.named_children:
                    parsed.relations.append(Relation(index, "EXTENDS", text(item), line(item)))
            elif child.type in ("super_interfaces", "extends_interfaces"):
                kind = "IMPLEMENTS" if child.type == "super_interfaces" else "EXTENDS"
                for group in child.named_children:
                    for item in (group.named_children if group.type == "type_list" else [group]):
                        parsed.relations.append(Relation(index, kind, text(item), line(item)))

    def walk(node: Any, scope: list[str], owner: int, base_paths: list[str]) -> None:
        for child in node.children:
            visit(child, scope, owner, base_paths)

    def visit(node: Any, scope: list[str], owner: int, base_paths: list[str]) -> None:
        kind = node.type
        if kind in _TYPES:
            name = text(node.child_by_field_name("name"))
            parsed.entities.append(Entity(_TYPES[kind], name, dotted(package, *scope, name), line(node), end_line(node)))
            index = len(parsed.entities) - 1
            heritage(node, index)
            bases: list[str] = []
            for annotation in _annotations(node):
                if _annotation_name(annotation) == "RequestMapping":
                    bases = _annotation_values(annotation)[0]
            body = node.child_by_field_name("body")
            if body is not None:
                walk(body, scope + [name], index, bases or [""])
            return
        if kind in ("method_declaration", "constructor_declaration"):
            name = text(node.child_by_field_name("name"))
            parameters = _parameters(node.child_by_field_name("parameters"))
            returns = node.child_by_field_name("type")
            signature = f"{name}({', '.join((p.type_name + ' ' if p.type_name else '') + p.name for p in parameters)})" + (
                f": {squash(text(returns), 80)}" if returns is not None and kind == "method_declaration" else "")
            parsed.entities.append(Entity("constructor" if kind == "constructor_declaration" else "method", name,
                                          dotted(package, *scope, name), line(node), end_line(node), signature=signature, parameters=parameters))
            index = len(parsed.entities) - 1
            for annotation in _annotations(node):
                label = _annotation_name(annotation)
                if label in _MAPPINGS or label == "RequestMapping":
                    paths, methods = _annotation_values(annotation)
                    verbs = [_MAPPINGS[label]] if label in _MAPPINGS else (methods or ["GET"])
                    for base in base_paths or [""]:
                        for sub in paths or [""]:
                            for verb in verbs:
                                parsed.endpoints.append(Endpoint(index, verb, join_path(base, sub), "spring", line(annotation)))
            body = node.child_by_field_name("body")
            if body is not None:
                walk(body, scope + [name], index, [])
            return
        if kind == "import_declaration":
            target = next((text(c) for c in node.named_children if c.type in ("scoped_identifier", "identifier")), "")
            if any(c.type == "asterisk" for c in node.named_children):
                target += ".*"
            if target:
                parsed.relations.append(Relation(owner, "IMPORTS", target, line(node)))
            return
        if kind == "method_invocation":
            target = text(node.child_by_field_name("name"))
            receiver = node.child_by_field_name("object")
            parsed.relations.append(Relation(owner, "CALLS", squash(f"{text(receiver)}.{target}" if receiver is not None else target, 200), line(node)))
        walk(node, scope, owner, base_paths)

    walk(tree.root_node, [], 0, [])
    return parsed
