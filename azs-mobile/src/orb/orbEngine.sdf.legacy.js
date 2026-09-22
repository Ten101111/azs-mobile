// Движок орба «Мобильного аналитика»: WebGL2 + пружинная машина состояний.
// Без зависимостей — одно и то же ядро работает в демо-странице и в React.
//
//   const orb = createOrb(canvas, { quality: "auto", interactive: true });
//   orb.setState("analyzing");        // idle | listening | searching | analyzing | forming | ready | refused | error
//   orb.demo.start(); orb.demo.stop();
//   orb.destroy();
//
// Как устроены переходы. Состояние — это не набор анимаций, а точка в
// пространстве из десяти параметров (свет, волна, поток, эрозия, порядок,
// столбцы…). При смене состояния меняются только цели; к ним каждый
// параметр идёт собственной пружиной со своей частотой и демпфированием.
// У эрозии масса больше, у света меньше — поэтому переходы перекрываются
// сами собой, и ни один из них не выглядит как отдельный клип.
import { ORB_VERT, ORB_FRAG } from "./orbShader.js";

export const ORB_STATE_NAMES = ["idle", "listening", "searching", "analyzing", "forming", "ready", "refused", "error"];

export const ORB_LABELS = {
  idle: "Готов к работе",
  listening: "Слушаю",
  searching: "Ищу данные",
  analyzing: "Анализирую",
  forming: "Формирую ответ",
  ready: "Готово",
  refused: "Вне области данных",
  error: "Ответ не получен",
};

// Цели параметров по состояниям.
//                 breath wave flow erode order light bars level mute slip
const TARGETS = {
  idle:      { scale: 1.000, breath: 1.0, wave: 0.0, flow: 0.0,  erode: 0.0,  order: 0.0, light: 0.55, bars: 0.0,  level: 0, mute: 0, slip: 0 },
  listening: { scale: 1.020, breath: 0.6, wave: 1.0, flow: 0.15, erode: 0.0,  order: 0.0, light: 0.72, bars: 1.0,  level: 0, mute: 0, slip: 0 },
  searching: { scale: 1.015, breath: 0.3, wave: 0.2, flow: 1.0,  erode: 0.14, order: 0.0, light: 0.82, bars: 0.25, level: 0, mute: 0, slip: 0 },
  analyzing: { scale: 1.040, breath: 0.2, wave: 0.1, flow: 0.85, erode: 1.0,  order: 0.0, light: 1.00, bars: 0.45, level: 0, mute: 0, slip: 0 },
  forming:   { scale: 1.020, breath: 0.3, wave: 0.0, flow: 0.6,  erode: 0.35, order: 1.0, light: 0.85, bars: 0.1,  level: 0, mute: 0, slip: 0 },
  ready:     { scale: 1.000, breath: 0.8, wave: 0.0, flow: 0.0,  erode: 0.0,  order: 0.4, light: 0.62, bars: 0.0,  level: 0, mute: 0, slip: 0 },
  refused:   { scale: 0.985, breath: 0.4, wave: 0.0, flow: 0.0,  erode: 0.0,  order: 0.0, light: 0.28, bars: 0.0,  level: 1, mute: 1, slip: 0 },
  error:     { scale: 0.985, breath: 0.3, wave: 0.0, flow: 0.0,  erode: 0.0,  order: 0.0, light: 0.18, bars: 0.0,  level: 0, mute: 1, slip: 1 },
};

// Пружины: частота (Гц) и коэффициент демпфирования. 1 — без перелёта,
// меньше — лёгкий перелёт, который читается как масса.
const SPRINGS = {
  scale:  { f: 0.50, z: 0.9 },
  breath: { f: 0.45, z: 1.0 },
  wave:   { f: 0.70, z: 0.85 },
  flow:   { f: 0.55, z: 0.95 },
  erode:  { f: 0.32, z: 1.0 },    // самая тяжёлая: раскрывается и закрывается медленно
  order:  { f: 0.42, z: 1.0 },
  light:  { f: 0.90, z: 0.80 },   // свет откликается первым
  bars:   { f: 0.80, z: 0.75 },
  level:  { f: 0.65, z: 1.0 },
  mute:   { f: 0.80, z: 1.0 },
  slip:   { f: 1.60, z: 0.50 },   // короткий сбой с перелётом
  pointerX: { f: 1.3, z: 1.0 },
  pointerY: { f: 1.3, z: 1.0 },
  hover:  { f: 1.2, z: 1.0 },
};

const DEMO_SCRIPT = [
  ["idle", 2000], ["listening", 2000], ["searching", 2500],
  ["analyzing", 4000], ["forming", 2500], ["ready", 2000],
];

function makeSpring(spec, value = 0) {
  const w = 2 * Math.PI * spec.f;
  return { x: value, v: 0, k: w * w, c: 2 * spec.z * w, target: value };
}
function stepSpring(s, dt) {
  // полунеявный Эйлер: устойчив при любом разумном dt
  const a = -s.k * (s.x - s.target) - s.c * s.v;
  s.v += a * dt;
  s.x += s.v * dt;
  return s.x;
}

