// Орб «Мобильного аналитика» — порт «Recursive Erosion» (Meng To, ThreeUI,
// MIT), переведённый в корпоративные цвета и посаженный на машину состояний.
//
// Что это. Не Three.js и не SDF-шейдер, а частицы: 2500 точек на сфере
// Фибоначчи, каждая смещается симплекс-шумом (сфера мнётся и дышит),
// второй шум с порогом выедает в решётке дыры (эрозия), семь «червей»
// ползут по большим кругам и подсвечивают точки рядом с собой, третий
// проход рисует их хвосты жемчужинами со свечением. Поверх — зерно.
// Геометрия, шум, черви, проходы — дословно из исходника, включая
// константы. Изменены: палитра (оранжевая → красная #E31E24), убран
// бейдж-подпись, а параметры, которые в оригинале были константами
// (порог эрозии, амплитуда смятия, сила червей, скорость петли, яркость),
// стали пружинными и зависят от состояния.
//
//   const orb = createOrb(canvas, { quality: "auto" });
//   orb.setState("analyzing");   // idle | listening | searching | analyzing | forming | ready | refused | error
//   orb.demo.start(); orb.destroy();
//
// Состояния — цели параметров; к ним каждый идёт своей пружиной, поэтому
// переходы перекрываются и ни один не выглядит отдельным клипом.

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

// Цели параметров. В оригинале: thr ≈ 0.19, morph = 1, worms = 1, speed = 1.
//   thr    — порог эрозии: чем выше, тем больше дыр
//   morph  — амплитуда смятия сферы
//   worms  — сила червей и их хвостов
//   speed  — скорость петли (оригинал — 1 оборот за 3,99 с)
//   light  — общая яркость
//   mute   — уход в серый металл (отказ, ошибка)
const TARGETS = {
  idle:      { thr: 0.06, morph: 0.45, worms: 0.35, speed: 0.45, light: 1.00, mute: 0 },
  listening: { thr: 0.09, morph: 0.70, worms: 0.55, speed: 0.75, light: 1.00, mute: 0 },
  searching: { thr: 0.13, morph: 0.75, worms: 1.15, speed: 1.00, light: 1.05, mute: 0 },
  analyzing: { thr: 0.21, morph: 1.00, worms: 1.00, speed: 1.30, light: 1.10, mute: 0 },
  forming:   { thr: 0.08, morph: 0.55, worms: 0.60, speed: 0.60, light: 1.00, mute: 0 },
  ready:     { thr: 0.02, morph: 0.35, worms: 0.25, speed: 0.40, light: 0.95, mute: 0 },
  refused:   { thr: 0.12, morph: 0.30, worms: 0.10, speed: 0.30, light: 0.80, mute: 1 },
  error:     { thr: 0.26, morph: 0.40, worms: 0.05, speed: 0.25, light: 0.70, mute: 1 },
};

const SPRINGS = {
  thr:   { f: 0.35, z: 1.0 },
  morph: { f: 0.45, z: 0.9 },
  worms: { f: 0.60, z: 0.9 },
  speed: { f: 0.50, z: 1.0 },
  light: { f: 0.90, z: 0.8 },
  mute:  { f: 0.80, z: 1.0 },
  pulse: { f: 1.40, z: 0.7 },
};

const DEMO_SCRIPT = [
  ["idle", 2000], ["listening", 2000], ["searching", 2500],
  ["analyzing", 4000], ["forming", 2500], ["ready", 2000],
];

