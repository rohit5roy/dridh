"""TypeScript / TSX / JavaScript extraction: entities, parameters, imports, calls, heritage, Express and NestJS endpoints."""

from __future__ import annotations

from typing import Any

from dridh.code_reacher._common import HTTP_VERBS, dotted, end_line, join_path, line, squash, string_value, text
from dridh.code_reacher.languages import parser_for
from dridh.code_reacher.model import Endpoint, Entity, Parameter, ParsedFile, Relation

_CLASSES = {"class_declaration": "class", "abstract_class_declaration": "class", "class": "class",
            "interface_declaration": "interface", "enum_declaration": "enum"}
_FUNCTIONS = ("function_declaration", "generator_function_declaration", "function_expression", "arrow_function")
_NEST_VERBS = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH", "Options": "OPTIONS", "Head": "HEAD"}


def module_name(path: str) -> str:
    stem = path.rsplit(".", 1)[0]
    parts = [part for part in stem.split("/") if part]
    if parts and parts[-1] == "index" and len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts) or "__root__"


def _parameters(node: Any) -> list[Parameter]:
    result: list[Parameter] = []
    if node is None:
        return result
    for child in node.named_children:
        name = type_name = default = None
        if child.type in ("required_parameter", "optional_parameter"):
            pattern = child.child_by_field_name("pattern") or (child.named_children[0] if child.named_children else None)
            name = text(pattern) + ("?" if child.type == "optional_parameter" else "")
            annotation = child.child_by_field_name("type")
            type_name = squash(text(annotation).lstrip(": "), 120) if annotation is not None else None
            value = child.child_by_field_name("value")
            default = squash(text(value), 120) if value is not None else None
        elif child.type == "rest_pattern" or child.type == "rest_parameter":
            name = text(child)
        elif child.type == "identifier":
            name = text(child)
        elif child.type == "assignment_pattern":
            name = text(child.child_by_field_name("left"))
            default = squash(text(child.child_by_field_name("right")), 120)
        if name:
            result.append(Parameter(len(result), name, type_name, default))
    return result


def _decorator_call(decorator: Any) -> tuple[str, str | None] | None:
    """(name, first string argument) of ``@Name('arg')`` / ``@Name``, else None."""
    expression = decorator.named_children[0] if decorator.named_children else None
    if expression is None:
        return None
    if expression.type == "identifier":
        return text(expression), None
    if expression.type == "call_expression":
        function = expression.child_by_field_name("function")
        arguments = expression.child_by_field_name("arguments")
        first = next((string_value(a) for a in (arguments.named_children if arguments is not None else []) if a.type == "string"), None)
        return text(function), first
    return None


