// Шейдер орба «Мобильного аналитика».
//
// Один полноэкранный квадрат, вся геометрия — SDF во fragment-шейдере.
// Единицы: радиус внешней кромки орба = 1. Пропорции сняты с референс-пака
// (mobile_analyst_svg_reference_pack, 1024×1024, внешняя кромка 389 px):
//   ядро            r = 0.63   (245/389)
//   полоса сегментов r = 0.73…1.00
//   столбцы         x = −0.165, 0, +0.165; низ y = −0.35; высоты 0.30/0.47/0.68
//
// Ничего не вращается. Сегменты дрейфуют на градусы и дробятся шумом,
// полости открываются порогом по fbm, филаменты текут вдоль своих кривых.
// Все «часы» (uFlowT, uErodeT, uWaveT) приходят из JS и идут с разной
// скоростью в разных состояниях — так движение получает инерцию.

export const ORB_VERT = `#version 300 es
in vec2 aPos;
void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }
`;

export const ORB_FRAG = `#version 300 es
precision highp float;

uniform vec2  uRes;
uniform float uTime;
uniform float uFlowT;
uniform float uErodeT;
uniform float uWaveT;
uniform vec2  uPointer;
uniform float uQuality;   // 1 — десктоп (4 октавы), 0 — телефон (2 октавы)

uniform float uScale;     // статичный масштаб состояния (присутствие)
uniform float uBreath;    // дыхание
uniform float uWave;      // волна по поверхности (слушает)
uniform float uFlow;      // филаменты данных (ищет)
uniform float uErode;     // эрозия: полости и вложенные кольца (анализирует)
uniform float uOrder;     // упорядочивание: полости закрываются, филаменты сходятся (формирует)
uniform float uLight;     // внутренний красный свет
uniform float uBars;      // столбцы как waveform
uniform float uLevel;     // столбцы на одном уровне (отказ)
uniform float uPulse;     // импульс готовности, фаза 0..1
uniform float uMute;      // приглушение (отказ, ошибка)
uniform float uSlip;      // сегмент сбился (ошибка)

out vec4 fragColor;

const float PI = 3.14159265;
const vec3 RED    = vec3(0.890, 0.118, 0.141);   // #E31E24
const vec3 RED_HI = vec3(1.000, 0.255, 0.278);   // #FF4147
const vec3 RED_LO = vec3(0.447, 0.027, 0.043);   // #72070B
const vec3 WHITE  = vec3(0.961, 0.961, 0.953);   // #F5F5F3
const vec3 SMOKE  = vec3(0.035, 0.039, 0.047);   // #090A0C
const vec3 GRAPH  = vec3(0.090, 0.098, 0.114);   // #17191D
const vec3 METAL  = vec3(0.541, 0.553, 0.573);   // #8A8D92

const float R_CORE = 0.63;
const float R_IN   = 0.73;
const float R_OUT  = 1.00;

// --- шум -------------------------------------------------------------------
vec2 hash2(vec2 p){
  p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
  return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
}
float gnoise(vec2 p){
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(dot(hash2(i), f), dot(hash2(i + vec2(1, 0)), f - vec2(1, 0)), u.x),
             mix(dot(hash2(i + vec2(0, 1)), f - vec2(0, 1)), dot(hash2(i + vec2(1, 1)), f - vec2(1, 1)), u.x), u.y);
}
float fbm(vec2 p){
  int oct = uQuality > 0.5 ? 4 : 2;
  float a = 0.5, s = 0.0;
  mat2 m = mat2(1.6, 1.2, -1.2, 1.6);
  for (int i = 0; i < 4; i++){ if (i >= oct) break; s += a * gnoise(p); p = m * p; a *= 0.5; }
  return s;   // примерно −0.6…0.6
}
float n01(float x){ return 0.5 + 0.5 * x; }

// --- SDF ---------------------------------------------------------------------
float sdRoundBox(vec2 p, vec2 b, float r){
  vec2 q = abs(p) - b + r;
  return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}
float sdSegment(vec2 p, vec2 a, vec2 b){
  vec2 pa = p - a, ba = b - a;
  float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
  return length(pa - ba * h);
}
float angDiff(float a, float b){ float d = a - b; return abs(mod(d + PI, 2.0 * PI) - PI); }

// «over» в премультиплицированном виде
void over(inout vec3 col, inout float a, vec3 sc, float sa){
  col = sc * sa + col * (1.0 - sa);
  a   = sa + a * (1.0 - sa);
}

// --- сегменты оболочки -------------------------------------------------------
// Дуга — настоящий SDF с круглыми торцами (как stroke-linecap: round
// в референсе), а не «угол × радиус» с плоскими срезами. Возвращает
// расстояние до тела дуги; отрицательное — внутри.
float sdArc(vec2 p, float c, float hw, float ra, float rb){
  // поворачиваем так, чтобы центр дуги смотрел вдоль +y
  float a = c - PI * 0.5;
  vec2 q = vec2(cos(-a) * p.x - sin(-a) * p.y, sin(-a) * p.x + cos(-a) * p.y);
  q.x = abs(q.x);
  vec2 sc = vec2(sin(hw), cos(hw));
  return ((sc.y * q.x > sc.x * q.y) ? length(q - sc * ra) : abs(length(q) - ra)) - rb;
}
// Дробление по углу: при frag = 0 дуга цельная, при 1 — распадается на осколки.
// Порог идёт по угловому шуму, шум медленно течёт — осколки перетекают,
// но ничего не вращается.
float dashMask(float th, float frag, float seed){
  float dash = n01(gnoise(vec2(th * 2.6 + seed * 3.1, uErodeT * 0.30 + seed)));
  float thr = 0.12 + 0.42 * frag;
  return smoothstep(thr, thr + 0.08, dash + 0.35 * (1.0 - frag));
}
// Стекло дуги: тело светлее к оси, тёмный кант, блик у внешнего ребра,
// продольный спад света от одного торца к другому (в референсе — linearGradient).
vec3 arcShade(float d, float rb, float radial, float along, float tone){
  float t = clamp(1.0 + d / rb, 0.0, 1.0);        // 1 на оси, 0 у края
  float body = pow(t, 0.9);
  vec3 c = mix(RED_LO, RED, body) * (0.85 + 0.25 * uLight);
  float hi = exp(-pow((radial - 0.55) / 0.16, 2.0)) * 0.55;
  c = mix(c, RED_HI, hi * body);
  c *= 0.62 + 0.38 * along;
  return mix(c * 0.5, c, tone);
}

void main(){
  float minSide = min(uRes.x, uRes.y);
  float px = 1.15 / (0.5 * minSide) * 1.2;           // толщина пикселя в единицах орба
  vec2 p = (gl_FragCoord.xy - 0.5 * uRes) / (0.5 * minSide) * 1.15;

  // дыхание — масштаб координат
  float breath = uScale * (1.0 + 0.012 * sin(uTime * 2.0 * PI / 5.5) * uBreath);
  p /= breath;

  float r  = length(p);
  float th = atan(p.y, p.x);

  vec3  col = vec3(0.0);
  float alpha = 0.0;

  // Параллакс: внутреннее содержимое уезжает на 3% от указателя,
  // оболочка стоит — так получается ощущение глубины, а не слежение.
  vec2 pin = p - uPointer * 0.03;

  // ------------------------------------------------------------------ ядро
  // Волна по поверхности: радиус кромки слегка плывёт по углу
  float ripple = (0.012 * sin(th * 7.0 - uWaveT * 3.0) + 0.008 * sin(th * 11.0 + uWaveT * 2.2)) * uWave
               + 0.004 * gnoise(p * 4.0 + uTime * 0.15) * uBreath;
  float rc = R_CORE * (1.0 + ripple);
  float coreCov = 1.0 - smoothstep(rc - px, rc + px, r);

  if (coreCov > 0.0) {
    vec2 q = pin / rc;
    float q2 = min(dot(q, q), 1.0);
    vec3 n = vec3(q, sqrt(1.0 - q2));
    vec3 L = normalize(vec3(-0.45 + uPointer.x * 0.35, 0.55 + uPointer.y * 0.35, 0.72));
    float diff = 0.5 + 0.5 * dot(n, L);
    vec3 base = mix(SMOKE, GRAPH * 1.7, diff);
    // френель: полированный металлический кант
    float fres = pow(1.0 - n.z, 2.6);
    base = mix(base, METAL * 0.55, fres * 0.55);
    // блик стекла: широкий мягкий + узкий острый, оба сверху-слева
    vec3 h = normalize(L + vec3(0.0, 0.0, 1.0));
    float ndh = max(dot(n, h), 0.0);
    base += WHITE * (pow(ndh, 14.0) * 0.06 + pow(ndh, 90.0) * 0.20);
    // второй свет снизу-справа — так стекло читается как объём, а не как диск
    vec3 L2 = normalize(vec3(0.5, -0.6, 0.45));
    base += METAL * 0.10 * pow(max(dot(n, L2), 0.0), 3.0);
    // тонкий металлический кант по контуру (в референсе — stroke #292C31)
    float rimLine = smoothstep(0.90, 0.985, sqrt(q2)) * (1.0 - smoothstep(0.985, 1.0, sqrt(q2)));
    base = mix(base, METAL * 0.42, rimLine * 0.6);

    // внутренний красный свет: источник снизу, плюс общее свечение к центру
    float light = uLight * (1.0 - 0.6 * uMute);
    float glowLo = exp(-dot(pin - vec2(0.0, -0.22), pin - vec2(0.0, -0.22)) / 0.16);
    float glowC  = (1.0 - q2) * 0.28;
    // едва заметное течение света в покое
    float drift = 1.0 + 0.10 * gnoise(pin * 2.0 + uTime * 0.12);
    base += RED * (0.55 * glowLo + glowC) * light * drift;

    // ---------------------------------------------------------- эрозия
    // Recursive erosion: поверхность стекла снимается слоями. Где fbm
    // выше порога — полость, под ней слой глубже (темнее, со своим шумом,
    // со своим параллаксом). Внутри первой полости той же логикой
    // открывается вторая, внутри второй — третья. Кромка каждой полости —
    // тонкая красная линия, как свет на срезе стекла. Красная «сеть»
    // видна только на самой глубине и слабо: это энергия внутри материала,
    // а не молнии поверх него. Силуэт держится: к кромке ядра эрозия
    // сходит на нет.
    float erode = uErode;
    if (erode > 0.002) {
      float keep = 1.0 - smoothstep(0.74, 0.98, sqrt(q2));
      float e = erode * keep;
      float thr = 1.0 - e * 0.58 + uOrder * 0.55;
      // параллакс глубины: каждый слой чуть сильнее уезжает за указателем
      vec2 par = uPointer * 0.02;
      vec2 e1 = (pin - par * 1.0) * 2.1 + vec2(uErodeT * 0.11, -uErodeT * 0.07);
      vec2 e2 = (pin - par * 2.2) * 4.3 + vec2(-uErodeT * 0.17, uErodeT * 0.13) + 7.3;
      vec2 e3 = (pin - par * 3.4) * 8.0 + vec2(uErodeT * 0.23, uErodeT * 0.19) + 19.1;
      float n1 = n01(fbm(e1));
      float n2 = n01(fbm(e2));
      float n3 = n01(gnoise(e3) * 0.7);
      float soft = 0.05;
      float cav1 = smoothstep(thr, thr + soft, n1);
      float cav2 = smoothstep(thr + 0.06, thr + 0.06 + soft, n2) * cav1;
      float cav3 = smoothstep(thr + 0.10, thr + 0.10 + soft, n3) * cav2;
      // кромки: узкие изолинии у порога, только на своём слое
      float rim1 = exp(-abs(n1 - thr) / 0.020) * e;
      float rim2 = exp(-abs(n2 - thr - 0.06) / 0.018) * cav1 * e;
      float rim3 = exp(-abs(n3 - thr - 0.10) / 0.016) * cav2 * e;
      // слои материала: глубже — темнее, свет внутри — краснее
      vec3 inner1 = SMOKE * 1.15 * (0.85 + 0.15 * diff) + RED_LO * 0.12 * light;
      vec3 inner2 = SMOKE * 0.8 + RED_LO * 0.3 * light;
      vec3 inner3 = SMOKE * 0.55 + RED_LO * 0.6 * light;
      // сеть энергии на глубине: гребни шума, тонко и тускло
      float veins = pow(1.0 - abs(gnoise(pin * 9.0 + uErodeT * 0.2)), 8.0) * cav2 * 0.16 * light;
      base = mix(base, inner1, cav1);
      base = mix(base, inner2, cav2);
      base = mix(base, inner3, cav3);
      base += RED * veins;
      base += RED * (rim1 * 0.50 + rim2 * 0.40 + rim3 * 0.30) * (0.6 + 0.4 * light);
      base += RED_HI * rim3 * 0.15;
      // объём: у стенки полости лёгкая тень на верхнем слое
      base *= 1.0 - 0.25 * smoothstep(thr - 0.06, thr, n1) * (1.0 - cav1) * e;
    }

    // ------------------------------------------------------- филаменты
    // Три кривые слева направо (03), сеть в анализе, три луча к ядру в формировании (05).
    float flow = uFlow;
    if (flow > 0.002) {
      float fil = 0.0;
      float amp = 0.09 * (1.0 - uOrder);
      for (int i = 0; i < 3; i++){
        float fi = float(i);
        float yc = 0.30 - 0.30 * fi;
        float ph = fi * 2.1;
        float wob = gnoise(vec2(pin.x * 2.0 + uFlowT * 0.3, fi * 3.7)) * 0.05 * (1.0 - uOrder);
        float y = pin.y - yc - amp * sin(pin.x * 5.5 + ph + uFlowT * 1.3) - wob;
        float w = 0.012 - 0.003 * fi;
        float line = exp(-(y * y) / (2.0 * w * w));
        // бегущий импульс вдоль кривой
        float run = fract(pin.x * 0.55 - uFlowT * 0.45 + fi * 0.33);
        float spark = smoothstep(0.0, 0.25, run) * (1.0 - smoothstep(0.25, 0.55, run));
        fil += line * (0.45 + 0.55 * spark) * (1.0 - 0.18 * fi);
      }
      // сеть в анализе: две диагонали
      float net = 0.0;
      if (uErode > 0.01) {
        for (int k = 0; k < 2; k++){
          float fk = float(k);
          vec2 d = normalize(vec2(1.0, fk * 2.0 - 1.0));
          float s = dot(pin, d);
          float t = dot(pin, vec2(-d.y, d.x));
          float y = t - 0.06 * sin(s * 7.0 + fk * 1.7 + uFlowT * 1.1);
          net += exp(-(y * y) / (2.0 * 0.007 * 0.007)) * 0.5;
        }
        net *= uErode * (1.0 - uOrder) * 0.45;
      }
      // лучи к ядру при формировании
      float rays = 0.0;
      if (uOrder > 0.01) {
        float d1 = sdSegment(pin, vec2(-0.62, 0.0), vec2(-0.24, 0.0));
        float d2 = sdSegment(pin, vec2( 0.62, 0.0), vec2( 0.26, 0.0));
        float d3 = sdSegment(pin, vec2( 0.0, 0.62), vec2( 0.0, 0.30));
        float dm = min(d1, min(d2, d3));
        rays = exp(-(dm * dm) / (2.0 * 0.010 * 0.010)) * uOrder;
        // импульсы стягиваются к центру
        float pulseIn = fract(-uFlowT * 0.5);
        float rr = length(pin);
        rays *= 0.55 + 0.45 * smoothstep(0.08, 0.0, abs(rr - mix(0.62, 0.28, pulseIn)));
      }
      float f = (fil * (1.0 - uOrder) * (1.0 - 0.45 * uErode) + net + rays) * flow;
      base += RED * f * 0.9 + RED_HI * f * f * 0.5;
    }

    // волна «слушаю»: мягкая горизонтальная волна через ядро (02)
    if (uWave > 0.002) {
      float y = pin.y - 0.04 - 0.09 * sin(pin.x * 6.0 + uWaveT * 2.5);
      float band = exp(-(y * y) / (2.0 * 0.03 * 0.03));
      base += RED * band * 0.22 * uWave;
    }

    over(col, alpha, base, coreCov);
  }

  // -------------------------------------------------------- сегменты оболочки
  // Композиция из референса: яркий сегмент справа, тёмный слева-снизу, тонкий сверху-слева.
  // Дрейф на ±4°, не вращение. В анализе дуги дробятся и появляются вложенные полосы.
  float driftA = 0.06 * sin(uTime * 0.21) * (0.3 + uWave);
  float frag = clamp(uErode * 1.1 - uOrder * 0.6, 0.0, 1.0);
  float tone = 1.0 - 0.55 * uMute;
  float rMid = 0.5 * (R_IN + R_OUT), rHalf = 0.5 * (R_OUT - R_IN);

  // сегмент 1 — яркий, справа
  {
    float c = 0.10 + driftA, hw = 0.93;
    float d = sdArc(p, c, hw, rMid, rHalf);
    float cov = (1.0 - smoothstep(-px, px, d)) * dashMask(th, frag, 1.0);
    float radial = clamp((r - R_IN) / (R_OUT - R_IN), 0.0, 1.0);
    float along = 0.5 + 0.5 * cos(th - c + hw * 0.6);
    over(col, alpha, arcShade(d, rHalf, radial, along, 1.0) * tone, cov);
  }
  // сегмент 2 — тёмный, слева-снизу (сдвигается при ошибке)
  {
    float c = -2.25 - driftA * 0.7 + uSlip * 0.42, hw = 0.74;
    float d = sdArc(p, c, hw, rMid, rHalf);
    float cov = (1.0 - smoothstep(-px, px, d)) * dashMask(th, frag, 2.0);
    float radial = clamp((r - R_IN) / (R_OUT - R_IN), 0.0, 1.0);
    float along = 0.5 + 0.5 * cos(th - c - hw * 0.5);
    over(col, alpha, arcShade(d, rHalf, radial, along, 0.40) * tone, cov);
  }
  // сегмент 3 — тонкий, сверху-слева
  {
    float c = 2.35 + driftA * 0.5, hw = 0.50, ra = R_IN + 0.02, rb = 0.02;
    float d = sdArc(p, c, hw, ra, rb);
    float cov = (1.0 - smoothstep(-px, px, d)) * dashMask(th, frag * 0.5, 3.0);
    float along = 0.5 + 0.5 * cos(th - c + hw * 0.4);
    over(col, alpha, arcShade(d, rb, 0.5, along, 0.85) * tone, cov);
  }

  // вложенные кольца эрозии (04): осколки тем мельче, чем глубже; закрываются при формировании
  if (uErode > 0.01) {
    float e = uErode * (1.0 - 0.7 * uOrder);
    float d1 = abs(r - 0.69) - 0.024;
    float d2 = abs(r - 0.645) - 0.028;
    float d3 = abs(r - 0.548) - 0.017;
    float k1 = (1.0 - smoothstep(-px, px, d1)) * dashMask(th * 1.3 + 0.7, 0.80, 4.0) * e;
    float k2 = (1.0 - smoothstep(-px, px, d2)) * dashMask(th * 1.7 + 2.1, 0.88, 5.0) * e;
    float k3 = (1.0 - smoothstep(-px, px, d3)) * dashMask(th * 2.2 + 4.0, 0.92, 6.0) * e;
    over(col, alpha, mix(RED_LO, RED, 0.75) * tone, k1 * 0.7);
    over(col, alpha, RED * tone, k2 * 0.45);
    over(col, alpha, RED_LO * 1.2 * tone, k3 * 0.6);
  }

  // мягкий ореол от сегментов наружу и внутрь — свет стекла, не блум
  {
    float a1 = 1.0 - smoothstep(0.80, 1.05, angDiff(th, 0.10 + driftA));
    float a2 = (1.0 - smoothstep(0.62, 0.85, angDiff(th, -2.25))) * 0.35;
    float ang = max(a1, a2);
    float haloA = ang * exp(-max(r - R_OUT, 0.0) / 0.09) * step(R_OUT, r) * 0.26
                + ang * exp(-max(R_IN - r, 0.0) / 0.05) * step(r, R_IN) * (1.0 - coreCov) * 0.40;
    haloA *= (0.6 + 0.5 * uLight) * tone * (1.0 - 0.2 * frag);
    over(col, alpha, RED, haloA);
  }

  // -------------------------------------------------------------- импульс
  if (uPulse > 0.001 && uPulse < 0.999) {
    float rp = mix(0.50, 1.10, uPulse);
    float w  = 0.018 + 0.02 * uPulse;
    float ring = exp(-(r - rp) * (r - rp) / (2.0 * w * w));
    float k = (1.0 - uPulse) * (1.0 - uPulse);
    vec3 pc = mix(WHITE, RED, smoothstep(0.15, 0.75, uPulse));
    over(col, alpha, pc, ring * k * 1.1);
  }

  // -------------------------------------------------------------- столбцы
  {
    float barA = 0.0;
    vec3 barC = vec3(0.0);
    for (int i = 0; i < 3; i++){
      float fi = float(i);
      float xc = -0.165 + 0.165 * fi;
      float hBase = fi < 0.5 ? 0.303 : (fi < 1.5 ? 0.470 : 0.684);   // из референса
      // waveform (слушает): дышат в противофазе; анализ: медленно меняют высоту
      float wf = 1.0 + uBars * 0.26 * sin(uWaveT * 2.6 + fi * 1.9 + sin(uWaveT * 0.8) * 0.6)
                     + uErode * 0.10 * sin(uErodeT * 0.9 + fi * 2.3) * (1.0 - uOrder);
      float h = mix(hBase * wf, 0.303, uLevel);
      vec2 bp = pin - vec2(xc, -0.35 + h * 0.5);
      float d = sdRoundBox(bp, vec2(0.059, h * 0.5), 0.036);
      float cov = 1.0 - smoothstep(-px, px, d);
      // вертикальный градиент как в референсе: белый сверху, чуть серее снизу
      float ty = clamp((bp.y + h * 0.5) / max(h, 1e-3), 0.0, 1.0);
      vec3 bc = mix(METAL * 1.45, WHITE, 0.35 + 0.65 * ty);
      bc = mix(bc, METAL, uMute * 0.7);
      barA = max(barA, cov);
      barC = mix(barC, bc, cov);
      // мягкое свечение вокруг столбцов — белое, короткое
      float glow = exp(-max(d, 0.0) / 0.035) * (1.0 - cov) * 0.10 * (0.5 + uLight);
      over(col, alpha, mix(WHITE, RED, 0.35), glow * (1.0 - uMute));
    }
    over(col, alpha, barC, barA);
  }

  // приглушение для отказа и ошибки: убираем цвет, не яркость столбцов
  float lum = dot(col, vec3(0.299, 0.587, 0.114));
  col = mix(col, vec3(lum) * 0.92, uMute * 0.55);

  fragColor = vec4(col, alpha);
}
`;
