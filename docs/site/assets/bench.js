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

/* ------------------------------------------------------------------ helpers */

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const fmt = (value, decimals = 3) => value.toFixed(decimals);

function counter(node, to, { decimals = 0, prefix = "", suffix = "", duration = 1100, from = 0 } = {}) {
  const show = (v) => { node.textContent = `${prefix}${v.toFixed(decimals)}${suffix}`; };
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

const CHARTS = { models: modelsChart, text: textVsModelChart, summaries: summariesChart, index: indexChart, run: runPanel, decides: decidesMatrix };

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
