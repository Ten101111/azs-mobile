// Малый орб ИИ-аналитика — холст 2D для мест, где WebGL-орб слишком тяжёл
// или слишком велик: строка этапа (20 px) и запасной вид вместо большого
// орба (64 px), когда WebGL недоступен.
//
// Идея. Орб собран из точек, как большой, но в каждом этапе точки
// перестраиваются в свою фигуру — этап читается по форме, а не только по
// скорости:
//
//   idle        сфера медленно дышит, почти вся графитовая
//   understand  «Понимаю вопрос»: сфера вращается, по экватору бежит
//               красный фокус — система вчитывается в вопрос
//   check       «Проверяю данные»: по сфере сверху вниз проходит полоса
//               проверки — точки в ней вспыхивают красным
//   compute     «Считаю показатели»: точки собираются в три столбца знака
//               ИИ-аналитика, столбцы меняют высоту вразнобой
//   write       «Формирую ответ»: точки выстраиваются в три строки текста,
//               «перо» идёт слева направо
//   done        три столбца встают полностью, один короткий импульс
//   refused     сфера сжимается и гаснет в графит
//   error       сфера графитовая, в кольце — разрыв
//
// Между фигурами точки перетекают (у каждой своя цель, движение — плавное
// приближение), поэтому смена этапа видна как превращение, а не как
// смена картинки. Цвета берутся из токенов приложения (--red, --text,
// --muted) — тема меняется вместе с приложением без отдельной палитры.
//
// Пресеты размеров настроены отдельно: на 20 px мало точек и они крупнее,
// иначе фигура рассыпается в серый шум; на 64 px точек больше и они мельче.
// Промежуточные размеры берут ближайший пресет.
//
// «Уменьшить движение»: точки не летают и не вращаются — фигура этапа стоит
// на месте и медленно меняет яркость (как индикатор ожидания, ИИ-14).

export const MINI_STATES = ["idle", "understand", "check", "compute", "write", "done", "refused", "error"];

export const MINI_LABELS = {
  idle: "ИИ-аналитик",
  understand: "Понимаю вопрос",
  check: "Проверяю данные",
  compute: "Считаю показатели",
  write: "Формирую ответ",
  done: "Ответ готов",
  refused: "Вне области данных",
  error: "Ответ не получен",
};

// dots — число точек, dot — радиус точки в долях размера холста,
// speed — множитель времени, ring — радиус фигуры в долях половины холста,
// flat — вместо сферы плоское кольцо (на 20 px сфера читается как пыль),
// figure — доля точек, уходящих в столбцы и строки (остальные держат круг),
// grow — увеличение столбцов относительно знака AiMark, spread — разнос
// столбцов по горизонтали, lineGap и lineWide — шаг и ширина строк текста.
// На 20 px столбцы и строки разнесены шире: иначе точки сливаются в пятно.
export const MINI_PRESETS = {
  20: { dots: 12, dot: 0.078, speed: 1.15, ring: 0.8, minAlpha: 0.45, flat: true, figure: 1, grow: 1.45, spread: 2.8, lineGap: 1.8, lineWide: 1.5 },
  64: { dots: 72, dot: 0.026, speed: 1.0, ring: 0.86, minAlpha: 0.22, flat: false, figure: 0.66, grow: 1.2, spread: 1.2, lineGap: 1.0, lineWide: 1.0 },
};

function presetFor(size) {
  return size <= 36 ? MINI_PRESETS[20] : MINI_PRESETS[64];
}

// Столбцы — те же пропорции, что у знака AiMark (viewBox 100×100):
// x = 41, 50, 59; высоты 18, 26, 34; общее основание y = 70.
const BARS = [
  { x: -0.18, h: 0.36 },
  { x: 0.0, h: 0.52 },
  { x: 0.18, h: 0.68 },
];
const BAR_BASE = 0.4;
// Строки текста в «Формирую ответ»: доля ширины и вертикальное положение.
const LINES = [
  { y: -0.3, w: 1.0 },
  { y: 0.0, w: 0.82 },
  { y: 0.3, w: 0.52 },
];