function compile(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(sh);
    gl.deleteShader(sh);
    throw new Error("Шейдер орба не собрался: " + log);
  }
  return sh;
}

export function isOrbSupported() {
  try {
    const c = document.createElement("canvas");
    return !!c.getContext("webgl2");
  } catch { return false; }
}

export function createOrb(canvas, options = {}) {
  const opts = {
    quality: "auto",          // auto | high | low
    interactive: true,        // параллакс и подсветка от указателя
    tapToListen: false,       // клик по орбу переводит idle → listening
    reducedMotion: "auto",    // auto | true | false
    initialState: "idle",     // с какого состояния стартуют пружины (для бесшовной передачи)
    maxDpr: null,
    onState: null,
    ...options,
  };

  const gl = canvas.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false, powerPreference: "low-power" });
  if (!gl) throw new Error("WebGL2 недоступен");

  const prog = gl.createProgram();
  gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, ORB_VERT));
  gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, ORB_FRAG));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("Программа орба не слинковалась: " + gl.getProgramInfoLog(prog));
  gl.useProgram(prog);

  const vao = gl.createVertexArray();
  gl.bindVertexArray(vao);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const aPos = gl.getAttribLocation(prog, "aPos");
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

  const U = {};
  for (const name of ["uRes", "uTime", "uFlowT", "uErodeT", "uWaveT", "uPointer", "uQuality",
    "uScale", "uBreath", "uWave", "uFlow", "uErode", "uOrder", "uLight", "uBars", "uLevel", "uPulse", "uMute", "uSlip"]) {
    U[name] = gl.getUniformLocation(prog, name);
  }

  // --- качество и DPR ---------------------------------------------------------
  const isMobile = /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && matchMedia("(pointer: coarse)").matches);
  const quality = opts.quality === "auto" ? (isMobile ? "low" : "high") : opts.quality;
  const dprCap = opts.maxDpr || (quality === "low" ? 1.5 : 2);

  // --- reduced motion ---------------------------------------------------------
  const rmQuery = matchMedia("(prefers-reduced-motion: reduce)");
  let reduced = opts.reducedMotion === "auto" ? rmQuery.matches : !!opts.reducedMotion;
  const onRm = () => { if (opts.reducedMotion === "auto") reduced = rmQuery.matches; };
  rmQuery.addEventListener?.("change", onRm);

  // --- состояние --------------------------------------------------------------
  const springs = {};
  const start = TARGETS[opts.initialState] || TARGETS.idle;
  for (const k of Object.keys(SPRINGS)) springs[k] = makeSpring(SPRINGS[k], start[k] ?? 0);
  let state = TARGETS[opts.initialState] ? opts.initialState : "idle";
  let pulse = { t: -1 };                 // −1 — нет импульса; иначе фаза 0..1
  const clocks = { flow: 0, erode: 0, wave: 0 };
  let pointerTarget = { x: 0, y: 0 };
  let hoverTarget = 0;

  function applyTargets() {
    const t = TARGETS[state];
    for (const k of Object.keys(t)) {
      let v = t[k];
      if (reduced) {
        // При отключённой анимации сложная деформация и волны не нужны:
        // состояние читается по свету, масштабу и столбцам.
        if (k === "erode" || k === "wave" || k === "flow") v = 0;
        if (k === "breath") v = Math.min(v, 0.5);
        if (k === "bars") v = Math.min(v, 0.3);        // столбцы дышат медленно и мало
      }
      springs[k].target = v;
    }
    springs.light.target += 0.15 * springs.hover.x;
  }

  function setState(next) {
    if (!TARGETS[next]) throw new Error(`Неизвестное состояние орба: ${next}`);
    if (next === state) return;
    const prev = state;
    state = next;
    if (next === "ready") pulse = { t: 0 };
    applyTargets();
    opts.onState?.(next, prev);
  }

  // --- указатель --------------------------------------------------------------
  function onMove(e) {
    const r = canvas.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * 2 - 1;
    const y = -(((e.clientY - r.top) / r.height) * 2 - 1);
    pointerTarget = { x: Math.max(-1, Math.min(1, x)), y: Math.max(-1, Math.min(1, y)) };
    hoverTarget = 1;
  }
  function onLeave() { pointerTarget = { x: 0, y: 0 }; hoverTarget = 0; }
  function onClick() { if (opts.tapToListen && state === "idle") setState("listening"); }
  if (opts.interactive) {
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerleave", onLeave);
    canvas.addEventListener("pointerdown", onMove);
    canvas.addEventListener("click", onClick);
  }

  // --- размер -----------------------------------------------------------------
  let cssW = 0, cssH = 0;
  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, dprCap);
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w; canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
    cssW = r.width; cssH = r.height;
  }
  const ro = new ResizeObserver(resize);
  ro.observe(canvas);
  resize();

  // --- цикл -------------------------------------------------------------------
  let raf = 0, last = performance.now() / 1000, t0 = last, running = true, visible = true, inView = true;
  let frames = 0, fpsWindowStart = last, fps = 0;

  const io = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => { inView = entries[0]?.isIntersecting !== false; kick(); }, { threshold: 0 })
    : null;
  io?.observe(canvas);
  const onVis = () => { visible = !document.hidden; kick(); };
  document.addEventListener("visibilitychange", onVis);

  function frame(nowMs) {
    raf = 0;
    if (!running || !visible || !inView) return;
    const now = nowMs / 1000;
    const dt = Math.min(0.05, Math.max(0.001, now - last));
    last = now;

    // пружины
    springs.pointerX.target = pointerTarget.x;
    springs.pointerY.target = pointerTarget.y;
    springs.hover.target = opts.interactive ? hoverTarget : 0;
    applyTargets();
    for (const k of Object.keys(springs)) stepSpring(springs[k], dt);

    // часы движутся с разной скоростью — в этом инерция самого движения
    const S = springs;
    clocks.flow  += dt * (0.2 + 1.4 * S.flow.x) * (1 - 0.6 * S.order.x);
    clocks.erode += dt * (0.25 + 0.9 * S.erode.x) * (1 - 0.75 * S.order.x);
    clocks.wave  += dt * (0.5 + 2.0 * S.wave.x + 0.8 * S.bars.x);

    // импульс готовности — одноразовый, 0.75 с, замедление к концу
    let pulseVal = 0;
    if (pulse.t >= 0) {
      pulse.t += dt / 0.75;
      if (pulse.t >= 1) pulse = { t: -1 };
      else pulseVal = 1 - Math.pow(1 - pulse.t, 3);
    }

    gl.uniform2f(U.uRes, canvas.width, canvas.height);
    gl.uniform1f(U.uTime, now - t0);
    gl.uniform1f(U.uFlowT, clocks.flow);
    gl.uniform1f(U.uErodeT, clocks.erode);
    gl.uniform1f(U.uWaveT, clocks.wave);
    gl.uniform2f(U.uPointer, S.pointerX.x, S.pointerY.x);
    gl.uniform1f(U.uQuality, quality === "high" ? 1 : 0);
    gl.uniform1f(U.uScale, S.scale.x);
    gl.uniform1f(U.uBreath, S.breath.x);
    gl.uniform1f(U.uWave, S.wave.x);
    gl.uniform1f(U.uFlow, S.flow.x);
    gl.uniform1f(U.uErode, S.erode.x);
    gl.uniform1f(U.uOrder, S.order.x);
    gl.uniform1f(U.uLight, S.light.x);
    gl.uniform1f(U.uBars, S.bars.x);
    gl.uniform1f(U.uLevel, S.level.x);
    gl.uniform1f(U.uPulse, pulseVal);
    gl.uniform1f(U.uMute, S.mute.x);
    gl.uniform1f(U.uSlip, S.slip.x);

    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    frames++;
    if (now - fpsWindowStart >= 1) { fps = frames / (now - fpsWindowStart); frames = 0; fpsWindowStart = now; }
    raf = requestAnimationFrame(frame);
  }
  function kick() { if (!raf && running && visible && inView) { last = performance.now() / 1000; raf = requestAnimationFrame(frame); } }
  kick();

  // --- демо -------------------------------------------------------------------
  let demoTimer = 0, demoIndex = -1;
  const demo = {
    running: false,
    start() {
      if (demo.running) return;
      demo.running = true; demoIndex = -1;
      const step = () => {
        demoIndex = (demoIndex + 1) % DEMO_SCRIPT.length;
        const [name, ms] = DEMO_SCRIPT[demoIndex];
        setState(name);
        demoTimer = setTimeout(step, ms);
      };
      step();
    },
    stop() { demo.running = false; clearTimeout(demoTimer); demoTimer = 0; },
  };

  return {
    setState,
    getState: () => state,
    get params() { const o = {}; for (const k of Object.keys(TARGETS.idle)) o[k] = springs[k].x; return o; },
    get fps() { return fps; },
    get quality() { return quality; },
    get reducedMotion() { return reduced; },
    setReducedMotion(v) { opts.reducedMotion = v; reduced = v === "auto" ? rmQuery.matches : !!v; },
    demo,
    pause() { running = false; },
    resume() { running = true; kick(); },
    destroy() {
      running = false;
      demo.stop();
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect(); io?.disconnect();
      document.removeEventListener("visibilitychange", onVis);
      rmQuery.removeEventListener?.("change", onRm);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("pointerdown", onMove);
      canvas.removeEventListener("click", onClick);
      gl.deleteProgram(prog); gl.deleteBuffer(buf); gl.deleteVertexArray(vao);
      gl.getExtension("WEBGL_lose_context")?.loseContext();
    },
  };
}