// --- корпоративная палитра ---------------------------------------------------
// В оригинале три оранжевых тона: cDim (0.94,0.33,0.05) → cMid (1,0.56,0.16)
// → cHot (1,0.71,0.31). Здесь та же лестница, но по красному: приглушённый
// #E31E24 → #E31E24 → #FF4147 с шагом к белому #F5F5F3 на самых горячих точках.
// Красный по светимости вдвое темнее оранжевого (0,30 против 0,55 у cDim
// оригинала), поэтому на тёмном ступени подняты: иначе решётка в покое
// исчезает. На светлом фоне всё наоборот — светлые точки тонут, поэтому
// там лестница темнее и «горячий» край упирается в чистый #E31E24.
// Переключение — как у оригинального компонента: mode = "dark" | "light".
const COL = {
  dark:  { dim: [0.86, 0.15, 0.17], mid: [1.00, 0.28, 0.30], hot: [1.00, 0.66, 0.64] },
  light: { dim: [0.52, 0.05, 0.07], mid: [0.78, 0.09, 0.11], hot: [1.00, 0.24, 0.27] },
};

function makeSpring(spec, value = 0) {
  const w = 2 * Math.PI * spec.f;
  return { x: value, v: 0, k: w * w, c: 2 * spec.z * w, target: value };
}
function stepSpring(s, dt) {
  const a = -s.k * (s.x - s.target) - s.c * s.v;
  s.v += a * dt; s.x += s.v * dt; return s.x;
}

// --- шейдеры (дословно из исходника; отличия помечены «LUKOIL») -------------
const NOISE = [
  'vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}',
  'vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}',
  'vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}',
  'vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}',
  'float snoise(vec3 v){',
  ' const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);',
  ' vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);',
  ' vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);',
  ' vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;',
  ' i=mod289(i);',
  ' vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));',
  ' float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;',
  ' vec4 j=p-49.0*floor(p*ns.z*ns.z);',
  ' vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);',
  ' vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(x)-abs(y);',
  ' vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);',
  ' vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));',
  ' vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;',
  ' vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);',
  ' vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));',
  ' p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;',
  ' vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;',
  ' return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));',
  '}'].join('\n');

const N = 2500;                    /* lattice points */
const WORMS = 7, TAIL = 14, WN = WORMS * TAIL;
const PEARL = 34, TN = WORMS * PEARL, TSTRIDE = 5;
const DUR = 3.99;                  /* reference loop length */