function fibonacciSphere(n) {
  const out = [];
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - ((i + 0.5) / n) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const a = golden * i;
    out.push([Math.cos(a) * r, y, Math.sin(a) * r]);
  }
  return out;
}

function readColors(el) {
  const css = getComputedStyle(el);
  const pick = (name, fallback) => (css.getPropertyValue(name) || "").trim() || fallback;
  return {
    red: pick("--red", "#E31E24"),
    ink: pick("--text", "#151A23"),
    mute: pick("--icon-muted", "#8D95A5"),
  };
}

function hexToRgb(value) {
  const hex = value.replace("#", "");
  const full = hex.length === 3 ? hex.split("").map((c) => c + c).join("") : hex;
  const num = parseInt(full, 16);
  if (Number.isNaN(num)) return [128, 128, 128];
  return [(num >> 16) & 255, (num >> 8) & 255, num & 255];
}

export function createMiniOrb(canvas, { size = 20, state = "idle", reducedMotion = "auto" } = {}) {
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Холст 2D недоступен");
  const preset = presetFor(size);
  const sphere = fibonacciSphere(preset.dots);
  // Порядок точек для фигур: по высоте на сфере — так при перестройке
  // соседние точки уходят в соседние места и фигура не «перемешивается».
  const order = sphere.map((p, i) => i).sort((a, b) => sphere[a][1] - sphere[b][1]);
  const rankOf = new Array(order.length);
  order.forEach((index, rank) => { rankOf[index] = rank; });
  const dots = sphere.map(() => ({ x: 0, y: 0, a: 0, r: 0, hot: 0 }));
  let current = MINI_STATES.includes(state) ? state : "idle";
  let colors = readColors(canvas);
  let rgb = { red: hexToRgb(colors.red), ink: hexToRgb(colors.ink), mute: hexToRgb(colors.mute) };
  let beatAt = -10;
  let doneAt = 0;
  let placed = false;

  const rmQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const schemeQuery = window.matchMedia("(prefers-color-scheme: dark)");
  let reduced = reducedMotion === "auto" ? rmQuery.matches : Boolean(reducedMotion);

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    canvas.width = Math.round(size * dpr);
    canvas.height = Math.round(size * dpr);
  }
  resize();

  function refreshColors() {
    colors = readColors(canvas);
    rgb = { red: hexToRgb(colors.red), ink: hexToRgb(colors.ink), mute: hexToRgb(colors.mute) };
  }

  // Цель каждой точки в момент t: координаты в [-1, 1], прозрачность,
  // масштаб точки и «жар» (0 — графит, 1 — красный).
  function target(i, t) {
    const [sx, sy, sz] = sphere[i];
    const rank = rankOf[i];
    const spin = t * 0.9;
    const cos = Math.cos(spin);
    const sin = Math.sin(spin);
    const rx = sx * cos + sz * sin;
    const rz = -sx * sin + sz * cos;
    const depth = (rz + 1) / 2; // 0 — дальняя сторона, 1 — ближняя
    // Плоский вариант: точки по кругу, круг медленно поворачивается.
    const ringAngle = (rank / preset.dots) * Math.PI * 2 + t * 0.5;
    const onSphere = preset.flat
      ? { x: Math.cos(ringAngle), y: Math.sin(ringAngle), a: 0.8, r: 1, hot: 0, z: 1 }
      : { x: rx, y: sy, a: 0.3 + 0.7 * depth, r: 0.75 + 0.35 * depth, hot: 0, z: rz };
    const grow = preset.grow;

    switch (current) {
      case "understand": {
        // Фокус ходит по ближней стороне слева направо и обратно; рядом с ним
        // точки краснеют — система вчитывается в вопрос.
        const fx = Math.sin(t * 1.6) * 0.8;
        const fy = Math.sin(t * 0.9) * 0.35;
        const d = Math.hypot(onSphere.x - fx, (onSphere.y - fy) * 1.2) + (onSphere.z < 0 ? 0.6 : 0);
        const hot = Math.max(0, 1 - d / (preset.flat ? 0.9 : 0.55));
        return { ...onSphere, hot, r: onSphere.r * (1 + 0.5 * hot), a: Math.max(onSphere.a, hot) };
      }
      case "check": {
        // Полоса проверки ходит сверху вниз и обратно.
        const band = Math.cos(t * 1.8) * 0.9;
        const hot = Math.max(0, 1 - Math.abs(onSphere.y - band) / (preset.flat ? 0.4 : 0.28));
        return { ...onSphere, x: onSphere.x * (1 + 0.08 * hot), hot, r: onSphere.r * (1 + 0.4 * hot), a: Math.max(onSphere.a * 0.8, hot) };
      }
      case "compute":
      case "done": {
        // Две трети точек — в столбцах, остальные держат контур круга.
        const inBars = Math.round(preset.dots * preset.figure);
        if (rank < inBars) {
          const bar = rank % 3;
          const slot = Math.floor(rank / 3);
          const slots = Math.ceil(inBars / 3);
          const wave = current === "done" ? 1 : 0.62 + 0.38 * Math.sin(t * 3.2 + bar * 2.1);
          const h = BARS[bar].h * grow * wave;
          const y = BAR_BASE * grow - (slot / Math.max(1, slots - 1)) * h;
          return { x: BARS[bar].x * preset.spread, y, a: 1, r: 1, hot: 1 };
        }
        const k = (rank - inBars) / Math.max(1, preset.dots - inBars);
        const ang = k * Math.PI * 2 + t * 0.6;
        return { x: Math.cos(ang), y: Math.sin(ang), a: 0.55, r: 0.8, hot: 0 };
      }
      case "write": {
        const inLines = Math.round(preset.dots * preset.figure);
        if (rank < inLines) {
          const line = rank % 3;
          const slot = Math.floor(rank / 3);
          const slots = Math.ceil(inLines / 3);
          const pos = slot / Math.max(1, slots - 1); // 0…1 вдоль строки
          const width = LINES[line].w * 1.2 * preset.lineWide;
          const x = -0.6 * preset.lineWide + pos * width;
          // «Перо» проходит строки по очереди; написанное остаётся, впереди — бледно.
          const head = (t * 0.55) % 1.3;
          const reach = head * 3 - line;
          const written = pos <= reach ? 1 : 0.25;
          const tip = Math.max(0, 1 - Math.abs(pos - reach) * 6);
          return { x, y: LINES[line].y * preset.lineGap, a: written, r: 1 + 0.4 * tip, hot: tip > 0.1 ? 1 : 0 };
        }
        const k = (rank - inLines) / Math.max(1, preset.dots - inLines);
        const ang = k * Math.PI * 2 - t * 0.4;
        return { x: Math.cos(ang), y: Math.sin(ang), a: 0.45, r: 0.8, hot: 0 };
      }
      case "refused":
        return { ...onSphere, x: onSphere.x * 0.82, y: onSphere.y * 0.82, a: onSphere.a * 0.6, hot: 0 };
      case "error": {
        const gap = Math.atan2(onSphere.y, onSphere.x);
        const cut = Math.abs(gap - 0.6) < 0.5 ? 0.08 : 1;
        return { ...onSphere, a: onSphere.a * 0.55 * cut, hot: 0 };
      }
      default: {
        const breath = 1 + 0.03 * Math.sin(t * 1.3);
        return { ...onSphere, x: onSphere.x * breath, y: onSphere.y * breath, a: onSphere.a * 0.8, hot: i % 7 === 0 ? 0.35 : 0 };
      }
    }
  }

  function draw(t, dt) {
    const w = canvas.width;
    const half = w / 2;
    const scale = half * preset.ring;
    const radius = w * preset.dot;
    ctx.clearRect(0, 0, w, w);
    const follow = reduced || !placed ? 1 : 1 - Math.exp(-dt * 9);
    const sinceBeat = t - beatAt;
    const beat = sinceBeat >= 0 && sinceBeat < 0.45 ? Math.sin((sinceBeat / 0.45) * Math.PI) : 0;
    const sinceDone = t - doneAt;
    const donePulse = current === "done" && sinceDone < 0.6 ? Math.sin((sinceDone / 0.6) * Math.PI) : 0;
    // При «Уменьшить движение» фигура стоит, а жизнь показывает яркость.
    const breathe = reduced && current !== "idle" && current !== "done" ? 0.75 + 0.25 * Math.sin(t * 2.6) : 1;
    // Сначала дальние, потом ближние — ближние ложатся сверху.
    const drawOrder = dots.map((d, i) => i);
    const goals = drawOrder.map((i) => target(i, reduced ? 0.4 : t));
    drawOrder.sort((a, b) => goals[a].a - goals[b].a);
    for (const i of drawOrder) {
      const goal = goals[i];
      const d = dots[i];
      d.x += (goal.x - d.x) * follow;
      d.y += (goal.y - d.y) * follow;
      d.a += (goal.a - d.a) * follow;
      d.r += (goal.r - d.r) * follow;
      d.hot += (goal.hot - d.hot) * follow;
      const hot = Math.min(1, d.hot + beat * 0.6 + donePulse);
      const base = current === "refused" || current === "error" ? rgb.mute : rgb.ink;
      const r = Math.round(base[0] + (rgb.red[0] - base[0]) * hot);
      const g = Math.round(base[1] + (rgb.red[1] - base[1]) * hot);
      const b = Math.round(base[2] + (rgb.red[2] - base[2]) * hot);
      const alpha = Math.max(preset.minAlpha * (d.a > 0.1 ? 1 : 0), Math.min(1, d.a)) * breathe;
      ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(half + d.x * scale, half + d.y * scale, radius * d.r * (1 + 0.25 * beat), 0, Math.PI * 2);
      ctx.fill();
    }
    placed = true;
  }

  let raf = 0;
  let last = performance.now() / 1000;
  let clock = 0;
  let visible = !document.hidden;
  let inView = true;

  function frame() {
    raf = 0;
    const now = performance.now() / 1000;
    const dt = Math.min(0.1, now - last);
    last = now;
    clock += dt * preset.speed;
    draw(clock, dt);
    if (visible && inView) raf = requestAnimationFrame(frame);
  }
  function kick() {
    if (!raf && visible && inView) {
      last = performance.now() / 1000;
      raf = requestAnimationFrame(frame);
    }
  }

  const io = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => { inView = entries[0]?.isIntersecting !== false; kick(); }, { threshold: 0 })
    : null;
  io?.observe(canvas);
  const onVis = () => { visible = !document.hidden; kick(); };
  document.addEventListener("visibilitychange", onVis);
  const onRm = () => { if (reducedMotion === "auto") reduced = rmQuery.matches; kick(); };
  rmQuery.addEventListener?.("change", onRm);
  // Тема приложения — системная: цвета перечитываются из токенов при смене.
  const onScheme = () => { refreshColors(); kick(); };
  schemeQuery.addEventListener?.("change", onScheme);
  kick();

  return {
    setState(next) {
      if (!MINI_STATES.includes(next) || next === current) return;
      current = next;
      if (next === "done") doneAt = clock;
      kick();
    },
    // Короткий импульс: закончился внутренний шаг (запрос, расчёт, график).
    beat() { beatAt = clock; kick(); },
    refreshColors,
    getState: () => current,
    destroy() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      io?.disconnect();
      document.removeEventListener("visibilitychange", onVis);
      rmQuery.removeEventListener?.("change", onRm);
      schemeQuery.removeEventListener?.("change", onScheme);
    },
  };
}
