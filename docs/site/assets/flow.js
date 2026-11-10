// The animated flow: how a feature moves through Dridh, stage by stage and step by step, including where it loops
// back and where it waits for a person. Plain JavaScript and SVG; each page styles it through CSS variables.
// Without JavaScript the page keeps its plain list of steps; with reduced motion nothing plays until asked.

const NS = "http://www.w3.org/2000/svg";
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Two layouts of the same diagram: wide for desktop, tall for phones. Paths run in the direction of travel. */
const LAYOUTS = {
  wide: {
    viewBox: "0 0 1000 320",
    nodes: {
      A: { kind: "step", x: 85, y: 160 }, B: { kind: "step", x: 265, y: 160 }, D1: { kind: "check", x: 440, y: 160 },
      C: { kind: "you", x: 615, y: 160 }, D2: { kind: "decide", x: 790, y: 160 },
    },
    size: { w: 140, h: 64, d: 46 },
    paths: {
      AB: "M155 160 H195", BD1: "M335 160 H394", D1C: "M486 160 H545", CD2: "M685 160 H744", X: "M836 160 H978",
      L1: "M440 114 V58 H265 V128", L2: "M790 206 V262 H265 V192",
    },
    labels: { D1C: [515, 148], X: [905, 148], L1: [352, 46], L2: [527, 286] },
  },
  tall: {
    viewBox: "0 0 360 720",
    nodes: {
      A: { kind: "step", x: 180, y: 52 }, B: { kind: "step", x: 180, y: 172 }, D1: { kind: "check", x: 180, y: 300 },
      C: { kind: "you", x: 180, y: 428 }, D2: { kind: "decide", x: 180, y: 556 },
    },
    size: { w: 196, h: 60, d: 46 },
    paths: {
      AB: "M180 82 V142", BD1: "M180 202 V254", D1C: "M180 346 V398", CD2: "M180 458 V510", X: "M180 602 V690",
      L1: "M226 300 H330 V172 H278", L2: "M134 556 H28 V172 H82",
    },
    labels: { D1C: [192, 376], X: [192, 650], L1: [238, 232], L2: [38, 364] },
  },
};

const DOC = { D1: "Complete?", D2: "Approve?", C: "You read it", L1: "no · fixes it, checks again", L2: "you ask for changes", D1C: "yes", X: "yes · next stage" };
const STAGES = [
  { name: "Use cases", labels: { ...DOC, A: "Asks you\nquestions", B: "Writes the\nuse cases" }, moves: [
    ["A", null, "The Coordinator asks you questions, one at a time, and writes down only your answers."],
    ["B", "AB", "It writes the use cases: who does what, and the rules."],
    ["D1", "BD1", "Dridh checks that the document is complete."],
    ["C", "D1C", "Complete. Now it stops and waits for you."],
    ["D2", "CD2", "You read it and decide. Only a person can approve."],
    [null, "X", "Approved. On to the design."],
  ] },
  { name: "Design", labels: { ...DOC, A: "Asks about\nthe design", B: "Writes the\ndesign" }, moves: [
    ["A", null, "It asks how the feature should be built."],
    ["B", "AB", "It writes the technical design."],
    ["D1", "BD1", "Dridh checks it."],
    ["B", "L1", "Something is missing, so it fixes the document…"],
    ["D1", "BD1", "…and checks it again."],
    ["C", "D1C", "Complete. It waits for you."],
    ["D2", "CD2", "You approve it."],
    [null, "X", "On to the plan."],
  ] },
  { name: "Plan", labels: { ...DOC, A: "Splits work\ninto stories", B: "Writes the\nplan" }, moves: [
    ["A", null, "It splits the work into small stories."],
    ["B", "AB", "It writes the plan."],
    ["D1", "BD1", "Dridh checks it."],
    ["C", "D1C", "Complete. It waits for you."],
    ["D2", "CD2", "You ask for a change."],
    ["B", "L2", "Your changes go back, and the plan is revised."],
    ["D1", "BD1", "Checked again."],
    ["C", "D1C", "Back to you."],
    ["D2", "CD2", "This time you approve."],
    [null, "X", "On to building."],
  ] },
  { name: "Build", labels: { ...DOC, A: "Builds a\nstory", B: "Runs its\ntests", D1: "Tests\npass?", C: "You review\nthe batch", L1: "no · repairs it (up to 3 tries)", L2: "you send it back", X: "yes · done" }, moves: [
    ["A", null, "Each story is built on its own, in a separate copy of your code."],
    ["B", "AB", "Its tests run."],
    ["D1", "BD1", "A test fails."],
    ["B", "L1", "It repairs the story and runs the tests again, up to three tries before it asks you."],
    ["D1", "BD1", "The tests pass."],
    ["C", "D1C", "Finished stories wait for you in a batch."],
    ["D2", "CD2", "You look through the batch and approve it."],
    [null, "X", "Every story is built and approved."],
  ] },
];
const TRACK = ["Idea", "Use cases", "Design", "Plan", "Build", "Done"];