const VS = [
  'precision highp float;',
  'attribute vec3 a_dir;',
  'attribute vec2 a_rand;',
  'uniform mat3 u_rot;',
  'uniform float u_th, u_px, u_pass, u_thr, u_trail;',
  'uniform float u_morph, u_worms, u_light, u_mute;',       // LUKOIL: параметры состояния
  'uniform vec3 u_cDim, u_cMid, u_cHot;',                     // LUKOIL: палитра снаружи
  'uniform vec2 u_off, u_scale;',
  'uniform vec4 u_worm[' + WN + '];',
  'varying vec3 v_col;',
  'varying float v_a, v_ca, v_k;',
  NOISE,
  'void main(){',
  ' vec3 dir=a_dir;',
  ' vec2 c=vec2(cos(u_th),sin(u_th));',
  ' float n1=snoise(dir*1.30+vec3(c*0.95,0.0));',
  ' float n2=snoise(dir*2.70+vec3(0.0,c*0.80));',
  ' float n3=snoise(dir*5.60+vec3(c.y*0.62,0.0,c.x*0.62));',
  ' float ridge=1.0-abs(n2);',
  ' float disp=0.54*n1+0.44*(ridge-0.5)+0.20*n3;',
  ' float R=1.0+0.305*disp*u_morph;',                         // LUKOIL: амплитуда по состоянию
  ' float e=0.66*snoise(dir*1.45+vec3(c*1.30,0.4))+0.34*snoise(dir*3.10+vec3(0.3,c*1.05));',
  ' float alive=smoothstep(u_thr-0.05,u_thr+0.06,e+0.5);',
  ' vec3 n=u_rot*dir;',
  ' vec3 p=u_rot*(dir*R);',
  ' float persp=1.0/(1.0-0.14*p.z);',
  ' float face=smoothstep(-0.10,0.06,n.z);',
  ' float boost=0.0;',
  ' if(u_trail>0.5){',
  '  boost=a_rand.x*1.5;',
  ' }else{',
  '  for(int i=0;i<' + WN + ';i++){',
  '   vec3 d=dir-u_worm[i].xyz;',
  '   boost+=u_worm[i].w*exp(-dot(d,d)*260.0);',
  '  }',
  '  boost=min(boost,1.5);',
  ' }',
  ' boost*=u_worms;',                                          // LUKOIL: сила червей по состоянию
  ' float live=(u_trail>0.5?1.0:max(alive,min(1.0,boost*0.9)))*face;',
  ' float sz=u_px*persp*(u_trail>0.5',
  '  ?(0.72+0.55*a_rand.y)*(1.0+boost*1.50)',
  '  :(0.78+0.50*a_rand.y)*(1.0+boost*1.2));',
  ' vec3 cDim=u_cDim;',                                        // LUKOIL: было vec3(0.94,0.33,0.05)
  ' vec3 cMid=u_cMid;',                                        //         vec3(1.00,0.56,0.16)
  ' vec3 cHot=u_cHot;',                                        //         vec3(1.00,0.71,0.31)
  ' vec3 col=mix(cDim,cMid,a_rand.x*a_rand.x);',
  ' if(u_trail>0.5) col=mix(cMid,cHot,clamp(boost,0.0,1.0));',
  ' else col=mix(col,cHot,clamp(boost*1.1,0.0,1.0));',
  ' float lum=dot(col,vec3(0.299,0.587,0.114));',             // LUKOIL: отказ и ошибка — в металл
  ' col=mix(col,vec3(0.541,0.553,0.573)*(0.55+lum),u_mute);',
  ' float rim=1.0+0.12*pow(1.0-abs(n.z),4.0);',
  ' float a=(u_trail>0.5',
  '  ?(0.52+0.20*a_rand.y)*clamp(boost,0.0,1.22)',
  '  :(0.92+0.28*a_rand.y)*(0.85+0.60*min(boost,1.2)))*rim*live;',
  ' a*=u_light;',                                              // LUKOIL: яркость по состоянию
  ' if(u_pass>0.5){',
  '  sz*=4.6; a*=0.115*smoothstep(0.15,0.70,boost);',
  ' }',
  ' v_col=col; v_a=a;',
  ' v_k=(u_pass>0.5)?3.0:(u_trail>0.5?0.0:2.1);',
  ' v_ca=(u_pass>0.5)?0.0:(u_trail>0.5?0.02:clamp(2.0/max(sz,3.0),0.015,0.055));',
  ' gl_PointSize=(a<0.004)?0.0:clamp(sz,0.0,140.0);',
  ' gl_Position=vec4(p.xy*persp*u_scale+u_off,0.0,1.0);',
  '}'].join('\n');

const FS = [
  'precision mediump float;',
  'varying vec3 v_col;',
  'varying float v_a, v_ca, v_k;',
  'float sp(vec2 c,float k){float d=length(c)*2.0;',
  ' if(k<0.5) return 1.0-smoothstep(0.42,1.0,d);',
  ' return pow(max(0.0,1.0-d),k);}',
  'void main(){',
  ' vec2 q=gl_PointCoord-0.5;',
  ' float k=v_k;',
  ' vec2 o=vec2(v_ca,v_ca*0.35);',
  ' float aR=sp(q+o,k), aG=sp(q,k), aB=sp(q-o,k);',
  ' vec3 c=vec3(v_col.r*aR,v_col.g*aG,v_col.b*aB)*v_a;',
  ' float cov=clamp(max(max(aR,aG),aB)*v_a,0.0,1.0);',
  ' gl_FragColor=vec4(min(c,vec3(1.0)),cov);',
  '}'].join('\n');

