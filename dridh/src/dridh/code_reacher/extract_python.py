"""Python extraction (Tree-sitter): entities, parameters, imports, calls, inheritance, FastAPI/Flask endpoints."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from dridh.code_reacher._common import HTTP_VERBS, dotted, end_line, line, squash, string_value, text
from dridh.code_reacher.languages import parser_for
from dridh.code_reacher.model import Endpoint, Entity, Parameter, ParsedFile, Relation


def module_name(path: str, packages: frozenset[str] | set[str] = frozenset()) -> str:
    """The importable module name: the path below the topmost directory that is a package (has ``__init__.py``).

    ``py/app/repo.py`` is ``app.repo`` when ``py/app`` holds an ``__init__.py`` and ``py`` does not, which is how
    Python code actually imports it (``from app.repo import ...``).
    """
    pure = PurePosixPath(path)
    parts = [] if pure.stem == "__init__" else [pure.stem]
    directory = pure.parent.as_posix() if pure.parent.as_posix() != "." else ""
    while directory and directory in packages:
        parts.insert(0, PurePosixPath(directory).name)
        directory = PurePosixPath(directory).parent.as_posix() if PurePosixPath(directory).parent.as_posix() != "." else ""
    return ".".join(parts) or pure.stem


def _docstring(body: Any) -> str:
    if body is None:
        return ""
    for child in body.named_children:
        if child.type == "expression_statement" and child.named_children and child.named_children[0].type == "string":
            value = string_value(child.named_children[0]) or ""
            return value.strip().splitlines()[0].strip() if value.strip() else ""
        break
    return ""


def _parameters(node: Any, *, is_method: bool) -> list[Parameter]:
    result: list[Parameter] = []
    if node is None:
        return result
    for child in node.named_children:
        name = type_name = default = None
        if child.type == "identifier":
            name = text(child)
        elif child.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
            ident = child.child_by_field_name("name") or next((c for c in child.named_children if c.type in ("identifier", "list_splat_pattern", "dictionary_splat_pattern")), None)
            name = text(ident)
            annotation = child.child_by_field_name("type")
            type_name = squash(text(annotation), 120) if annotation is not None else None
            value = child.child_by_field_name("value")
            default = squash(text(value), 120) if value is not None else None
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            name = text(child)
        if name:
            result.append(Parameter(len(result), name, type_name, default))
    if is_method and result and result[0].name in ("self", "cls"):
        result = [Parameter(i, p.name, p.type_name, p.default) for i, p in enumerate(result[1:])]
    return result


def _first_string_argument(call: Any) -> str | None:
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return None
    for argument in arguments.named_children:
        if argument.type == "keyword_argument":
            continue
        return string_value(argument)
    return None


def _route_methods(call: Any) -> list[str]:
    arguments = call.child_by_field_name("arguments")
    methods: list[str] = []
    for argument in (arguments.named_children if arguments is not None else []):
        if argument.type == "keyword_argument" and text(argument.child_by_field_name("name")) == "methods":
            value = argument.child_by_field_name("value")
            methods += [(string_value(item) or "").upper() for item in (value.named_children if value is not None else []) if item.type == "string"]
    return [m for m in methods if m] or ["GET"]


def extract(source: bytes, path: str, packages: frozenset[str] | set[str] = frozenset()) -> ParsedFile:
    tree = parser_for("python").parse(source)
    module = module_name(path, packages)
    parsed = ParsedFile(path=path, language="python", has_syntax_errors=bool(tree.root_node.has_error))
    parsed.entities.append(Entity("module", module.rsplit(".", 1)[-1], module, 1, end_line(tree.root_node), signature=None))

    def endpoints_from(decorators: list[Any], owner: int) -> None:
        for decorator in decorators:
            expression = decorator.named_children[0] if decorator.named_children else None
            if expression is None or expression.type != "call":
                continue
            function = expression.child_by_field_name("function")
            if function is None or function.type != "attribute":
                continue
            verb = text(function.child_by_field_name("attribute")).lower()
            route = _first_string_argument(expression)
            if route is None:
                continue
            if verb in HTTP_VERBS:
                parsed.endpoints.append(Endpoint(owner, verb.upper(), route, "fastapi", line(decorator)))
            elif verb == "route":
                for method in _route_methods(expression):
                    parsed.endpoints.append(Endpoint(owner, method, route, "flask", line(decorator)))

    def define(node: Any, decorators: list[Any], scope: list[str], owner: int, in_class: bool) -> None:
        name = text(node.child_by_field_name("name"))
        qualified = dotted(module, *scope, name)
        if node.type == "class_definition":
            entity = Entity("class", name, qualified, line(node), end_line(node), doc=_docstring(node.child_by_field_name("body")))
            parsed.entities.append(entity)
            index = len(parsed.entities) - 1
            supers = node.child_by_field_name("superclasses")
            for base in (supers.named_children if supers is not None else []):
                if base.type in ("identifier", "attribute"):
                    parsed.relations.append(Relation(index, "EXTENDS", text(base), line(base)))
            walk_children(node.child_by_field_name("body"), scope + [name], index, True)
            return
        parameters = _parameters(node.child_by_field_name("parameters"), is_method=in_class)
        returns = node.child_by_field_name("return_type")
        signature = f"{name}({', '.join(p.name + (': ' + p.type_name if p.type_name else '') for p in parameters)})" + (f" -> {squash(text(returns), 80)}" if returns is not None else "")
        entity = Entity("method" if in_class else "function", name, qualified, line(node), end_line(node), signature=signature,
                        doc=_docstring(node.child_by_field_name("body")), parameters=parameters)
        parsed.entities.append(entity)
        index = len(parsed.entities) - 1
        endpoints_from(decorators, index)
        walk_children(node.child_by_field_name("body"), scope + [name], index, False)

    def visit(node: Any, scope: list[str], owner: int, in_class: bool) -> None:
        kind = node.type
        if kind == "decorated_definition":
            definition = node.child_by_field_name("definition")
            decorators = [child for child in node.children if child.type == "decorator"]
            if definition is not None and definition.type in ("function_definition", "class_definition"):
                define(definition, decorators, scope, owner, in_class)
            return
        if kind in ("function_definition", "class_definition"):
            define(node, [], scope, owner, in_class)
            return
        if kind == "import_statement":
            for item in node.named_children:
                target = item.child_by_field_name("name") if item.type == "aliased_import" else item
                if target is not None and target.type == "dotted_name":
                    parsed.relations.append(Relation(owner, "IMPORTS", text(target), line(node)))
            return
        if kind == "import_from_statement":
            origin = text(node.child_by_field_name("module_name"))
            names = [child for child in node.children_by_field_name("name")]
            if any(child.type == "wildcard_import" for child in node.named_children) or not names:
                parsed.relations.append(Relation(owner, "IMPORTS", origin, line(node)))
            for item in names:
                target = item.child_by_field_name("name") if item.type == "aliased_import" else item
                parsed.relations.append(Relation(owner, "IMPORTS", f"{origin}.{text(target)}", line(node)))
            return
        if kind == "call":
            function = node.child_by_field_name("function")
            if function is not None and function.type in ("identifier", "attribute"):
                parsed.relations.append(Relation(owner, "CALLS", squash(text(function), 200), line(node)))
        walk_children(node, scope, owner, in_class)

    def walk_children(node: Any, scope: list[str], owner: int, in_class: bool) -> None:
        if node is None:
            return
        for child in node.children:
            visit(child, scope, owner, in_class)

    walk_children(tree.root_node, [], 0, False)
    return parsed
