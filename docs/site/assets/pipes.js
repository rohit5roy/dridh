// The page's background: a faded network of pipes over the whole screen, a symbol of how Dridh moves work. Packets
// flow along the pipes, split into parallel lanes, wait at gates (amber squares: a person decides), loop back now and
// then (a check failed) and merge again. The section in view sets the mood: questions wander in the hero, the plan
// lines up, the build fans out into many lanes, review holds work at the gates and passes it, delivery merges it.
// Plain canvas on the page's 48 px grid, no library; paused when the tab is hidden; drawn still for reduced motion.

const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const canvas = document.createElement("canvas");
canvas.className = "pipes";
canvas.setAttribute("aria-hidden", "true");
document.body.prepend(canvas);
const ctx = canvas.getContext("2d");

const CELL = 48; // the hero's grid
const AMBER = [255, 176, 0];
const PASS = [143, 209, 138];
const LINE = [143, 141, 124];

/* Moods, one per stage (data-stage on each section). spawn: packets per second; speed: cells per second; branch: chance to
   take a connector at a junction; hold: chance a gate holds a packet; loop: chance to be sent back a little; lit: share
   of pipes that glow; pass: gates flash green as work passes a check; merge: lanes join towards the top. */
const MOODS = [
  { spawn: 3, speed: 1.6, branch: 0.55, hold: 0.25, loop: 0.12, lit: 0.25, pass: 0 },
  { spawn: 4, speed: 2.2, branch: 0.35, hold: 0.35, loop: 0.1, lit: 0.35, pass: 0 },
  { spawn: 5, speed: 2.6, branch: 0.12, hold: 0.3, loop: 0.04, lit: 0.45, pass: 0 },
  { spawn: 10, speed: 3.6, branch: 0.45, hold: 0.1, loop: 0.08, lit: 0.6, pass: 0 },
  { spawn: 6, speed: 2.6, branch: 0.15, hold: 0.7, loop: 0.05, lit: 0.45, pass: 1 },
  { spawn: 5, speed: 3.2, branch: 0.6, hold: 0.15, loop: 0, lit: 0.35, pass: 0.5, merge: true },
];

let width = 0, height = 0, dpr = 1;
let rows = []; // horizontal pipes: { y, from, to, lit } (from/to in cells)
let links = []; // vertical connectors between neighbouring pipes: { x, a, b }
let gates = []; // { x, row, flash }
let packets = [];
let mood = { ...MOODS[0] };
let target = 0;
let last = performance.now();

/* ------------------------------------------------------------------ a random network on the grid */

function rand(seed) { // deterministic: the same network for the same screen size
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
}

function build() {
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  width = window.innerWidth; height = window.innerHeight;
  canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
  const r = rand(width * 7 + height);
  const cols = Math.ceil(width / CELL) + 1;
  rows = []; links = []; gates = [];
  for (let y = CELL * 1.5; y < height; y += CELL * (r() < 0.5 ? 1 : 2)) {
    const from = r() < 0.7 ? 0 : Math.floor(r() * cols * 0.3); // most pipes cross the whole screen, some start or end inside
    const to = r() < 0.7 ? cols : Math.floor(cols * (0.6 + r() * 0.4));
    rows.push({ y, from, to, lit: r() });
  }
  for (let i = 0; i + 1 < rows.length; i += 1) {
    for (let c = 2; c < cols - 1; c += 1) {
      if (r() < 0.09 && c > Math.max(rows[i].from, rows[i + 1].from) && c < Math.min(rows[i].to, rows[i + 1].to)) {
        links.push({ x: c * CELL, a: i, b: i + 1 });
      }
    }
  }
  rows.forEach((row, i) => {
    for (let c = row.from + 3; c < row.to - 1; c += 3 + Math.floor(r() * 6)) gates.push({ x: c * CELL, row: i, flash: 0 });
  });
  packets = [];
}

/* ------------------------------------------------------------------ packets */

function spawn() {
  if (!rows.length) return null;
  const row = Math.floor(Math.random() * rows.length);
  const packet = { row, x: rows[row].from * CELL, y: rows[row].y, toY: null, nextRow: row, wait: 0, trail: [], age: 0, back: 0 };
  packets.push(packet);
  return packet;
}