def extract(source: bytes, path: str, language: str = "typescript") -> ParsedFile:
    tree = parser_for(language).parse(source)
    module = module_name(path)
    parsed = ParsedFile(path=path, language=language, has_syntax_errors=bool(tree.root_node.has_error))
    parsed.entities.append(Entity("module", module.rsplit(".", 1)[-1], module, 1, end_line(tree.root_node)))

    def heritage(node: Any, index: int) -> None:
        for child in node.children:
            if child.type == "class_heritage":
                for clause in child.named_children:
                    kind = "EXTENDS" if clause.type == "extends_clause" else "IMPLEMENTS"
                    for item in clause.named_children:
                        if item.type in ("identifier", "type_identifier", "member_expression", "nested_type_identifier"):
                            parsed.relations.append(Relation(index, kind, text(item), line(item)))
            elif child.type in ("extends_type_clause", "extends_clause"):
                for item in child.named_children:
                    if item.type in ("type_identifier", "identifier", "nested_type_identifier"):
                        parsed.relations.append(Relation(index, "EXTENDS", text(item), line(item)))

    def add_function(node: Any, name: str, entity_type: str, scope: list[str], decorators: list[Any], class_base: str | None) -> int:
        parameters = _parameters(node.child_by_field_name("parameters"))
        returns = node.child_by_field_name("return_type")
        signature = f"{name}({', '.join(p.name + (': ' + p.type_name if p.type_name else '') for p in parameters)})" + (
            f": {squash(text(returns).lstrip(': '), 80)}" if returns is not None else "")
        parsed.entities.append(Entity(entity_type, name, dotted(module, *scope, name), line(node), end_line(node), signature=signature, parameters=parameters))
        index = len(parsed.entities) - 1
        for decorator in decorators:
            info = _decorator_call(decorator)
            if info and info[0] in _NEST_VERBS and class_base is not None:
                parsed.endpoints.append(Endpoint(index, _NEST_VERBS[info[0]], join_path(class_base, info[1] or ""), "nestjs", line(decorator)))
        return index

    def walk(node: Any, scope: list[str], owner: int, class_base: str | None, pending: list[Any]) -> None:
        """Visit ``node``'s children in order; consecutive ``decorator`` siblings apply to the next declaration."""
        decorators: list[Any] = list(pending)
        for child in node.children:
            if child.type == "decorator":
                decorators.append(child)
                continue
            if not child.is_named or child.type == "comment":
                continue  # `export`, `{`, `;` and comments sit between a decorator and its declaration
            handle(child, scope, owner, class_base, decorators)
            decorators = []

    def handle(node: Any, scope: list[str], owner: int, class_base: str | None, decorators: list[Any]) -> bool:
        kind = node.type
        if kind in _CLASSES:
            name = text(node.child_by_field_name("name")) or "(anonymous)"
            parsed.entities.append(Entity(_CLASSES[kind], name, dotted(module, *scope, name), line(node), end_line(node)))
            index = len(parsed.entities) - 1
            heritage(node, index)
            base = None
            for decorator in decorators + [c for c in node.children if c.type == "decorator"]:
                info = _decorator_call(decorator)
                if info and info[0] == "Controller":
                    base = "/" + (info[1] or "").strip("/")
            body = node.child_by_field_name("body")
            if body is not None:
                walk(body, scope + [name], index, base if base is not None else None, [])
            return True
        if kind == "method_definition":
            name = text(node.child_by_field_name("name"))
            entity_type = "constructor" if name == "constructor" else "method"
            index = add_function(node, name, entity_type, scope, decorators, class_base)
            walk_body(node, scope + [name], index)
            return True
        if kind in ("function_declaration", "generator_function_declaration"):
            name = text(node.child_by_field_name("name"))
            index = add_function(node, name, "function", scope, decorators, None)
            walk_body(node, scope + [name], index)
            return True
        if kind == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                name = text(node.child_by_field_name("name"))
                index = add_function(value, name, "function", scope, decorators, None)
                walk_body(value, scope + [name], index)
                return True
        if kind in ("public_field_definition", "field_definition"):
            value = node.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression"):
                name = text(node.child_by_field_name("name"))
                index = add_function(value, name, "method", scope, decorators, class_base)
                walk_body(value, scope + [name], index)
                return True
        if kind == "import_statement":
            source = string_value(node.child_by_field_name("source"))
            if source:
                parsed.relations.append(Relation(owner, "IMPORTS", source, line(node)))
            return True
        if kind == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is not None and function.type in ("identifier", "member_expression"):
                target = squash(text(function), 200)
                parsed.relations.append(Relation(owner, "CALLS", target, line(node)))
                if function.type == "member_expression":
                    verb = text(function.child_by_field_name("property")).lower()
                    first = next((a for a in (arguments.named_children if arguments is not None else [])), None)
                    route = string_value(first) if first is not None else None
                    if verb in HTTP_VERBS and route and route.startswith("/"):
                        parsed.endpoints.append(Endpoint(owner, verb.upper(), route, "express", line(node)))
            walk(node, scope, owner, class_base, [])
            return True
        walk(node, scope, owner, class_base, [])
        return False

    def walk_body(node: Any, scope: list[str], owner: int) -> None:
        body = node.child_by_field_name("body")
        if body is None:
            return
        if body.type == "statement_block":
            walk(body, scope, owner, None, [])
        else:  # expression-bodied arrow function: the body node itself may be a call
            handle(body, scope, owner, None, [])

    walk(tree.root_node, [], 0, None, [])
    return parsed