function rng(s) { let a = s >>> 0; return function () { a += 0x6D2B79F5; let t = a; t = Math.imul(t ^ t >>> 15, t | 1); t ^= t + Math.imul(t ^ t >>> 7, t | 61); return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }

export function isOrbSupported() {
  try { const c = document.createElement("canvas"); return !!(c.getContext("webgl") || c.getContext("experimental-webgl")); }
  catch { return false; }
}

export function createOrb(canvas, options = {}) {
  const opts = {
    quality: "auto", interactive: true, tapToListen: false,
    reducedMotion: "auto", initialState: "idle", maxDpr: null, onState: null,
    grain: true,               // зерно поверх, как в оригинале
    mode: "auto",              // dark | light | auto — палитра под фон, как у оригинала
    ...options,
  };

  const gl = canvas.getContext("webgl", { alpha: true, antialias: false, premultipliedAlpha: true, preserveDrawingBuffer: true });
  if (!gl) throw new Error("WebGL недоступен");

  // Зерно рисуется вторым холстом поверх (mix-blend-mode: screen), как в оригинале.
  let fx = null, fc = null;
  if (opts.grain && canvas.parentElement) {
    fx = document.createElement("canvas");
    fx.setAttribute("aria-hidden", "true");
    Object.assign(fx.style, { position: "absolute", inset: "0", width: "100%", height: "100%", pointerEvents: "none", mixBlendMode: "screen", opacity: "0.4", borderRadius: "inherit" });
    const host = canvas.parentElement;
    if (getComputedStyle(host).position === "static") host.style.position = "relative";
    host.appendChild(fx);
    fc = fx.getContext("2d");
  }

  function sh(type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error("Шейдер орба не собрался: " + gl.getShaderInfoLog(s));
    return s;
  }
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("Программа орба не слинковалась: " + gl.getProgramInfoLog(prog));
  gl.useProgram(prog);

  // --- геометрия: сфера Фибоначчи, решётка читается спиральными рядами ----------
  const R0 = rng(20260812);
  const dirs = new Float32Array(N * 3), rnds = new Float32Array(N * 2);
  const GA = Math.PI * (3 - Math.sqrt(5)), SP = Math.sqrt(4 * Math.PI / N);
  for (let i = 0; i < N; i++) {
    const y = 1 - (i + 0.5) / N * 2, r = Math.sqrt(Math.max(0, 1 - y * y)), th = GA * i;
    const jx = (R0() * 2 - 1) * SP * 0.08, jy = (R0() * 2 - 1) * SP * 0.08, jz = (R0() * 2 - 1) * SP * 0.08;
    const vx = Math.cos(th) * r + jx, vy = y + jy, vz = Math.sin(th) * r + jz;
    const il = 1 / Math.hypot(vx, vy, vz);
    dirs[i * 3] = vx * il; dirs[i * 3 + 1] = vy * il; dirs[i * 3 + 2] = vz * il;
    rnds[i * 2] = R0(); rnds[i * 2 + 1] = R0();
  }
  const A = { dir: gl.getAttribLocation(prog, "a_dir"), rand: gl.getAttribLocation(prog, "a_rand") };
  gl.enableVertexAttribArray(A.dir); gl.enableVertexAttribArray(A.rand);
  const stat = (data) => { const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW); return b; };
  const bDir = stat(dirs), bRand = stat(rnds);
  function bindShell() {
    gl.bindBuffer(gl.ARRAY_BUFFER, bDir); gl.vertexAttribPointer(A.dir, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, bRand); gl.vertexAttribPointer(A.rand, 2, gl.FLOAT, false, 0, 0);
  }

  const U = {};
  ["u_rot", "u_th", "u_scale", "u_px", "u_pass", "u_thr", "u_off", "u_worm", "u_trail",
    "u_morph", "u_worms", "u_light", "u_mute", "u_cDim", "u_cMid", "u_cHot"].forEach((k) => { U[k] = gl.getUniformLocation(prog, k); });
  const schemeQuery = matchMedia("(prefers-color-scheme: dark)");
  function applyPalette() {
    const mode = opts.mode === "auto" ? (schemeQuery.matches ? "dark" : "light") : opts.mode;
    const c = COL[mode] || COL.dark;
    gl.uniform3fv(U.u_cDim, c.dim); gl.uniform3fv(U.u_cMid, c.mid); gl.uniform3fv(U.u_cHot, c.hot);
  }
  applyPalette();
  const onScheme = () => { applyPalette(); kick(); };
  schemeQuery.addEventListener?.("change", onScheme);

  // --- черви: каждый идёт по наклонённому большому кругу, петля бесшовна --------
  const W = [];
  for (let i = 0; i < WORMS; i++) {
    const cz = 0.06 + R0() * 0.86, ca0 = R0() * 6.283, cr = Math.sqrt(Math.max(0, 1 - cz * cz));
    const c = [Math.cos(ca0) * cr, Math.sin(ca0) * cr, cz];
    const t0 = Math.abs(c[1]) < 0.85 ? [0, 1, 0] : [1, 0, 0];
    const d = t0[0] * c[0] + t0[1] * c[1] + t0[2] * c[2];
    let u = [t0[0] - c[0] * d, t0[1] - c[1] * d, t0[2] - c[2] * d];
    const lu = Math.hypot(u[0], u[1], u[2]); u = [u[0] / lu, u[1] / lu, u[2] / lu];
    const v = [c[1] * u[2] - c[2] * u[1], c[2] * u[0] - c[0] * u[2], c[0] * u[1] - c[1] * u[0]];
    const rho = 0.46 + R0() * 0.42, sr = Math.sin(rho), crho = Math.cos(rho);
    W.push({ c, u, v, sr, cr: crho, m: 1 + Math.floor(R0() * 2), ph: R0() * 6.283, str: 0.90 + R0() * 0.28, arc: (0.78 + R0() * 0.34) / sr, fl: R0() * 6.283 });
  }
  function onPath(w, ang, out) {
    const ca = Math.cos(ang), sa = Math.sin(ang);
    out[0] = w.c[0] * w.cr + (w.u[0] * ca + w.v[0] * sa) * w.sr;
    out[1] = w.c[1] * w.cr + (w.u[1] * ca + w.v[1] * sa) * w.sr;
    out[2] = w.c[2] * w.cr + (w.u[2] * ca + w.v[2] * sa) * w.sr;
  }
  const tmpA = [0, 0, 0], tmpB = [0, 0, 0];
  const wormPos = new Float32Array(WN * 4);
  function worms(th) {
    for (let k = 0; k < WORMS; k++) {
      const w = W[k], head = th * w.m + w.ph;
      const flick = 0.76 + 0.24 * Math.sin(th * 2.0 + w.fl);
      for (let j = 0; j < TAIL; j++) {
        const o = k * TAIL + j;
        onPath(w, head - j * (w.arc / TAIL), tmpA);
        wormPos[o * 4] = tmpA[0]; wormPos[o * 4 + 1] = tmpA[1]; wormPos[o * 4 + 2] = tmpA[2];
        wormPos[o * 4 + 3] = w.str * flick * (0.42 + 0.58 * Math.pow(1 - j / TAIL, 0.7)) * (0.80 + 0.20 * Math.sin(j * 1.7 + head * 2.0));
      }
    }
  }
  const pearls = new Float32Array(TN * TSTRIDE), pearlSeed = new Float32Array(TN);
  for (let i = 0; i < TN; i++) pearlSeed[i] = R0();
  const bTrail = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, bTrail);
  gl.bufferData(gl.ARRAY_BUFFER, pearls.byteLength, gl.DYNAMIC_DRAW);
  function bindTrail() {
    gl.bindBuffer(gl.ARRAY_BUFFER, bTrail);
    gl.vertexAttribPointer(A.dir, 3, gl.FLOAT, false, TSTRIDE * 4, 0);
    gl.vertexAttribPointer(A.rand, 2, gl.FLOAT, false, TSTRIDE * 4, 12);
  }
  function rope(th) {
    for (let k = 0; k < WORMS; k++) {
      const w = W[k], head = th * w.m + w.ph;
      const flick = 0.86 + 0.14 * Math.sin(th * 2.0 + w.fl);
      const pstep = w.arc / PEARL;
      for (let j = 0; j < PEARL; j++) {
        const ang = head - j * pstep;
        const o = k * PEARL + j, b = o * TSTRIDE, u = j / PEARL;
        const wob = 0.055 * Math.sin(j * 0.42 + head * 2.0 + w.fl) + 0.030 * Math.sin(j * 0.17 - head);
        onPath(w, ang + wob, tmpB);
        pearls[b] = tmpB[0]; pearls[b + 1] = tmpB[1]; pearls[b + 2] = tmpB[2];
        pearls[b + 3] = w.str * flick * (0.34 + 0.66 * Math.pow(1 - u, 0.55)) * (0.74 + 0.26 * Math.sin(j * 1.15 + head * 2.0 + w.fl));
        pearls[b + 4] = pearlSeed[o];
      }
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, bTrail);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, pearls);
  }
  function rot(th) {
    const ax = 0.22 * Math.sin(th) + 0.06 * Math.sin(th * 2 + 1.1);
    const ay = 0.30 * Math.sin(th + 2.2) + 0.08 * Math.cos(th * 2);
    const az = 0.10 * Math.cos(th + 0.6);
    const cx = Math.cos(ax), sx = Math.sin(ax), cy = Math.cos(ay), sy = Math.sin(ay), cz = Math.cos(az), sz = Math.sin(az);
    const m00 = cz * cy, m01 = cz * sy * sx - sz * cx, m02 = cz * sy * cx + sz * sx;
    const m10 = sz * cy, m11 = sz * sy * sx + cz * cx, m12 = sz * sy * cx - cz * sx;
    const m20 = -sy, m21 = cy * sx, m22 = cy * cx;
    return new Float32Array([m00, m10, m20, m01, m11, m21, m02, m12, m22]);
  }

  // --- зерно (LUKOIL: тонировано в красный, было n, n*0.74, n*0.52) ----------
  const TILES = [];
  for (let t = 0; t < 5; t++) {
    const c = document.createElement("canvas"); c.width = c.height = 170;
    const g = c.getContext("2d"), im = g.createImageData(170, 170), d = im.data;
    for (let i = 0; i < d.length; i += 4) { const v = R0(), n = v * v * 255; d[i] = n; d[i + 1] = n * 0.36; d[i + 2] = n * 0.36; d[i + 3] = 255; }
    g.putImageData(im, 0, 0); TILES.push(c);
  }
  let pats = null;
  function grain(frame) {
    if (!fc || !fx.width) return;
    // screen-наложение на светлом фоне ничего не даёт, кроме мутного налёта
    const dark = opts.mode === "auto" ? schemeQuery.matches : opts.mode === "dark";
    if (!dark) { fc.clearRect(0, 0, fx.width, fx.height); return; }
    if (!pats) pats = TILES.map((t) => fc.createPattern(t, "repeat"));
    fc.setTransform(1, 0, 0, 1, 0, 0);
    fc.clearRect(0, 0, fx.width, fx.height);
    fc.globalAlpha = 0.11;
    fc.fillStyle = pats[frame % pats.length];
    fc.setTransform(1, 0, 0, 1, (frame * 37) % 170, (frame * 61) % 170);
    fc.fillRect(-170, -170, fx.width + 340, fx.height + 340);
    fc.setTransform(1, 0, 0, 1, 0, 0);
  }

  // --- состояние ----------------------------------------------------------------
  const isMobile = /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && matchMedia("(pointer: coarse)").matches);
  const quality = opts.quality === "auto" ? (isMobile ? "low" : "high") : opts.quality;
  const dprCap = opts.maxDpr || (quality === "low" ? 1.5 : 2);
  const rmQuery = matchMedia("(prefers-reduced-motion: reduce)");
  let reduced = opts.reducedMotion === "auto" ? rmQuery.matches : !!opts.reducedMotion;
  const onRm = () => { if (opts.reducedMotion === "auto") reduced = rmQuery.matches; kick(); };
  rmQuery.addEventListener?.("change", onRm);

  const springs = {};
  const start = TARGETS[opts.initialState] || TARGETS.idle;
  for (const k of Object.keys(SPRINGS)) springs[k] = makeSpring(SPRINGS[k], start[k] ?? 0);
  let state = TARGETS[opts.initialState] ? opts.initialState : "idle";
  let hover = 0, pointer = { x: 0, y: 0 };

  function applyTargets() {
    const t = TARGETS[state];
    for (const k of Object.keys(t)) springs[k].target = t[k];
    springs.light.target += 0.12 * hover;
  }
  function setState(next) {
    if (!TARGETS[next]) throw new Error(`Неизвестное состояние орба: ${next}`);
    if (next === state) return;
    const prev = state; state = next;
    if (next === "ready") { springs.pulse.x = 1; springs.pulse.v = 0; }   // короткая вспышка
    applyTargets();
    opts.onState?.(next, prev);
    if (reduced) render(at);
  }

  // указатель: лёгкий сдвиг сцены, не слежение
  function onMove(e) {
    const r = canvas.getBoundingClientRect();
    pointer = { x: Math.max(-1, Math.min(1, ((e.clientX - r.left) / r.width) * 2 - 1)), y: Math.max(-1, Math.min(1, -(((e.clientY - r.top) / r.height) * 2 - 1))) };
    hover = 1;
  }
  function onLeave() { pointer = { x: 0, y: 0 }; hover = 0; }
  function onClick() { if (opts.tapToListen && state === "idle") setState("listening"); }
  if (opts.interactive) {
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerleave", onLeave);
    canvas.addEventListener("click", onClick);
  }

  // --- размер и цикл --------------------------------------------------------------
  let VW = 0, VH = 0, size = 0, aspX = 1, aspY = 1;
  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, dprCap);
    VW = Math.max(1, Math.round(r.width * dpr)); VH = Math.max(1, Math.round(r.height * dpr));
    if (canvas.width !== VW || canvas.height !== VH) {
      canvas.width = VW; canvas.height = VH;
      if (fx) { fx.width = VW; fx.height = VH; pats = null; }
    }
    size = Math.min(VW, VH);
    aspX = VW > VH ? VH / VW : 1; aspY = VH > VW ? VW / VH : 1;
    gl.viewport(0, 0, VW, VH);
  }
  const ro = new ResizeObserver(() => { resize(); if (reduced) render(at); });
  ro.observe(canvas); resize();

  let th = 0, at = 1.2, frameNo = 0, smoothPx = 0, smoothPy = 0;
  function render(t) {
    const S = springs;
    worms(th); rope(th);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.uniformMatrix3fv(U.u_rot, false, rot(th));
    gl.uniform1f(U.u_th, th);
    gl.uniform2f(U.u_scale, 0.672 * aspX, 0.672 * aspY);
    // Оригинал рассчитан на мастер 1080: на 120–240 px точки вырождались
    // в пыль. Ниже 400 px растим их в полтора раза и держим пол в 1,5 px.
    gl.uniform1f(U.u_px, Math.max(1.5, size / 1080 * 8.0) * (size < 400 ? 1.5 : 1.0));
    gl.uniform1f(U.u_thr, S.thr.x + 0.04 * Math.sin(th * 2 + 0.8));           // оригинал: 0.19 + 0.04·sin
    gl.uniform2f(U.u_off, 0.006 * Math.sin(th + 1.0) + smoothPx * 0.02, -0.012 * Math.cos(th) + smoothPy * 0.02);
    gl.uniform4fv(U.u_worm, wormPos);
    gl.uniform1f(U.u_morph, S.morph.x);
    gl.uniform1f(U.u_worms, S.worms.x);
    gl.uniform1f(U.u_light, S.light.x * (1 + 0.5 * S.pulse.x));
    gl.uniform1f(U.u_mute, S.mute.x);

    bindShell();
    gl.uniform1f(U.u_trail, 0); gl.uniform1f(U.u_pass, 0);
    gl.drawArrays(gl.POINTS, 0, N);

    bindTrail();
    gl.uniform1f(U.u_trail, 1); gl.uniform1f(U.u_pass, 1);
    gl.drawArrays(gl.POINTS, 0, TN);
    gl.uniform1f(U.u_pass, 0);
    gl.drawArrays(gl.POINTS, 0, TN);
    grain(frameNo++);
  }

  let raf = 0, last = performance.now() / 1000, running = true, visible = true, inView = true;
  let frames = 0, fpsWindowStart = last, fps = 0;
  const io = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => { inView = entries[0]?.isIntersecting !== false; kick(); }, { threshold: 0 }) : null;
  io?.observe(canvas);
  const onVis = () => { visible = !document.hidden; kick(); };
  document.addEventListener("visibilitychange", onVis);

  function tick(nowMs) {
    raf = 0;
    if (!running || !visible || !inView) return;
    const now = nowMs / 1000;
    const dt = Math.min(0.05, Math.max(0.001, now - last)); last = now;
    applyTargets();
    for (const k of Object.keys(springs)) stepSpring(springs[k], dt);
    springs.pulse.target = 0;
    smoothPx += (pointer.x - smoothPx) * Math.min(1, dt * 4);
    smoothPy += (pointer.y - smoothPy) * Math.min(1, dt * 4);
    // Петля идёт со скоростью состояния: в анализе быстрее, в покое медленнее.
    // При отключённой анимации стоит на месте — как в оригинале (seek 1.2 с).
    if (!reduced) { at += dt * springs.speed.x; th = 2 * Math.PI * (at / DUR); render(at); }
    else if (frameNo === 0) { th = 2 * Math.PI * (1.2 / DUR); render(1.2); }
    frames++;
    if (now - fpsWindowStart >= 1) { fps = frames / (now - fpsWindowStart); frames = 0; fpsWindowStart = now; }
    if (!reduced) raf = requestAnimationFrame(tick);
  }
  function kick() { if (!raf && running && visible && inView) { last = performance.now() / 1000; raf = requestAnimationFrame(tick); } }
  kick();

  let demoTimer = 0, demoIndex = -1;
  const demo = {
    running: false,
    start() {
      if (demo.running) return;
      demo.running = true; demoIndex = -1;
      const step = () => { demoIndex = (demoIndex + 1) % DEMO_SCRIPT.length; const [name, ms] = DEMO_SCRIPT[demoIndex]; setState(name); demoTimer = setTimeout(step, ms); };
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
    setReducedMotion(v) { opts.reducedMotion = v; reduced = v === "auto" ? rmQuery.matches : !!v; frameNo = 0; kick(); },
    demo,
    pause() { running = false; },
    resume() { running = true; kick(); },
    destroy() {
      running = false; demo.stop();
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect(); io?.disconnect();
      document.removeEventListener("visibilitychange", onVis);
      rmQuery.removeEventListener?.("change", onRm);
      schemeQuery.removeEventListener?.("change", onScheme);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("click", onClick);
      fx?.remove();
      gl.deleteProgram(prog); gl.deleteBuffer(bDir); gl.deleteBuffer(bRand); gl.deleteBuffer(bTrail);
      gl.getExtension("WEBGL_lose_context")?.loseContext();
    },
  };
}
