// Benchmarks and "who decides", drawn as charts that animate when they scroll into view. Plain ES module, no build
// step, no library. The numbers are the published measurements (docs/benchmarks/*.md); each chart links to its source.
// Without JavaScript the section keeps its links; with reduced motion every chart is shown in its final state at once.

const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const DOCS = "https://github.com/rohit5roy/dridh/blob/main/docs/benchmarks/";

/* ------------------------------------------------------------------ data (from docs/benchmarks) */

const MODELS = [ // CodeSearchNet, mean MRR@10 of Python and Java, richer text (r4)
  { name: "gte-modernbert-base", note: "149M · 600 MB", value: 0.912 },
  { name: "gte-base", note: "109M · 437 MB", value: 0.906 },
  { name: "gte-small", note: "33M · 134 MB · shipped", value: 0.895, mark: true },
  { name: "bge-small-en-v1.5", note: "33M · before 0.9.0rc4", value: 0.873 },
  { name: "mxbai-embed-xsmall-v1", note: "24M", value: 0.730 },
  { name: "potion-base-8M", note: "8M · static", value: 0.631 },
];
const TEXT_VS_MODEL = [ // what each change added to MRR@10
  { name: "Richer text · Python", note: "name → + purpose + code", value: 0.102, mark: true },
  { name: "Richer text · Java", note: "name → + purpose + code", value: 0.058, mark: true },
  { name: "Bigger model", note: "gte-small → gte-modernbert", value: 0.017 },
];
const SUMMARIES = [ // top-1, code with every doc comment removed: before → after `dridh code summarize`
  { name: "Python · hybrid", note: "the default search", before: 0.677, after: 0.843 },
  { name: "Python · full-text", before: 0.621, after: 0.798 },
  { name: "Python · vector", before: 0.843, after: 0.864 },
  { name: "Java · hybrid", note: "the default search", before: 0.729, after: 0.824 },
  { name: "Java · full-text", before: 0.553, after: 0.749 },
  { name: "Java · vector", before: 0.799, after: 0.834 },
];
const INDEX = [ // apache/dubbo, 35,975 entities, 4 CPUs
  { name: "First full index", note: "parse + embed · 1.5 GB peak", seconds: 1308, label: "21.8 min" },
  { name: "Structure only", note: "--no-embed", seconds: 14.9, label: "15 s" },
  { name: "After editing a file", note: "1 file, 52 vectors", seconds: 13.1, label: "13 s" },
];
const RUN_STATS = [ // one feature, every stage, opencode/big-pickle
  { value: 26, label: "agent runs, none failed" },
  { value: 0, label: "repairs needed", prefix: "" },
  { value: 4, of: 4, label: "stories done on the first try" },
  { value: 29, of: 29, label: "flows proven by a named test" },
  { value: 2.4, decimals: 1, suffix: "M", label: "tokens" },
  { value: 0, prefix: "$", label: "model cost" },
];
const RUN_STAGES = [ // agent minutes per stage
  { name: "use cases", minutes: 2 }, { name: "design", minutes: 8 }, { name: "plan", minutes: 16 }, { name: "build", minutes: 7 },
];
const DECIDES = [ // who may do what: [action, agent, you]; true yes, false never, null not needed, "scripts" no one: checked by scripts
  ["Ask questions, write the use cases, design and plan", true, null],
  ["Write the code and its tests in a worktree", true, null],
  ["Check documents, tests and flow coverage", "scripts", "scripts"],
  ["Tick a review checklist", false, true],
  ["Approve a stage, typing the confirmation", false, true],
  ["Start a run, judge a checkpoint lot", false, true],
  ["Accept a deviation from the use cases", false, true],
  ["Complete the feature", false, true],
];