function el(name, attrs = {}, parent) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

function draw(layout, id) {
  const svg = el("svg", { viewBox: layout.viewBox, class: `flow-svg flow-${id}`, "aria-hidden": "true", focusable: "false" });
  const defs = el("defs", {}, svg);
  const marker = el("marker", { id: `arrow-${id}`, viewBox: "0 0 10 10", refX: "8", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" }, defs);
  el("path", { d: "M0 0 L10 5 L0 10 z", class: "flow-arrow" }, marker);
  const paths = {};
  for (const [key, d] of Object.entries(layout.paths)) {
    paths[key] = el("path", { d, class: `flow-path ${key.startsWith("L") ? "is-loop" : ""}`, "marker-end": `url(#arrow-${id})` }, svg);
  }
  const labels = {};
  for (const [key, [x, y]] of Object.entries(layout.labels)) {
    labels[key] = el("text", { x, y, class: `flow-label ${key.startsWith("L") ? "is-loop" : ""}`, "text-anchor": id === "tall" ? "start" : "middle" }, svg);
  }
  const nodes = {};
  const { w, h, d } = layout.size;
  for (const [key, n] of Object.entries(layout.nodes)) {
    const g = el("g", { class: `flow-node is-${n.kind}` }, svg);
    if (n.kind === "check" || n.kind === "decide") {
      el("path", { d: `M${n.x} ${n.y - d} L${n.x + d} ${n.y} L${n.x} ${n.y + d} L${n.x - d} ${n.y} Z`, class: "flow-shape" }, g);
    } else {
      el("rect", { x: n.x - w / 2, y: n.y - h / 2, width: w, height: h, class: "flow-shape" }, g);
    }
    nodes[key] = { g, text: el("text", { x: n.x, y: n.y, class: "flow-text", "text-anchor": "middle" }, g), x: n.x, y: n.y };
  }
  const token = el("circle", { r: "8", cx: layout.nodes.A.x, cy: layout.nodes.A.y, class: "flow-token" }, svg);
  return { svg, paths, labels, nodes, token, id };
}

function setText(textNode, value) {
  const lines = value.split("\n");
  textNode.replaceChildren();
  lines.forEach((line, i) => {
    const t = el("tspan", { x: textNode.getAttribute("x"), dy: i === 0 ? `${(-(lines.length - 1) * 0.6) + 0.35}em` : "1.2em" }, textNode);
    t.textContent = line;
  });
}

/* Narrow screens: loop labels break onto short lines beside their loop. */
function wrap(text, width) {
  const lines = [];
  for (const word of text.split(" ")) {
    const last = lines[lines.length - 1];
    if (last && `${last} ${word}`.length <= width) lines[lines.length - 1] = `${last} ${word}`;
    else lines.push(word);
  }
  return lines.join("\n");
}

export function mountFlow(root) {
  root.classList.add("flow-ready");
  const head = document.createElement("div");
  head.className = "flow-head";
  const track = document.createElement("ol");
  track.className = "flow-track";
  for (const name of TRACK) { const li = document.createElement("li"); li.textContent = name; track.append(li); }
  const controls = document.createElement("div");
  controls.className = "flow-controls";
  const toggle = Object.assign(document.createElement("button"), { type: "button", className: "flow-btn" });
  const restart = Object.assign(document.createElement("button"), { type: "button", className: "flow-btn", textContent: "Restart" });
  controls.append(toggle, restart);
  head.append(track, controls);

  const stage = document.createElement("div");
  stage.className = "flow-stage";
  const diagrams = [draw(LAYOUTS.wide, "wide"), draw(LAYOUTS.tall, "tall")];
  stage.append(...diagrams.map((dg) => dg.svg));
  const caption = document.createElement("p");
  caption.className = "flow-caption";
  const legend = document.createElement("p");
  legend.className = "flow-legend";
  legend.innerHTML = `<span class="key key-you"></span>waits for you <span class="key key-check"></span>Dridh checks <span class="key key-loop"></span>loops back`;
  root.prepend(head);
  head.after(stage, caption, legend);

  let playing = !reduce;
  let wake = null;
  let run = 0;

  const visible = () => diagrams.find((dg) => getComputedStyle(dg.svg).display !== "none") ?? diagrams[0];
  const wait = (ms) => new Promise((resolve) => { wake = resolve; setTimeout(resolve, ms); });
  const paused = () => new Promise((resolve) => { const check = () => (playing ? resolve() : setTimeout(check, 120)); check(); });

  function label(stageIndex) {
    const s = STAGES[stageIndex];
    for (const dg of diagrams) {
      for (const [key, n] of Object.entries(dg.nodes)) setText(n.text, s.labels[key]);
      for (const [key, t] of Object.entries(dg.labels)) {
        if (dg.id === "tall" && key.startsWith("L")) setText(t, wrap(s.labels[key], 13));
        else t.textContent = s.labels[key];
      }
    }
    [...track.children].forEach((li, i) => { li.classList.toggle("is-done", i < stageIndex + 1); li.classList.toggle("is-now", i === stageIndex + 1); });
  }
  function focus(key) {
    for (const dg of diagrams) for (const [k, n] of Object.entries(dg.nodes)) n.g.classList.toggle("is-active", k === key);
  }
  function place(key) {
    for (const dg of diagrams) { const n = dg.nodes[key]; dg.token.setAttribute("cx", n.x); dg.token.setAttribute("cy", n.y); dg.token.classList.remove("is-gone"); }
  }
  function travel(pathKey, myRun) {
    const dg = visible();
    const path = dg.paths[pathKey];
    const length = path.getTotalLength();
    const duration = Math.min(1500, Math.max(500, length * (dg.id === "wide" ? 5 : 3.2)));
    path.classList.add("is-lit");
    const other = diagrams.find((x) => x !== dg);
    return new Promise((resolve) => {
      let start = null;
      let elapsed = 0;
      const frame = (now) => {
        if (myRun !== run) return resolve();
        if (!playing) { start = null; requestAnimationFrame(frame); return; }
        if (start === null) start = now - elapsed;
        elapsed = now - start;
        const t = Math.min(1, elapsed / duration);
        const eased = t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2; // ease-in-out for movement on screen
        const p = path.getPointAtLength(eased * length);
        dg.token.setAttribute("cx", p.x); dg.token.setAttribute("cy", p.y);
        if (t < 1) requestAnimationFrame(frame);
        else { path.classList.remove("is-lit"); other.token.classList.add("is-gone"); resolve(); }
      };
      requestAnimationFrame(frame);
    });
  }

  async function play() {
    const myRun = ++run;
    for (const dg of diagrams) dg.token.classList.remove("is-gone");
    [...track.children].forEach((li, i) => { li.classList.toggle("is-now", i === 0); li.classList.remove("is-done"); });
    label(0); focus(null); place("A");
    for (const dg of diagrams) dg.token.classList.add("is-gone");
    caption.textContent = "You describe the feature in plain words. The Coordinator takes it from there.";
    await wait(2200); await paused(); if (myRun !== run) return;
    for (let s = 0; s < STAGES.length; s += 1) {
      label(s);
      for (const [node, via, say] of STAGES[s].moves) {
        if (myRun !== run) return;
        if (via) { focus(null); await travel(via, myRun); } else { place(node); }
        if (myRun !== run) return;
        focus(node);
        caption.textContent = `${STAGES[s].name}: ${say}`;
        await wait(node ? 1900 : 1500); await paused();
      }
    }
    if (myRun !== run) return;
    focus(null);
    for (const dg of diagrams) dg.token.classList.add("is-gone");
    [...track.children].forEach((li, i) => { li.classList.toggle("is-done", i < TRACK.length - 1); li.classList.toggle("is-now", i === TRACK.length - 1); });
    caption.textContent = "Done: you mark the feature complete. Pull requests are optional, and Dridh never merges.";
    await wait(4200); await paused();
    if (myRun === run) play();
  }

  function sync() { toggle.textContent = playing ? "Pause" : "Play"; toggle.setAttribute("aria-pressed", String(!playing)); }
  toggle.addEventListener("click", () => { playing = !playing; sync(); if (playing && run === 0) play(); wake?.(); });
  restart.addEventListener("click", () => { playing = true; sync(); play(); });
  sync();

  // Start when it scrolls into view, so the walkthrough begins at the beginning.
  label(0); place("A");
  for (const dg of diagrams) dg.token.classList.add("is-gone");
  caption.textContent = reduce ? "Press Play to walk through how a feature moves." : "You describe the feature in plain words. The Coordinator takes it from there.";
  if (!reduce) {
    const seen = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) { seen.disconnect(); play(); } }, { threshold: 0.35 });
    seen.observe(root);
  }
}

for (const root of document.querySelectorAll("[data-flow]")) mountFlow(root);
