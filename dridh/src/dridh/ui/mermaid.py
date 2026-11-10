"""Draw Mermaid diagrams as box-drawing text for terminal viewers.

Covers the diagram types Dridh's document templates use: ``flowchart``/``graph`` (TD, TB, LR), ``sequenceDiagram`` and
``erDiagram``. Anything else, or anything this parser does not understand, is returned as ``None`` so the caller keeps
the original source block. The drawing is a reading aid; the Mermaid source in the document stays canonical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- canvas

_UP, _DOWN, _LEFT, _RIGHT = 1, 2, 4, 8
_LINE = {
    _UP | _DOWN: "│", _LEFT | _RIGHT: "─", _DOWN | _RIGHT: "┌", _DOWN | _LEFT: "┐", _UP | _RIGHT: "└", _UP | _LEFT: "┘",
    _UP | _DOWN | _RIGHT: "├", _UP | _DOWN | _LEFT: "┤", _LEFT | _RIGHT | _DOWN: "┬", _LEFT | _RIGHT | _UP: "┴",
    _UP | _DOWN | _LEFT | _RIGHT: "┼", _UP: "│", _DOWN: "│", _LEFT: "─", _RIGHT: "─",
}


class _Canvas:
    def __init__(self) -> None:
        self.text: dict[tuple[int, int], str] = {}
        self.lines: dict[tuple[int, int], int] = {}

    def put(self, x: int, y: int, value: str) -> None:
        for i, ch in enumerate(value):
            self.text[(x + i, y)] = ch

    def free(self, x: int, y: int, width: int) -> bool:
        return all((x + i, y) not in self.text and (x + i, y) not in self.lines for i in range(width))

    def link(self, x: int, y: int, bits: int) -> None:
        self.lines[(x, y)] = self.lines.get((x, y), 0) | bits

    def hline(self, x1: int, x2: int, y: int) -> None:
        a, b = sorted((x1, x2))
        for x in range(a, b + 1):
            self.link(x, y, (_LEFT if x > a else 0) | (_RIGHT if x < b else 0))

    def vline(self, x: int, y1: int, y2: int) -> None:
        a, b = sorted((y1, y2))
        for y in range(a, b + 1):
            self.link(x, y, (_UP if y > a else 0) | (_DOWN if y < b else 0))

    def box(self, x: int, y: int, width: int, label: str) -> None:
        self.put(x, y, "┌" + "─" * (width - 2) + "┐")
        self.put(x, y + 1, "│" + label.center(width - 2) + "│")
        self.put(x, y + 2, "└" + "─" * (width - 2) + "┘")

    def render(self) -> str:
        cells = {**{k: _LINE.get(v, " ") for k, v in self.lines.items() if v}, **self.text}
        if not cells:
            return ""
        width = max(x for x, _ in cells) + 1
        height = max(y for _, y in cells) + 1
        rows = ["".join(cells.get((x, y), " ") for x in range(width)).rstrip() for y in range(height)]
        return "\n".join(rows)


# --------------------------------------------------------------------------- flowchart

_SHAPES = [("([", "])"), ("[[", "]]"), ("[(", ")]"), ("((", "))"), ("{{", "}}"), ("[", "]"), ("(", ")"), ("{", "}"), (">", "]")]
_NODE = re.compile(r"^\s*([A-Za-z0-9_.:-]+)\s*(.*)$")
_ARROW = re.compile(r"\s*(?:--\s*([^->|][^>]*?)\s*-->|==\s*([^=>]+?)\s*==>|-\.\s*([^.>]+?)\s*\.->|(-->|---|-\.->|==>|--o|--x|<-->))\s*(?:\|([^|]*)\|)?\s*")


@dataclass
class _Graph:
    direction: str = "TD"
    labels: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    edges: list[tuple[str, str, str]] = field(default_factory=list)


def _clean(label: str) -> str:
    label = label.strip().strip('"').strip("'")
    return re.sub(r"<br\s*/?>", " ", label).strip()


def _take_node(text: str, graph: _Graph) -> tuple[str, str] | None:
    match = _NODE.match(text)
    if not match:
        return None
    node, rest = match.group(1), match.group(2)
    for opening, closing in _SHAPES:
        if rest.startswith(opening):
            end = rest.find(closing, len(opening))
            if end < 0:
                return None
            graph.labels[node] = _clean(rest[len(opening):end]) or node
            rest = rest[end + len(closing):]
            break
    if node not in graph.order:
        graph.order.append(node)
        graph.labels.setdefault(node, node)
    return node, rest


def _parse_flowchart(lines: list[str]) -> _Graph | None:
    head = lines[0].split()
    graph = _Graph(direction=(head[1].upper() if len(head) > 1 else "TD"))
    if graph.direction not in ("TD", "TB", "LR", "BT", "RL"):
        return None
    for raw in lines[1:]:
        line = raw.split("%%", 1)[0].strip().rstrip(";")
        if not line or re.match(r"^(subgraph|end|classDef|class|style|linkStyle|click|direction)\b", line):
            continue
        taken = _take_node(line, graph)
        if taken is None:
            return None
        source, rest = taken
        while rest.strip():
            arrow = _ARROW.match(rest)
            if not arrow:
                return None
            label = _clean(arrow.group(1) or arrow.group(2) or arrow.group(3) or arrow.group(5) or "")
            nxt = _take_node(rest[arrow.end():], graph)
            if nxt is None:
                return None
            target, rest = nxt
            if (source, target, label) not in graph.edges:
                graph.edges.append((source, target, label))
            source = target
    return graph if graph.order else None


def _ranks(graph: _Graph) -> tuple[dict[str, int], list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Longest-path ranks over the graph with back edges (cycles) set aside."""
    forward, back, state = [], [], {}
    outgoing: dict[str, list[tuple[str, str, str]]] = {n: [] for n in graph.order}
    for edge in graph.edges:
        outgoing[edge[0]].append(edge)

    def visit(node: str) -> None:
        state[node] = 1
        for edge in outgoing[node]:
            if state.get(edge[1]) == 1 or edge[0] == edge[1]:
                back.append(edge)
                continue
            forward.append(edge)
            if edge[1] not in state:
                visit(edge[1])
        state[node] = 2

    for node in graph.order:
        if node not in state:
            visit(node)
    rank = {n: 0 for n in graph.order}
    for _ in graph.order:
        for s, t, _label in forward:
            rank[t] = max(rank[t], rank[s] + 1)
    return rank, forward, back