const WINS = [ // the four claims, one number each
  { n: "01", title: "Minimising token waste", value: 0, label: "wasted retries: 0 repairs in 4 stories" },
  { n: "02", title: "Enforced accuracy", value: 29, of: 29, label: "requirements proven by a named, passing test" },
  { n: "03", title: "Cheap models deliver", value: 0, prefix: "$", label: "model cost of a working feature" },
  { n: "04", title: "You are the pilot", value: 0, label: "actions an agent can approve" },
  { n: "05", title: "Open source, on your machine", value: 0, label: "Dridh servers: search, state and checks run locally" },
];
const LOCAL = [ // what runs on your machine
  { name: "Dridh runtime", note: "stages, gates, checks" },
  { name: "Code search", note: "index + gte-small model" },
  { name: "State and documents", note: "SQLite in your project" },
  { name: "Worktrees and tests", note: "one per story" },
];
const OUTSIDE = [ // what leaves it
  { name: "Your model provider", note: "prompts, through OpenCode, as you set it" },
  { name: "GitHub", note: "off by default", off: true },
];
const LICENCES = [
  ["Dridh", "AGPL-3.0"], ["OpenCode", "MIT"], ["gte-small", "MIT"], ["FastEmbed", "Apache-2.0"], ["ONNX Runtime", "MIT"],
  ["SQLite", "public domain"], ["sqlite-vec", "MIT / Apache-2.0"], ["tree-sitter", "MIT"],
];
const TOKENS = [ // the free-model run's 2.38 M tokens, from its usage log
  { name: "cache read", share: 56.2, tokens: "1.34M", tone: "cache" },
  { name: "input", share: 28.8, tokens: "686k", tone: "input" },
  { name: "reasoning", share: 9.9, tokens: "237k", tone: "reasoning" },
  { name: "output", share: 5.0, tokens: "120k", tone: "output" },
];
const SCRIPTED = ["documents validated", "contracts matched", "file ownership checked", "flows proven", "delivery checked"];
const GATES = ["document valid", "contract matches", "tests pass", "flow proven", "you approve"];
const SMALL_VS_BIG = [ // [measure, gte-small share, gte-modernbert-base share, labels, note]
  ["search quality", 0.895, 0.912, "gte-small 0.895", "4.5× larger 0.912", "within 0.017"],
  ["speed", 1, 0.336, "gte-small 1.13×", "0.38×", "3× faster"],
  ["download", 0.223, 1, "134 MB", "600 MB", "a quarter of the size"],
];
const ACCURACY_STATS = [
  { value: 87.5, decimals: 1, suffix: "%", label: "right function first in code search (Python)" },
  { value: 17, label: "failure recoveries, each with a test" },
  { value: 1228, label: "tests run on every change" },
];
const VALUE_STATS = [
  { value: 4, of: 4, label: "stories on the first try" },
  { value: 82, label: "passing tests delivered" },
  { value: 26, of: 26, label: "agent runs succeeded" },
  { value: 35, suffix: " min", label: "agent time, all stages" },
];

/* ------------------------------------------------------------------ helpers */

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const fmt = (value, decimals = 3) => value.toFixed(decimals);