function step(dt) {
  if (Math.random() < dt * mood.spawn * (width / 1280) && packets.length < 140) spawn();
  for (const p of packets) {
    p.age += dt;
    if (p.wait > 0) { p.wait -= dt; continue; }
    if (p.toY !== null) { // along a connector to the neighbouring pipe
      const move = mood.speed * CELL * dt;
      if (Math.abs(p.toY - p.y) <= move) { p.y = p.toY; p.toY = null; p.row = p.nextRow; } else p.y += Math.sign(p.toY - p.y) * move;
      continue;
    }
    const row = rows[p.row];
    p.y = row.y;
    const before = p.x;
    if (p.back > 0) { // sent back a few cells: the repair loop
      p.x -= mood.speed * CELL * dt;
      p.back -= mood.speed * dt;
      if (p.back <= 0) p.wait = 0.2;
      continue;
    }
    p.x += mood.speed * CELL * dt;
    for (const g of gates) { // a gate: maybe wait (a person decides), then pass (a check lights)
      if (g.row === p.row && before < g.x && p.x >= g.x) {
        if (Math.random() < mood.hold) { p.x = g.x; p.wait = 0.5 + Math.random() * 1.2; }
        g.flash = 1;
        if (Math.random() < mood.loop) p.back = 1.5 + Math.random() * 2;
      }
    }
    for (const l of links) { // a junction: maybe split into another lane, or merge
      if ((l.a === p.row || l.b === p.row) && before < l.x && p.x >= l.x && Math.random() < mood.branch) {
        const other = l.a === p.row ? l.b : l.a;
        if (mood.merge && rows[other].y > row.y && Math.random() < 0.6) continue; // delivery merges towards the top
        p.x = l.x; p.nextRow = other; p.toY = rows[other].y;
        break;
      }
    }
  }
  for (const p of packets) {
    p.trail.push({ x: p.x, y: p.y });
    if (p.trail.length > 9) p.trail.shift();
  }
  packets = packets.filter((p) => p.age < 40 && p.x <= rows[p.row].to * CELL && p.x >= rows[p.row].from * CELL - 1);
  for (const g of gates) g.flash = Math.max(0, g.flash - dt * 1.6);
}

/* ------------------------------------------------------------------ drawing */

const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

function draw() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.lineWidth = 1;
  for (const row of rows) { // pipes; more of them glow in busier stages
    ctx.strokeStyle = row.lit < mood.lit ? rgba(AMBER, 0.13) : rgba(LINE, 0.09);
    ctx.beginPath(); ctx.moveTo(row.from * CELL, row.y + 0.5); ctx.lineTo(row.to * CELL, row.y + 0.5); ctx.stroke();
  }
  ctx.strokeStyle = rgba(LINE, 0.08);
  for (const l of links) { ctx.beginPath(); ctx.moveTo(l.x + 0.5, rows[l.a].y); ctx.lineTo(l.x + 0.5, rows[l.b].y); ctx.stroke(); }
  ctx.fillStyle = rgba(LINE, 0.18);
  for (const l of links) { ctx.fillRect(l.x - 1.5, rows[l.a].y - 1.5, 3, 3); ctx.fillRect(l.x - 1.5, rows[l.b].y - 1.5, 3, 3); }
  for (const g of gates) { // gates light when work passes; green when it passes a check
    const y = rows[g.row].y;
    ctx.fillStyle = "#0f100d";
    ctx.fillRect(g.x - 4, y - 4, 8, 8);
    ctx.strokeStyle = rgba(mood.pass > 0.5 && g.flash > 0.05 ? PASS : AMBER, 0.18 + 0.5 * g.flash);
    ctx.strokeRect(g.x - 3.5, y - 3.5, 7, 7);
  }
  for (const p of packets) { // packets with a short fading trail
    p.trail.forEach((t, i) => {
      ctx.fillStyle = rgba(AMBER, 0.05 + 0.25 * (i / p.trail.length));
      ctx.fillRect(t.x - 1, t.y - 1, 2, 2);
    });
    ctx.fillStyle = rgba(AMBER, p.wait > 0 ? 0.85 : 0.6);
    ctx.fillRect(p.x - 2, p.y - 2, 4, 4);
  }
}

/* ------------------------------------------------------------------ loop, and the section in view */

function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  const goal = MOODS[target];
  for (const key of ["spawn", "speed", "branch", "hold", "loop", "lit", "pass"]) mood[key] += (goal[key] - mood[key]) * Math.min(1, dt * 1.5);
  mood.merge = goal.merge;
  step(dt);
  draw();
  if (!document.hidden) requestAnimationFrame(frame);
}

function still() { // reduced motion: a settled picture of the same network
  packets = [];
  for (let k = 0; k < 40 && rows.length; k += 1) {
    const row = k % rows.length;
    const x = (rows[row].from + (rows[row].to - rows[row].from) * ((k * 0.618) % 1)) * CELL;
    packets.push({ row, x, y: rows[row].y, toY: null, nextRow: row, wait: 0, trail: [], age: 0, back: 0 });
  }
  draw();
}

build();
window.addEventListener("resize", () => { build(); if (reduce) still(); });

const seen = new Map();
const watch = new IntersectionObserver((entries) => {
  for (const e of entries) seen.set(e.target, e.intersectionRatio);
  let best = null, ratio = 0;
  for (const [node, r] of seen) if (r > ratio) { best = node; ratio = r; }
  if (best) {
    target = Math.min(MOODS.length - 1, Number(best.dataset.stage) || 0);
    if (reduce) { mood = { ...MOODS[target] }; still(); }
  }
}, { threshold: [0, 0.15, 0.3, 0.5, 0.75, 1] });
document.querySelectorAll("[data-stage]").forEach((s) => watch.observe(s));

if (reduce) still();
else {
  for (let k = 0; k < 40; k += 1) { const p = spawn(); if (p) p.x += Math.random() * width * 0.8; }
  requestAnimationFrame(frame);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { last = performance.now(); requestAnimationFrame(frame); } });
}