def _layers(graph: _Graph):  # type: ignore[no-untyped-def]
    rank, forward, back = _ranks(graph)
    layers: list[list[str]] = [[] for _ in range(max(rank.values()) + 1)]
    for node in graph.order:
        layers[rank[node]].append(node)
    segments: list[tuple[str, str, str]] = []  # rank-adjacent pieces; dummies are named "~n"
    count = 0
    for s, t, label in forward:
        previous = s
        for r in range(rank[s] + 1, rank[t]):
            count += 1
            dummy = f"~{count}"
            layers[r].append(dummy)
            segments.append((previous, dummy, ""))
            previous = dummy
        segments.append((previous, t, label))
    for _ in range(2):  # barycentre ordering, top to bottom
        position = {n: i for layer in layers for i, n in enumerate(layer)}
        for r in range(1, len(layers)):
            parents = {n: [position[s] for s, t, _ in segments if t == n] for n in layers[r]}
            layers[r].sort(key=lambda n: sum(parents[n]) / len(parents[n]) if parents[n] else position[n])
            position.update({n: i for i, n in enumerate(layers[r])})
    return layers, segments, back


def _lanes(spans: list[tuple[int, int, str, str]]) -> list[int]:
    """Give each connector its own lane unless it shares an end with one already there."""
    lanes: list[list[tuple[int, int, str, str]]] = []
    out = []
    for span in spans:
        for i, lane in enumerate(lanes):
            if all(span[1] < other[0] - 1 or span[0] > other[1] + 1 or span[2] == other[2] or span[3] == other[3] for other in lane):
                lane.append(span)
                out.append(i)
                break
        else:
            lanes.append([span])
            out.append(len(lanes) - 1)
    return out