function counter(node, to, { decimals = 0, prefix = "", suffix = "", duration = 1100, from = 0 } = {}) {
  const show = (v) => {
    const text = to >= 1000 ? Math.round(v).toLocaleString("en-US") : v.toFixed(decimals);
    node.textContent = `${prefix}${text}${suffix}`;
  };
  if (reduce) return show(to);
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    show(from + (to - from) * (1 - Math.pow(1 - t, 3)));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/** A row: label (name + note) | track with one or two fills | value. `scale` maps a value to 0..1 of the track. */
function barRow(item, scale, { before, after, value, label, mark }) {
  const row = el("div", "bar-row" + (mark ? " is-mark" : ""));
  const name = el("div", "bar-name");
  name.append(el("span", "", item.name));
  if (item.note) name.append(el("small", "", item.note));
  const track = el("div", "bar-track");
  if (before !== undefined) {
    const base = el("div", "bar-fill is-before");
    base.style.setProperty("--to", scale(before));
    track.append(base);
  }
  const fill = el("div", "bar-fill" + (before !== undefined ? " is-after" : ""));
  fill.style.setProperty("--to", scale(after ?? value));
  track.append(fill);
  const out = el("div", "bar-value", label ?? "");
  row.append(name, track, out);
  return { row, out };
}

/* ------------------------------------------------------------------ charts */

function modelsChart(host) {
  const rows = MODELS.map((m) => barRow(m, (v) => v, { value: m.value, mark: m.mark }));
  rows.forEach(({ row }) => host.append(row));
  return () => rows.forEach(({ out }, i) => counter(out, MODELS[i].value, { decimals: 3 }));
}

const EMBEDDED = [ // what is embedded per function, and gte-small's Python MRR@10 with it
  { parts: ["name", "signature", "path"], value: 0.812, label: "before" },
  { parts: ["+ purpose"], value: 0.840, label: "+ doc comment" },
  { parts: ["+ start of the code"], value: 0.914, label: "shipped" },
];

function textVsModelChart(host) {
  const max = 0.11;
  const rows = TEXT_VS_MODEL.map((m) => barRow(m, (v) => v / max, { value: m.value, mark: m.mark }));
  rows.forEach(({ row }) => host.append(row));
  // what each function is embedded as, built up block by block
  const steps = el("div", "embed-steps");
  steps.append(el("div", "embed-title", "what one function is embedded as (gte-small, Python)"));
  const values = EMBEDDED.map((step, i) => {
    const line = el("div", "embed-line");
    line.style.setProperty("--i", i);
    const chips = el("div", "embed-chips");
    EMBEDDED.slice(0, i + 1).forEach((s, k) => s.parts.forEach((part) => chips.append(el("span", k === i ? "chip is-new" : "chip", part))));
    const value = el("b", "", "0.000");
    line.append(chips, value);
    steps.append(line);
    return value;
  });
  host.append(steps);
  return () => {
    rows.forEach(({ out }, i) => counter(out, TEXT_VS_MODEL[i].value, { decimals: 3, prefix: "+" }));
    values.forEach((v, i) => setTimeout(() => counter(v, EMBEDDED[i].value, { decimals: 3, from: i ? EMBEDDED[i - 1].value : 0.5 }),
      reduce ? 0 : 400 + i * 450));
  };
}

function summariesChart(host) {
  const rows = SUMMARIES.map((m) => barRow(m, (v) => v, { before: m.before, after: m.after, mark: /hybrid/.test(m.name) }));
  rows.forEach(({ row }) => host.append(row));
  return () => rows.forEach(({ out }, i) => {
    const { before, after } = SUMMARIES[i];
    out.textContent = `${fmt(before)} → `;
    const arrow = el("b", "");
    out.append(arrow);
    arrow.textContent = fmt(before);
    setTimeout(() => counter(arrow, after, { decimals: 3, from: before }), reduce ? 0 : 700);
  });
}

function indexChart(host) {
  const max = INDEX[0].seconds;
  // linear on purpose: the first index is long, every later one takes seconds; the short bars get a visible minimum
  const rows = INDEX.map((m, i) => barRow(m, (v) => Math.max(v / max, 0.012), { value: m.seconds, label: m.label, mark: i > 0 }));
  rows.forEach(({ row }) => host.append(row));
  return () => {};
}

function runPanel(host) {
  const stats = el("div", "run-stats");
  const numbers = RUN_STATS.map((s) => {
    const cell = el("div", "run-stat");
    const big = el("strong", "", "0");
    cell.append(big, el("span", "", s.label));
    stats.append(cell);
    return big;
  });
  const total = RUN_STAGES.reduce((sum, s) => sum + s.minutes, 0);
  const strip = el("div", "run-strip");
  RUN_STAGES.forEach((s, i) => {
    const seg = el("div", "run-seg");
    seg.style.setProperty("--w", s.minutes / total);
    seg.style.setProperty("--i", i);
    seg.append(el("span", "", `${s.name} ${s.minutes} min`));
    strip.append(seg);
  });
  host.append(stats, el("div", "run-strip-label", "agent time by stage · 33 of 35 min"), strip);
  return () => RUN_STATS.forEach((s, i) => {
    counter(numbers[i], s.value, { decimals: s.decimals ?? 0, prefix: s.prefix ?? "", suffix: s.of ? ` / ${s.of}` : s.suffix ?? "" });
  });
}

function decidesMatrix(host) {
  const head = el("div", "decide-row is-head");
  head.append(el("span", "", ""), el("span", "", "agent"), el("span", "", "you"));
  host.append(head);
  DECIDES.forEach(([action, agent, you], i) => {
    const row = el("div", "decide-row");
    row.style.setProperty("--i", i);
    row.append(el("span", "decide-action", action));
    for (const who of [agent, you]) {
      const kind = who === "scripts" ? "script" : who === null ? "none" : who ? "yes" : "no";
      const mark = el("span", `decide-mark is-${kind}`, { script: "script", none: "·", yes: "✓", no: "✕" }[kind]);
      mark.setAttribute("aria-label", { script: "done by scripts", none: "not needed", yes: "yes", no: "never" }[kind]);
      row.append(mark);
    }
    host.append(row);
  });
  return () => {};
}

function winsBand(host) {
  const nums = WINS.map((w) => {
    const cell = el("a", "win-num");
    cell.href = `#win-${w.n}`;
    const big = el("strong", "", "0");
    cell.append(el("small", "", `${w.n} · ${w.title}`), big, el("span", "", w.label));
    host.append(cell);
    return big;
  });
  return () => WINS.forEach((w, i) => counter(nums[i], w.value, { prefix: w.prefix ?? "", suffix: w.of ? `/${w.of}` : "", duration: 1400 }));
}

function tokensChart(host) {
  const NS = "http://www.w3.org/2000/svg";
  const wrap = el("div", "donut-wrap");
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 120 120");
  svg.setAttribute("class", "donut");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", TOKENS.map((t) => `${t.name} ${t.share}%`).join(", "));
  const R = 48, C = 2 * Math.PI * R;
  let offset = 0;
  TOKENS.forEach((t, i) => {
    const arc = document.createElementNS(NS, "circle");
    arc.setAttribute("cx", "60"); arc.setAttribute("cy", "60"); arc.setAttribute("r", String(R));
    arc.setAttribute("class", `arc is-${t.tone}`);
    const len = (t.share / 100) * C;
    arc.style.setProperty("--len", len.toFixed(2));
    arc.style.setProperty("--gap", (C - len).toFixed(2));
    arc.style.setProperty("--off", (-offset).toFixed(2));
    arc.style.setProperty("--i", i);
    offset += len;
    svg.append(arc);
  });
  const centre = el("div", "donut-centre");
  const pct = el("strong", "", "0%");
  centre.append(pct, el("span", "", "from cache"));
  const legend = el("ul", "donut-legend");
  TOKENS.forEach((t) => {
    const li = el("li", `is-${t.tone}`);
    li.append(el("b", "", `${t.share}%`), el("span", "", ` ${t.name} · ${t.tokens}`));
    legend.append(li);
  });
  wrap.append(svg, centre);
  const scripted = el("div", "scripted");
  scripted.append(el("div", "scripted-title", "checked by scripts, not models: 0 tokens"));
  const chips = el("div", "embed-chips");
  SCRIPTED.forEach((name, i) => { const c = el("span", "chip is-script", name); c.style.setProperty("--i", i); chips.append(c); });
  scripted.append(chips);
  const row = el("div", "donut-row");
  row.append(wrap, legend);
  host.append(row, scripted);
  return () => counter(pct, 56.2, { decimals: 1, suffix: "%", duration: 1500 });
}

function proofChart(host) {
  const dots = el("div", "proof-dots");
  dots.setAttribute("role", "img");
  dots.setAttribute("aria-label", "29 of 29 requirements proven");
  for (let i = 0; i < 29; i += 1) { const d = el("span", "dot"); d.style.setProperty("--i", i); dots.append(d); }
  const gates = el("div", "gate-strip");
  GATES.forEach((g, i) => {
    const step = el("div", "gate" + (i === GATES.length - 1 ? " is-you" : ""));
    step.style.setProperty("--i", i);
    step.append(el("span", "", g));
    gates.append(step);
  });
  const stats = el("div", "run-stats is-three");
  const nums = ACCURACY_STATS.map((a) => {
    const cell = el("div", "run-stat");
    const big = el("strong", "", "0");
    cell.append(big, el("span", "", a.label));
    stats.append(cell);
    return big;
  });
  host.append(dots, el("div", "scripted-title", "every story passes every gate, in order"), gates, stats);
  return () => ACCURACY_STATS.forEach((a, i) => setTimeout(() => counter(nums[i], a.value, { decimals: a.decimals ?? 0, suffix: a.suffix ?? "" }),
    reduce ? 0 : 1200));
}

function valuePanel(host) {
  const stats = el("div", "run-stats is-four");
  const nums = VALUE_STATS.map((s) => {
    const cell = el("div", "run-stat");
    const big = el("strong", "", "0");
    cell.append(big, el("span", "", s.label));
    stats.append(cell);
    return big;
  });
  const total = RUN_STAGES.reduce((sum, s) => sum + s.minutes, 0);
  const strip = el("div", "run-strip");
  RUN_STAGES.forEach((s, i) => {
    const seg = el("div", "run-seg");
    seg.style.setProperty("--w", s.minutes / total);
    seg.style.setProperty("--i", i);
    seg.title = `${s.name}: ${s.minutes} min`;
    seg.append(el("span", "seg-long", s.minutes / total < 0.12 ? `${s.minutes}m` : `${s.name} ${s.minutes}m`), el("span", "seg-short", `${s.minutes}m`));
    strip.append(seg);
  });
  // small beats big: the shipped search model against one 4.5 times its size
  const vs = el("div", "versus");
  vs.append(el("div", "scripted-title", "small beats big: the shipped search model vs one 4.5× its size"));
  for (const [label, small, big, smallText, bigText, better] of SMALL_VS_BIG) {
    const row = el("div", "vs-row");
    const pair = el("div", "vs-pair");
    for (const [value, text, cls] of [[small, smallText, "is-small"], [big, bigText, "is-big"]]) {
      const bar = el("div", `vs-bar ${cls}${value < 0.45 ? " is-outside" : ""}`);
      bar.style.setProperty("--to", value);
      bar.append(el("span", "", text));
      pair.append(bar);
    }
    row.append(el("div", "vs-label", `${label}${better ? ` · ${better}` : ""}`), pair);
    vs.append(row);
  }
  host.append(stats, el("div", "run-strip-label", "agent minutes per stage: use cases 2, design 8, plan 16, build 7"), strip, vs);
  return () => VALUE_STATS.forEach((s, i) => counter(nums[i], s.value, { suffix: s.of ? `/${s.of}` : s.suffix ?? "" }));
}

function localDiagram(host) {
  const box = el("div", "local-box");
  box.append(el("div", "local-title", "your machine"));
  const inside = el("div", "local-inside");
  LOCAL.forEach((item, i) => {
    const node = el("div", "local-node");
    node.style.setProperty("--i", i);
    node.append(el("b", "", item.name), el("span", "", item.note));
    inside.append(node);
  });
  box.append(inside);
  const out = el("div", "local-out");
  OUTSIDE.forEach((item, i) => {
    const node = el("div", "out-node" + (item.off ? " is-off" : ""));
    node.style.setProperty("--i", i);
    node.append(el("i", "", ""), el("b", "", item.name), el("span", "", item.note));
    out.append(node);
  });
  const row = el("div", "local-row");
  row.append(box, out);
  const licences = el("div", "licences");
  LICENCES.forEach(([name, licence], i) => {
    const chip = el("span", "licence");
    chip.style.setProperty("--i", i);
    chip.append(el("b", "", name), el("span", "", licence));
    licences.append(chip);
  });
  host.append(row, el("div", "scripted-title", "every part is open source"), licences);
  return () => {};
}

const CHARTS = { local: localDiagram, wins: winsBand, tokens: tokensChart, proof: proofChart, value: valuePanel, models: modelsChart, text: textVsModelChart, summaries: summariesChart, index: indexChart, run: runPanel, decides: decidesMatrix };

/* ------------------------------------------------------------------ wiring: draw each chart, animate it once when seen */

for (const host of document.querySelectorAll("[data-chart]")) {
  const build = CHARTS[host.dataset.chart];
  if (!build) continue;
  host.textContent = "";
  const play = build(host);
  host.classList.add("is-ready");
  const start = () => { host.classList.add("is-in"); play(); };
  if (reduce || !("IntersectionObserver" in window)) { start(); continue; }
  const watch = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) { watch.disconnect(); start(); }
  }, { threshold: 0.35 });
  watch.observe(host);
}

export { DOCS };