def _draw_top_down(graph: _Graph) -> str:
    layers, segments, back = _layers(graph)
    width = {n: 1 if n.startswith("~") else len(graph.labels[n]) + 4 for layer in layers for n in layer}
    rows = [sum(width[n] for n in layer) + 3 * (len(layer) - 1) for layer in layers]
    widest = max(rows)
    x: dict[str, int] = {}
    for layer, row in zip(layers, rows, strict=False):
        cursor = (widest - row) // 2
        for n in layer:
            x[n] = cursor
            cursor += width[n] + 3
    centre = {n: x[n] + width[n] // 2 for n in x}
    canvas, y, top = _Canvas(), 0, {}
    gaps: list[list[tuple[str, str, str]]] = [[s for s in segments if s[0] in layers[r]] for r in range(len(layers))]
    for r, layer in enumerate(layers):
        for n in layer:
            top[n] = y
            if n.startswith("~"):
                canvas.vline(centre[n], y, y + 2)
            else:
                canvas.box(x[n], y, width[n], graph.labels[n])
        spans = [(min(centre[s], centre[t]), max(centre[s], centre[t]), s, t) for s, t, _ in gaps[r]]
        lane_of = _lanes(spans) if spans else []
        lane_count = max(lane_of, default=-1) + 1
        y += 3 + lane_count + 2
        for (s, t, label), lane in zip(gaps[r], lane_of, strict=False):
            bus = top[s] + 3 + lane
            if s.startswith("~"):
                canvas.vline(centre[s], top[s] + 2, bus)
            else:
                canvas.put(centre[s], top[s] + 2, "┬")  # leave the box through its bottom edge
                canvas.vline(centre[s], top[s] + 2, bus)
            canvas.hline(centre[s], centre[t], bus)
            canvas.vline(centre[t], bus, y - 1)
            if not t.startswith("~"):
                canvas.put(centre[t], y - 1, "▼")
            if label:
                spot = centre[t] + 2
                if canvas.free(spot, y - 2, len(label)):
                    canvas.put(spot, y - 2, label)
    text = canvas.render()
    if back:
        text += "\n" + "\n".join(f"↺ {graph.labels[s]} → {graph.labels[t]}" + (f" ({label})" if label else "") for s, t, label in back)
    return text


def _draw_left_right(graph: _Graph) -> str:
    layers, segments, back = _layers(graph)
    width = {n: 1 if n.startswith("~") else len(graph.labels[n]) + 4 for layer in layers for n in layer}
    height = {n: 1 if n.startswith("~") else 3 for n in width}
    column = [max(width[n] for n in layer) for layer in layers]
    tall = [sum(height[n] for n in layer) + (len(layer) - 1) for layer in layers]
    tallest = max(tall)
    y: dict[str, int] = {}
    for layer, total in zip(layers, tall, strict=False):
        cursor = (tallest - total) // 2
        for n in layer:
            y[n] = cursor
            cursor += height[n] + 1
    middle = {n: y[n] + height[n] // 2 for n in y}
    canvas, x, left = _Canvas(), 0, {}
    for r, layer in enumerate(layers):
        for n in layer:
            left[n] = x + (column[r] - width[n]) // 2
            if n.startswith("~"):
                canvas.hline(x, x + column[r] - 1, middle[n])
            else:
                canvas.box(left[n], y[n], width[n], graph.labels[n])
        gap = [s for s in segments if s[0] in layer]
        spans = [(min(middle[s], middle[t]), max(middle[s], middle[t]), s, t) for s, t, _ in gap]
        lane_of = _lanes(spans) if spans else []
        labels = max((len(label) for _, _, label in gap), default=0)
        start = x + column[r]
        x = start + 1 + (max(lane_of, default=-1) + 1) + labels + 3
        for (s, t, label), lane in zip(gap, lane_of, strict=False):
            exit_x = left[s] + width[s] if not s.startswith("~") else start
            bus = start + 1 + lane
            canvas.hline(exit_x, bus, middle[s])
            canvas.vline(bus, middle[s], middle[t])
            canvas.hline(bus, x - 1, middle[t])
            if not t.startswith("~"):
                canvas.put(x - 1, middle[t], "►")
            if label:
                spot = bus + 2
                canvas.put(spot, middle[t] - 1 if canvas.free(spot, middle[t] - 1, len(label)) else middle[t], label)
    text = canvas.render()
    if back:
        text += "\n" + "\n".join(f"↺ {graph.labels[s]} → {graph.labels[t]}" + (f" ({label})" if label else "") for s, t, label in back)
    return text


# --------------------------------------------------------------------------- sequence

_MESSAGE = re.compile(r"^\s*([^\s\->:+]+)\s*(-->>|->>|-->|->|--x|-x|--\))?(-\)|)\s*([+-]?)\s*([^\s:+-][^:]*?)\s*:\s*(.*)$")


def _draw_sequence(lines: list[str]) -> str | None:
    names: list[str] = []
    alias: dict[str, str] = {}
    steps: list[tuple[str, ...]] = []

    def known(name: str) -> str:
        if name not in names:
            names.append(name)
            alias.setdefault(name, name)
        return name

    for raw in lines[1:]:
        line = raw.split("%%", 1)[0].strip()
        if not line or line.startswith(("activate", "deactivate", "autonumber")):
            continue
        declared = re.match(r"^(participant|actor)\s+(\S+)(?:\s+as\s+(.+))?$", line)
        if declared:
            alias[known(declared.group(2))] = _clean(declared.group(3) or declared.group(2))
            continue
        block = re.match(r"^(loop|alt|else|opt|par|and|critical|break|rect)\b\s*(.*)$", line)
        if block:
            steps.append(("block", f"{block.group(1)} {block.group(2)}".strip()))
            continue
        if line == "end":
            steps.append(("block", "end"))
            continue
        note = re.match(r"^[Nn]ote\s+(?:left of|right of|over)\s+([^:]+):\s*(.*)$", line)
        if note:
            steps.append(("note", note.group(2).strip()))
            continue
        message = re.match(r"^\s*([^\s:]+?)\s*(-->>|->>|--x|-x|--\)|-\)|-->|->)\s*[+-]?\s*([^\s:]+)\s*:\s*(.*)$", line)
        if not message:
            return None
        steps.append(("msg", known(message.group(1)), known(message.group(3)), message.group(2), message.group(4).strip()))
    if not names:
        return None
    boxes = {n: len(alias[n]) + 4 for n in names}
    index = {n: i for i, n in enumerate(names)}
    gap = [max((boxes[names[i]] + boxes[names[i + 1]]) // 2 + 3, 0) for i in range(len(names) - 1)]
    for step in steps:
        if step[0] == "msg" and step[1] != step[2]:
            a, b = sorted((index[step[1]], index[step[2]]))
            need = len(step[4]) + 4
            if sum(gap[a:b]) < need:
                gap[b - 1] += need - sum(gap[a:b])
    centre = [boxes[names[0]] // 2]
    for g in gap:
        centre.append(centre[-1] + g)
    canvas = _Canvas()
    for n in names:
        canvas.box(centre[index[n]] - boxes[n] // 2, 0, boxes[n], alias[n])
    row = 3
    for step in steps:
        if step[0] == "msg":
            _, s, t, arrow, label = step
            a, b = centre[index[s]], centre[index[t]]
            dashed = arrow.startswith("--")
            if s == t:
                canvas.put(a + 1, row, "─┐ " + label)
                canvas.put(a + 1, row + 1, "◄┘")
                row += 3
                continue
            lo, hi = sorted((a, b))
            canvas.put(lo + (hi - lo - len(label)) // 2 + 1, row, label)
            body = ("┄" if dashed else "─") * (hi - lo - 1)
            canvas.put(lo + 1, row + 1, (body[:-1] + "►") if b > a else ("◄" + body[1:]))
            row += 2
        else:
            text = f"┄┄ {step[1]} ┄┄" if step[0] == "block" else f"[note] {step[1]}"
            canvas.put(1, row, text)
            row += 1
    for c in centre:
        for r in range(3, row):
            if (c, r) not in canvas.text:
                canvas.put(c, r, "│")
    return canvas.render()


# --------------------------------------------------------------------------- entity relationships

_CARD = {"||": "one", "|o": "zero or one", "o|": "zero or one", "}|": "one or more", "|{": "one or more",
         "}o": "zero or more", "o{": "zero or more"}


def _draw_er(lines: list[str]) -> str | None:
    rows = []
    for raw in lines[1:]:
        line = raw.split("%%", 1)[0].strip()
        if not line or line.endswith("{") or line == "}" or not re.search(r"--|\.\.", line):
            continue
        match = re.match(r'^(\S+)\s+([|}o]{2})(--|\.\.)([|{o]{2})\s+(\S+)\s*:\s*"?(.*?)"?$', line)
        if not match:
            return None
        a, left, _, right, b, label = match.groups()
        rows.append((a, _CARD.get(left, left), label, _CARD.get(right, right), b))
    if not rows:
        return None
    width = max(len(r[0]) for r in rows)
    return "\n".join(f"{a.ljust(width)}  ({left}) ──{label}──► ({right})  {b}" for a, left, label, right, b in rows)


# --------------------------------------------------------------------------- entry points

def render(source: str) -> str | None:
    """Text drawing of one Mermaid diagram, or ``None`` when its type or syntax is not supported."""
    lines = [line for line in source.strip().splitlines() if line.strip() and not line.strip().startswith("%%")]
    if not lines:
        return None
    kind = lines[0].split()[0]
    try:
        if kind in ("flowchart", "graph"):
            graph = _parse_flowchart(lines)
            if graph is None:
                return None
            return _draw_left_right(graph) if graph.direction in ("LR", "RL") else _draw_top_down(graph)
        if kind == "sequenceDiagram":
            return _draw_sequence(lines)
        if kind == "erDiagram":
            return _draw_er(lines)
    except (KeyError, ValueError, IndexError):
        return None
    return None


_FENCE = re.compile(r"^```mermaid[ \t]*\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)


def render_markdown(markdown: str) -> tuple[str, int, int]:
    """Replace each Mermaid block with its text drawing. Returns (markdown, drawn, kept_as_source)."""
    drawn = kept = 0

    def swap(match: re.Match[str]) -> str:
        nonlocal drawn, kept
        picture = render(match.group(1))
        if picture is None:
            kept += 1
            return match.group(0)
        drawn += 1
        return "```text\n" + picture + "\n```"

    return _FENCE.sub(swap, markdown), drawn, kept
