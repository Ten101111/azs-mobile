// Ответ аналитического агента: структура «главный вывод → что произошло →
// почему → где → что делать → ограничения», графики на SVG без библиотек,
// таблицы результатов и свёртка «Показать расчёты».
//
// Числа на графиках приходят с бэкенда уже привязанными к результатам
// запросов (create_chart берёт их из rN/pN), здесь они только рисуются.
// Компоненты не знают о модели и не считают ничего сами: любой расчёт
// в интерфейсе был бы вторым источником правды.
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, X, AlertTriangle, Maximize2, Download } from "lucide-react";

// Уровни глубины (ИИ-20, 23.09.2026). Запасной список — если сервер не
// прислал свой; настоящие подписи и ориентир времени приходят в /api/ai/status.
export const AI_DEPTH_FALLBACK = [
  { code: "auto", title: "Авто", hint: "уровень выбирает ИИ по вопросу", about: "", typical: "" },
  { code: "fast", title: "Лёгкий", hint: "одна цифра или факт", about: "", typical: "" },
  { code: "analyze", title: "Средний", hint: "сравнения и динамика", about: "", typical: "" },
  { code: "deep", title: "Высокий", hint: "причины, отклонения, сценарии", about: "", typical: "" },
];

export const DEPTH_TITLES = { fast: "Лёгкий", analyze: "Средний", deep: "Высокий" };
export const TASK_TITLES = {
  lookup: "факт", compare: "сравнение", trend: "динамика", diagnose: "диагностика причин",
  anomaly: "поиск аномалий", opportunity: "точки роста", whatif: "сценарий", other: "разбор",
};

const SERIES_COLORS = ["var(--text)", "var(--red)", "var(--muted)", "var(--line-strong)", "var(--amber)", "var(--green)"];

// --- форматирование ----------------------------------------------------------

function compact(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const abs = Math.abs(value);
  const sign = value < 0 ? "−" : "";
  const one = (n) => n.toLocaleString("ru-RU", { maximumFractionDigits: n >= 100 ? 0 : 1 });
  if (abs >= 1e9) return `${sign}${one(abs / 1e9)} млрд`;
  if (abs >= 1e6) return `${sign}${one(abs / 1e6)} млн`;
  if (abs >= 1e4) return `${sign}${one(abs / 1e3)} тыс.`;
  return `${sign}${abs.toLocaleString("ru-RU", { maximumFractionDigits: abs >= 100 ? 0 : 1 })}`;
}

function full(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("ru-RU", { maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2 });
}

function trim(label, max = 16) {
  const text = String(label ?? "");
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function niceTicks(min, max, count = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0];
  if (min === max) return [min];
  const span = max - min;
  const rough = span / count;
  const power = 10 ** Math.floor(Math.log10(rough));
  const candidates = [1, 2, 2.5, 5, 10].map((m) => m * power);
  const step = candidates.find((c) => c >= rough) || candidates[candidates.length - 1];
  const start = Math.floor(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

function useWidth(fallback = 560) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const measure = () => setWidth(Math.max(240, Math.round(node.getBoundingClientRect().width)));
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

// --- график ----------------------------------------------------------------

function Legend({ series }) {
  if (!series || series.length < 2) return null;
  return (
    <div className="ai-legend">
      {series.map((s, i) => (
        <span key={s.name} className="ai-legend-item">
          <i style={{ background: SERIES_COLORS[i % SERIES_COLORS.length] }} />
          {s.name}
        </span>
      ))}
    </div>
  );
}

function Axes({ width, height, pad, ticks, yScale, xLabels, xPositions, unit }) {
  const every = Math.max(1, Math.ceil(xLabels.length / Math.max(2, Math.floor((width - pad.left - pad.right) / 72))));
  return (
    <g className="ai-axes">
      {ticks.map((t) => (
        <g key={t}>
          <line x1={pad.left} x2={width - pad.right} y1={yScale(t)} y2={yScale(t)} className={t === 0 ? "zero" : ""} />
          <text x={pad.left - 6} y={yScale(t)} dy="0.32em" textAnchor="end">{compact(t)}</text>
        </g>
      ))}
      {xLabels.map((label, i) => (
        (i % every === 0 || i === xLabels.length - 1) && (
          <text key={`${label}-${i}`} x={xPositions[i]} y={height - pad.bottom + 16} textAnchor="middle">
            <title>{label}</title>
            {trim(label, 14)}
          </text>
        )
      ))}
      {unit && <text x={pad.left} y={12} className="unit">{unit}</text>}
    </g>
  );
}

function LineChart({ chart, width, height = 260 }) {
  const pad = { top: 22, right: 40, bottom: 34, left: 56 };
  const labels = chart.x || [];
  const series = chart.series || [];
  const values = series.flatMap((s) => s.values.filter((v) => v !== null && v !== undefined));
  if (!values.length) return null;
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const spread = rawMax - rawMin || Math.abs(rawMax) || 1;
  // Ноль показываем только если ряд к нему близок: иначе динамика сплющивается.
  const min = rawMin > 0 && rawMin - spread * 0.5 > 0 ? rawMin - spread * 0.25 : Math.min(0, rawMin);
  const max = rawMax < 0 ? 0 : rawMax + spread * 0.1;
  const ticks = niceTicks(min, max);
  const lo = Math.min(min, ticks[0]);
  const hi = Math.max(max, ticks[ticks.length - 1]);
  const yScale = (v) => pad.top + (height - pad.top - pad.bottom) * (1 - (v - lo) / (hi - lo || 1));
  const step = labels.length > 1 ? (width - pad.left - pad.right) / (labels.length - 1) : 0;
  const xs = labels.map((_, i) => pad.left + step * i);
  return (
    <svg className="ai-chart" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
      <Axes width={width} height={height} pad={pad} ticks={ticks} yScale={yScale} xLabels={labels} xPositions={xs} unit={chart.unit} />
      {series.map((s, si) => {
        const color = SERIES_COLORS[si % SERIES_COLORS.length];
        const points = s.values.map((v, i) => (v === null || v === undefined ? null : [xs[i], yScale(v)]));
        const path = points.map((p, i) => (p ? `${i === 0 || !points[i - 1] ? "M" : "L"}${p[0].toFixed(1)},${p[1].toFixed(1)}` : "")).join(" ");
        const last = [...points].reverse().find(Boolean);
        return (
          <g key={s.name} className="ai-series">
            <path d={path} fill="none" stroke={color} strokeWidth={si === 0 ? 2.2 : 1.8} strokeLinejoin="round" strokeLinecap="round" strokeDasharray={si >= 2 ? "4 4" : undefined} />
            {labels.length <= 40 && points.map((p, i) => p && (
              <circle key={i} cx={p[0]} cy={p[1]} r={2.6} fill={color}>
                <title>{`${labels[i]} — ${s.name}: ${full(s.values[i])}`}</title>
              </circle>
            ))}
            {last && si === 0 && (
              <text x={last[0]} y={last[1] - 10} className="value" textAnchor="middle">
                {compact(s.values.filter((v) => v !== null && v !== undefined).slice(-1)[0])}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function BarChart({ chart, width, stacked = false, height: tall = 260 }) {
  const labels = chart.x || [];
  const series = chart.series || [];
  const horizontal = labels.length > 12 || (labels.length > 5 && labels.some((l) => String(l).length > 12));
  if (horizontal) return <HorizontalBars chart={chart} width={width} stacked={stacked} />;
  const height = tall;
  const pad = { top: 22, right: 16, bottom: 34, left: 56 };
  const totals = labels.map((_, i) => {
    if (!stacked) return series.map((s) => s.values[i] || 0);
    const pos = series.reduce((acc, s) => acc + Math.max(0, s.values[i] || 0), 0);
    const neg = series.reduce((acc, s) => acc + Math.min(0, s.values[i] || 0), 0);
    return [pos, neg];
  }).flat();
  const min = Math.min(0, ...totals);
  const max = Math.max(0, ...totals);
  const ticks = niceTicks(min, max === min ? min + 1 : max);
  const lo = Math.min(min, ticks[0]);
  const hi = Math.max(max, ticks[ticks.length - 1]);
  const yScale = (v) => pad.top + (height - pad.top - pad.bottom) * (1 - (v - lo) / (hi - lo || 1));
  const slot = (width - pad.left - pad.right) / Math.max(1, labels.length);
  const xs = labels.map((_, i) => pad.left + slot * (i + 0.5));
  const groupWidth = Math.min(slot * 0.7, 56);
  const barWidth = stacked ? groupWidth : groupWidth / Math.max(1, series.length);
  const showValues = labels.length <= 12 && series.length === 1;
  return (
    <svg className="ai-chart" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
      <Axes width={width} height={height} pad={pad} ticks={ticks} yScale={yScale} xLabels={labels} xPositions={xs} unit={chart.unit} />
      {labels.map((label, i) => {
        let posBase = 0;
        let negBase = 0;
        return series.map((s, si) => {
          const v = s.values[i];
          if (v === null || v === undefined) return null;
          const color = series.length === 1 ? (v < 0 ? "var(--red)" : "var(--text)") : SERIES_COLORS[si % SERIES_COLORS.length];
          let y0;
          let y1;
          let x;
          if (stacked) {
            const base = v >= 0 ? posBase : negBase;
            y0 = yScale(base);
            y1 = yScale(base + v);
            if (v >= 0) posBase += v; else negBase += v;
            x = xs[i] - barWidth / 2;
          } else {
            y0 = yScale(0);
            y1 = yScale(v);
            x = xs[i] - groupWidth / 2 + barWidth * si;
          }
          const top = Math.min(y0, y1);
          const h = Math.max(1, Math.abs(y1 - y0));
          return (
            <g key={`${label}-${s.name}`}>
              <rect x={x} y={top} width={Math.max(2, barWidth - 2)} height={h} fill={color} rx={2}>
                <title>{`${label} — ${s.name}: ${full(v)}`}</title>
              </rect>
              {showValues && (
                <text x={xs[i]} y={v >= 0 ? top - 5 : top + h + 12} textAnchor="middle" className="value">{compact(v)}</text>
              )}
            </g>
          );
        });
      })}
    </svg>
  );
}

const MAX_HORIZONTAL_ROWS = 30;

function HorizontalBars({ chart, width, stacked }) {
  const total = (chart.x || []).length;
  const labels = (chart.x || []).slice(0, MAX_HORIZONTAL_ROWS);
  const series = (chart.series || []).map((s) => ({ ...s, values: s.values.slice(0, MAX_HORIZONTAL_ROWS) }));
  const row = 22;
  const pad = { top: 8, right: 84, bottom: total > labels.length ? 22 : 8, left: Math.min(200, Math.max(80, Math.max(...labels.map((l) => String(l).length)) * 7)) };
  const height = pad.top + pad.bottom + labels.length * row;
  const totals = labels.map((_, i) => series.map((s) => s.values[i] || 0)).flat();
  const min = Math.min(0, ...totals);
  const max = Math.max(0, ...totals);
  const xScale = (v) => pad.left + (width - pad.left - pad.right) * ((v - min) / (max - min || 1));
  const barH = stacked || series.length === 1 ? row - 6 : (row - 6) / series.length;
  return (
    <svg className="ai-chart horizontal" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
      <line x1={xScale(0)} x2={xScale(0)} y1={pad.top} y2={height - pad.bottom} className="zero" />
      {labels.map((label, i) => {
        const y = pad.top + row * i;
        return (
          <g key={`${label}-${i}`}>
            <text x={pad.left - 8} y={y + row / 2} dy="0.32em" textAnchor="end" className="cat">
              <title>{label}</title>
              {trim(label, Math.floor(pad.left / 7))}
            </text>
            {series.map((s, si) => {
              const v = s.values[i];
              if (v === null || v === undefined) return null;
              const color = series.length === 1 ? (v < 0 ? "var(--red)" : "var(--text)") : SERIES_COLORS[si % SERIES_COLORS.length];
              const x0 = xScale(Math.min(0, v));
              const x1 = xScale(Math.max(0, v));
              const yy = y + 3 + (stacked || series.length === 1 ? 0 : barH * si);
              return (
                <g key={s.name}>
                  <rect x={x0} y={yy} width={Math.max(1.5, x1 - x0)} height={Math.max(2, barH - 1)} fill={color} rx={2}>
                    <title>{`${label} — ${s.name}: ${full(v)}`}</title>
                  </rect>
                  {si === 0 && (
                    <text x={x1 + 5} y={y + row / 2} dy="0.32em" textAnchor="start" className="value">{compact(v)}</text>
                  )}
                </g>
              );
            })}
          </g>
        );
      })}
      {total > labels.length && (
        <text x={pad.left} y={height - 6} className="unit">{`показаны первые ${labels.length} из ${total}; остальные — в таблице`}</text>
      )}
    </svg>
  );
}

function Waterfall({ chart, width, height: tall = 260 }) {
  const labels = chart.x || [];
  const deltas = (chart.series?.[0]?.values || []).map((v) => v || 0);
  const start = chart.start ?? null;
  const steps = [];
  let running = start ?? 0;
  if (start !== null) steps.push({ label: "Начало", from: 0, to: start, kind: "total" });
  deltas.forEach((d, i) => {
    steps.push({ label: labels[i], from: running, to: running + d, kind: d < 0 ? "neg" : "pos" });
    running += d;
  });
  steps.push({ label: "Итог", from: 0, to: running, kind: "total" });
  const height = tall;
  const pad = { top: 22, right: 16, bottom: 34, left: 56 };
  const all = steps.flatMap((s) => [s.from, s.to]);
  const min = Math.min(0, ...all);
  const max = Math.max(0, ...all);
  const ticks = niceTicks(min, max === min ? min + 1 : max);
  const lo = Math.min(min, ticks[0]);
  const hi = Math.max(max, ticks[ticks.length - 1]);
  const yScale = (v) => pad.top + (height - pad.top - pad.bottom) * (1 - (v - lo) / (hi - lo || 1));
  const slot = (width - pad.left - pad.right) / steps.length;
  const xs = steps.map((_, i) => pad.left + slot * (i + 0.5));
  const barWidth = Math.min(slot * 0.62, 64);
  return (
    <svg className="ai-chart" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
      <Axes width={width} height={height} pad={pad} ticks={ticks} yScale={yScale} xLabels={steps.map((s) => s.label)} xPositions={xs} unit={chart.unit} />
      {steps.map((s, i) => {
        const top = Math.min(yScale(s.from), yScale(s.to));
        const h = Math.max(1.5, Math.abs(yScale(s.to) - yScale(s.from)));
        const color = s.kind === "total" ? "var(--line-strong)" : s.kind === "neg" ? "var(--red)" : "var(--text)";
        const value = s.kind === "total" ? s.to : s.to - s.from;
        return (
          <g key={`${s.label}-${i}`}>
            {i > 0 && <line x1={xs[i - 1] + barWidth / 2} x2={xs[i] - barWidth / 2} y1={yScale(steps[i - 1].to)} y2={yScale(steps[i - 1].to)} className="connector" />}
            <rect x={xs[i] - barWidth / 2} y={top} width={barWidth} height={h} fill={color} rx={2}>
              <title>{`${s.label}: ${full(value)}`}</title>
            </rect>
            <text x={xs[i]} y={top - 5} textAnchor="middle" className="value">{compact(value)}</text>
          </g>
        );
      })}
    </svg>
  );
}

function Scatter({ chart, width, height = 280 }) {
  const points = chart.points || [];
  if (!points.length) return null;
  const pad = { top: 22, right: 16, bottom: 34, left: 56 };
  const xsRaw = points.map((p) => p.x);
  const ysRaw = points.map((p) => p.y);
  const xTicks = niceTicks(Math.min(...xsRaw), Math.max(...xsRaw), 5);
  const yTicks = niceTicks(Math.min(...ysRaw), Math.max(...ysRaw));
  const xlo = xTicks[0]; const xhi = xTicks[xTicks.length - 1] || xlo + 1;
  const ylo = yTicks[0]; const yhi = yTicks[yTicks.length - 1] || ylo + 1;
  const xScale = (v) => pad.left + (width - pad.left - pad.right) * ((v - xlo) / (xhi - xlo || 1));
  const yScale = (v) => pad.top + (height - pad.top - pad.bottom) * (1 - (v - ylo) / (yhi - ylo || 1));
  return (
    <svg className="ai-chart" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
      <g className="ai-axes">
        {yTicks.map((t) => (
          <g key={`y${t}`}>
            <line x1={pad.left} x2={width - pad.right} y1={yScale(t)} y2={yScale(t)} />
            <text x={pad.left - 6} y={yScale(t)} dy="0.32em" textAnchor="end">{compact(t)}</text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={`x${t}`} x={xScale(t)} y={height - pad.bottom + 16} textAnchor="middle">{compact(t)}</text>
        ))}
        <text x={width - pad.right} y={height - 4} textAnchor="end" className="unit">{chart.xTitle}</text>
        <text x={pad.left} y={12} className="unit">{chart.yTitle}</text>
      </g>
      {points.map((p, i) => (
        <g key={i}>
          <circle cx={xScale(p.x)} cy={yScale(p.y)} r={4} fill="var(--red)" fillOpacity={0.75}>
            <title>{`${p.label ? `${p.label}: ` : ""}${full(p.x)} · ${full(p.y)}`}</title>
          </circle>
          {points.length <= 30 && p.label && (
            <text x={xScale(p.x) + 6} y={yScale(p.y) - 6} className="value">{trim(p.label, 12)}</text>
          )}
        </g>
      ))}
    </svg>
  );
}

function KpiCards({ chart, fmt }) {
  return (
    <div className="ai-facts">
      {(chart.cards || []).map((card) => (
        <div className="ai-fact" key={card.label}>
          <div className="ai-fact-value">{fmt.cell(card.value)}</div>
          <div className="ai-fact-label">{card.label}</div>
          {card.delta !== undefined && card.delta !== null && (
            <div className={`ai-fact-delta ${card.delta < 0 ? "down" : "up"}`}>{card.delta > 0 ? "+" : ""}{fmt.cell(card.delta)}</div>
          )}
        </div>
      ))}
    </div>
  );
}

// --- полноэкранный просмотр (ИИ-06) --------------------------------------------
// Таблицы и графики стоят в одной колонке с текстом ответа (решение владельца
// от 23.09.2026). Широкую таблицу или мелкий график открывают на весь экран.

// ИИ-12: таблицу или данные графика можно забрать в Excel или CSV. Файл собирает
// сервер из сохранённого ответа, с паспортом: откуда цифры, за какой период, по чьей области.
export function AiDownload({ download }) {
  if (!download?.messageId) return null;
  const href = (format) =>
    `/api/ai/messages/${download.messageId}/export?part=${encodeURIComponent(download.part || "main")}&format=${format}`;
  return (
    <span className="ai-download">
      <Download size={13} aria-hidden="true" />
      <a href={href("xlsx")} download title="Скачать в Excel: данные и паспорт выгрузки">Excel</a>
      <a href={href("csv")} download title="Скачать CSV для Excel: разделитель «;», паспорт под таблицей">CSV</a>
    </span>
  );
}

export function AiExpandButton({ onClick, label = "На весь экран" }) {
  return (
    <button type="button" className="ai-expand" onClick={onClick} aria-label={label} title={label}>
      <Maximize2 size={14} aria-hidden="true" />
    </button>
  );
}

// Полноэкранный просмотр таблицы или графика: закрывается кнопкой и Esc,
// фокус возвращается туда, откуда открыли. Рисуется в body — анимации ленты
// с transform иначе ломали бы position: fixed.
export function AiFullscreen({ title, onClose, children }) {
  const closeRef = useRef(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement;
    closeRef.current?.focus();
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      previous?.focus?.();
    };
  }, []);
  return createPortal(
    <div className="ai-fullscreen" role="dialog" aria-modal="true" aria-label={title || "Просмотр на весь экран"}>
      <div className="ai-fullscreen-head">
        <strong>{title}</strong>
        <button ref={closeRef} type="button" className="ai-fullscreen-close" onClick={() => onCloseRef.current()} aria-label="Закрыть (Esc)">
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      <div className="ai-fullscreen-body">{children}</div>
    </div>,
    document.body,
  );
}

export function AiTable({ columns, rows, fmt, title, meta, expandable = true, download = null }) {
  const decimals = columns.map((_, index) => fmt.decimals(rows, index));
  const [full, setFull] = useState(false);
  const table = (
    <div className="ai-table-wrap">
      <table className="ai-table">
        <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j} className={typeof cell === "number" ? "num" : ""}>{fmt.cell(cell, decimals[j])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
  return (
    <div className="ai-table-card">
      {(title || meta || expandable || download) && (
        <div className="ai-table-head">
          {title && <span className="ai-table-title">{title}</span>}
          {meta && <span className="ai-table-count">{meta}</span>}
          <AiDownload download={download} />
          {expandable && <AiExpandButton onClick={() => setFull(true)} />}
        </div>
      )}
      {table}
      {full && <AiFullscreen title={title || "Таблица"} onClose={() => setFull(false)}>{table}</AiFullscreen>}
    </div>
  );
}

// Графики с осями можно открыть на весь экран; карточкам KPI и таблице-графику
// это не нужно.
const EXPANDABLE_CHARTS = new Set(["line", "bar", "stacked_bar", "waterfall", "scatter"]);

function ChartBody({ chart, fmt, width, height }) {
  if (chart.type === "line") return <LineChart chart={chart} width={width} height={height} />;
  if (chart.type === "bar") return <BarChart chart={chart} width={width} height={height} />;
  if (chart.type === "stacked_bar") return <BarChart chart={chart} width={width} stacked height={height} />;
  if (chart.type === "waterfall") return <Waterfall chart={chart} width={width} height={height} />;
  if (chart.type === "scatter") return <Scatter chart={chart} width={width} height={height} />;
  if (chart.type === "kpi") return <KpiCards chart={chart} fmt={fmt} />;
  if (chart.type === "table") return <AiTable columns={chart.columns || []} rows={chart.rows || []} fmt={fmt} expandable={false} />;
  return null;
}

function FullscreenChart({ chart, fmt }) {
  const [ref, width] = useWidth(900);
  const height = Math.max(320, Math.min(640, Math.round((typeof window !== "undefined" ? window.innerHeight : 800) - 220)));
  return (
    <div className="ai-chart-card full">
      <div ref={ref} className="ai-chart-body"><ChartBody chart={chart} fmt={fmt} width={width} height={height} /></div>
      {chart.type !== "kpi" && chart.type !== "table" && <Legend series={chart.series} />}
      {chart.note && <div className="ai-chart-note">{chart.note}</div>}
    </div>
  );
}

export function AiChart({ chart, fmt, download = null }) {
  const [ref, width] = useWidth();
  const [full, setFull] = useState(false);
  const expandable = EXPANDABLE_CHARTS.has(chart.type);
  return (
    <figure className="ai-chart-card">
      <div className="ai-chart-head">
        {chart.title && <figcaption className="ai-chart-title">{chart.title}</figcaption>}
        <AiDownload download={download} />
        {expandable && <AiExpandButton onClick={() => setFull(true)} />}
      </div>
      <div ref={ref} className="ai-chart-body"><ChartBody chart={chart} fmt={fmt} width={width} /></div>
      {chart.type !== "kpi" && chart.type !== "table" && <Legend series={chart.series} />}
      {chart.note && <div className="ai-chart-note">{chart.note}</div>}
      {full && (
        <AiFullscreen title={chart.title || "График"} onClose={() => setFull(false)}>
          <FullscreenChart chart={chart} fmt={fmt} />
        </AiFullscreen>
      )}
    </figure>
  );
}

// --- шаги и структура -------------------------------------------------------

export function aiAgentStages(answer) {
  const stages = [];
  if (answer.plan || answer.depth !== "fast") {
    stages.push({
      key: "plan",
      label: `Разобрал задачу: ${TASK_TITLES[answer.taskType] || answer.taskType || "разбор"} · уровень «${DEPTH_TITLES[answer.depth] || answer.depth}»`,
      ms: answer.planMs,
      done: true,
    });
  }
  for (const step of answer.steps || []) {
    stages.push({ key: step.key, label: step.label, ms: step.ms, done: step.ok !== false });
  }
  return stages;
}

function Section({ title, items, tone }) {
  if (!items || !items.length) return null;
  return (
    <section className={tone ? `ai-section ${tone}` : "ai-section"}>
      <h4 className="ai-section-title">{title}</h4>
      <ul className="ai-list">
        {items.map((item, i) => <li key={i}>{typeof item === "string" ? item : item?.text || item?.action || ""}</li>)}
      </ul>
    </section>
  );
}

// --- типы утверждений (ИИ-25) и рекомендации (ИИ-16) -------------------------
// Тип пункта ставит бэкенд по происхождению чисел. В самом ответе меток нет —
// пункты идут обычным списком (решение владельца от 23.09.2026: метки у каждой
// строки перегружают экран). Тип, источники, формулы и поля рекомендаций —
// в свёрнутом нижнем блоке «Расшифровка ответа».

export const CLAIM_TITLES = { fact: "Факт", calc: "Расчёт", hypothesis: "Гипотеза", recommendation: "Рекомендация" };

function rowsWord(n) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return "строка";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "строки";
  return "строк";
}

function sourceText(source) {
  const rows = typeof source.rows === "number" ? `, ${source.rows.toLocaleString("ru-RU")} ${rowsWord(source.rows)}` : "";
  const kind = source.kind === "python" ? "вычисление" : "запрос к витрине";
  return `${kind} «${source.title}»${rows}`;
}

function shorten(text, max = 90) {
  const clean = String(text || "").trim();
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}

function ClaimTag({ type }) {
  return <span className={`ai-claim-tag is-${type || "fact"}`}>{CLAIM_TITLES[type] || CLAIM_TITLES.fact}</span>;
}

function RecommendationSection({ analysis }) {
  const recs = analysis.recommendations;
  if (!Array.isArray(recs)) return <Section title="Что можно сделать" items={analysis.actions} />;
  if (!recs.length && !analysis.recommendationsWithheld) return null;
  return (
    <section className="ai-section">
      <h4 className="ai-section-title">Что можно сделать</h4>
      {recs.length ? (
        <ul className="ai-list">
          {recs.map((rec, i) => <li key={i}>{rec.action}</li>)}
        </ul>
      ) : (
        <p className="ai-rec-empty">Недостаточно данных для рекомендации.</p>
      )}
      {recs.length > 0 && analysis.recommendationNote && <p className="ai-rec-note">{analysis.recommendationNote}</p>}
    </section>
  );
}

// Что показать в «Расшифровке ответа» по одному пункту.
function claimLines(claim) {
  const lines = [];
  for (const f of claim.formula || []) lines.push(["Формула", f]);
  const sources = (claim.sources || []).map(sourceText).join("; ");
  if (sources) lines.push([claim.type === "hypothesis" ? "Числа сверены с" : "Источник", sources]);
  if ((claim.columns || []).length) lines.push([claim.columns.length > 1 ? "Столбцы" : "Столбец", claim.columns.map((c) => `«${c}»`).join(", ")]);
  if (claim.note) lines.push(["Пояснение", claim.note]);
  if (claim.check) lines.push(["Проверить", claim.check]);
  return lines;
}

function BasisEntry({ type, text, lines }) {
  return (
    <li className="ai-basis-entry">
      <div className="ai-basis-head">
        <ClaimTag type={type} />
        <span className="ai-basis-quote">{shorten(text)}</span>
      </div>
      <dl className="ai-basis-rows">
        {lines.map(([label, value], i) => (
          <div key={i} className="ai-basis-row">
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </li>
  );
}

// Есть ли что расшифровывать: размеченные пункты или рекомендации.
export function aiHasBasis(answer) {
  const a = answer.analysis || {};
  const marks = a.claims || {};
  return ["happened", "why", "where"].some((k) => (marks[k] || []).length > 0) || (a.recommendations || []).length > 0;
}

export function AiClaimsBasis({ answer }) {
  const a = answer.analysis || {};
  const marks = a.claims || {};
  const plan = answer.plan || {};
  const entries = [];
  for (const key of ["happened", "why", "where"]) {
    const items = a[key] || [];
    const claims = marks[key] || [];
    if (claims.length !== items.length) continue;
    items.forEach((text, i) => {
      const lines = claimLines(claims[i] || {});
      if (lines.length) entries.push({ key: `${key}-${i}`, type: claims[i]?.type, text, lines });
    });
  }
  (a.recommendations || []).forEach((rec, i) => {
    const lines = [["Основание", rec.basis], ["Эффект", rec.effect]];
    for (const f of rec.effectFormula || []) lines.push(["Расчёт эффекта", f]);
    lines.push(["Не подходит", rec.limits], ["Источник", rec.source], ["Уверенность", rec.confidence]);
    if (rec.softened) lines.push(["Формулировка", "смягчена: категоричные слова заменены по стоп-листу"]);
    entries.push({ key: `rec-${i}`, type: "recommendation", text: rec.action, lines });
  });
  const context = [
    plan.period ? `период — ${plan.period}` : "",
    (plan.filters || []).filter(Boolean).length ? `фильтры — ${plan.filters.filter(Boolean).join(", ")}` : "",
    answer.scopeLabel ? `область — ${answer.scopeLabel}` : "",
  ].filter(Boolean).join("; ");
  return (
    <div className="ai-basis">
      <p className="ai-basis-lead">
        Метку ставит система по происхождению чисел, а не модель: «Факт» — цифра из витрины, «Расчёт» — посчитано
        из данных ответа, «Гипотеза» — данные её не доказывают.
        {context ? ` Данные: ${context}.` : ""}
      </p>
      {entries.length > 0 && (
        <ul className="ai-basis-list">
          {entries.map((e) => <BasisEntry key={e.key} type={e.type} text={e.text} lines={e.lines} />)}
        </ul>
      )}
    </div>
  );
}

function AiSteps({ steps, maySeeSql, fmt }) {
  return (
    <ol className="ai-steps">
      {steps.map((step) => (
        <li key={step.key} className={step.ok === false ? "ai-step bad" : "ai-step"}>
          <span className="ai-step-mark" aria-hidden="true">{step.ok === false ? <X size={13} /> : <Check size={13} />}</span>
          <div className="ai-step-body">
            <div className="ai-step-line">
              <span>{step.label}</span>
              {step.ms ? <span className="ai-step-ms">{step.ms >= 1000 ? `${(step.ms / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} с` : `${step.ms} мс`}</span> : null}
            </div>
            {(step.warnings || []).map((w) => (
              <div key={w} className="ai-step-warn"><AlertTriangle size={12} /> {w}</div>
            ))}
            {step.error && <div className="ai-step-warn">{step.error}</div>}
            {maySeeSql && step.sql && <pre className="ai-sql">{step.sql}</pre>}
            {maySeeSql && step.code && <pre className="ai-sql">{step.code}</pre>}
            {maySeeSql && step.output && <pre className="ai-sql out">{step.output}</pre>}
          </div>
        </li>
      ))}
    </ol>
  );
}

export function AiAnalysis({ answer, maySeeSql, Fold, fmt, messageId = null }) {
  const download = (part) => (messageId ? { messageId, part } : null);
  const analysis = answer.analysis || {};
  const rows = answer.rows || [];
  const columns = answer.columns || [];
  const steps = answer.steps || [];
  const sqlSteps = steps.filter((s) => s.kind === "sql" && s.ok !== false);
  const readRows = sqlSteps.reduce((acc, s) => acc + (s.rows || 0), 0);
  const mainId = answer.frame?.mainResult;
  const mainStep = steps.find((s) => s.resultId && mainId && s.resultId.toLowerCase() === String(mainId).toLowerCase())
    || [...steps].reverse().find((s) => s.resultId && s.kind === "sql");
  const mainTitle = mainStep?.purpose || "Данные расчёта";
  const charted = new Set((answer.charts || []).map((c) => c.source));
  const showMain = rows.length > 0 && !charted.has(mainId || mainStep?.resultId);

  return (
    <>
      {analysis.headline && <p className="ai-headline">{analysis.headline}</p>}
      <Section title="Что произошло" items={analysis.happened} />
      {(answer.charts || []).map((chart) => (
        <AiChart key={chart.id} chart={chart} fmt={fmt} download={download(`chart-${chart.id}`)} />
      ))}
      <Section title="Почему" items={analysis.why} />
      <Section title="Где именно" items={analysis.where} />
      {showMain && (
        <AiTable
          columns={columns}
          rows={rows}
          fmt={fmt}
          title={mainTitle}
          meta={answer.truncated ? `первые ${fmt.int(rows.length)}` : `${fmt.int(rows.length)} строк`}
          download={download("main")}
        />
      )}
      <RecommendationSection analysis={analysis} />
      <Section title="Ограничения данных" items={analysis.limitations} tone="limits" />

      <div className="ai-folds">
        {/* Расшифровка — отдельным нижним блоком, свёрнутым (решение владельца от 23.09.2026). */}
        {aiHasBasis(answer) && (
          <Fold title="Расшифровка ответа" meta="источники, формулы, основания">
            <AiClaimsBasis answer={answer} />
          </Fold>
        )}
        {(answer.tables || []).map((table) => (
          <Fold key={table.id} title={`Таблица: ${table.title}`} meta={`${fmt.int(table.rows.length)} строк`}>
            <AiTable columns={table.columns} rows={table.rows} fmt={fmt} download={download(`table-${table.id}`)} />
          </Fold>
        ))}
        <Fold title="Откуда число" meta={`DWH ЛИКАРД · ${answer.scopeLabel}`}>
          <p className="ai-source-text">
            Источник: DWH ЛИКАРД. Область данных: {answer.scopeLabel} — подставлена системой, а не выбрана моделью.
            {" "}Уровень: «{DEPTH_TITLES[answer.depth] || answer.depth}», задача — {TASK_TITLES[answer.taskType] || answer.taskType}.
            {" "}Запросов к витрине: {sqlSteps.length}, прочитано строк: {fmt.int(readRows)}.
            {" "}Каждый запрос прошёл проверку допустимости; числа в тексте сверены с результатами расчёта.
          </p>
        </Fold>
        {steps.length > 0 && (
          <Fold title="Показать расчёты" meta={maySeeSql ? "шаги, SQL и код" : `${steps.length} шагов`}>
            <AiSteps steps={steps} maySeeSql={maySeeSql} fmt={fmt} />
          </Fold>
        )}
      </div>
    </>
  );
}

export function aiAnalysisText(answer) {
  const a = answer.analysis || {};
  const parts = [];
  if (a.headline) parts.push(a.headline);
  const block = (title, items) => {
    if (!items || !items.length) return;
    const lines = items.map((item) => `• ${typeof item === "string" ? item : item?.text || item?.action || ""}`);
    parts.push(`${title}:\n${lines.join("\n")}`);
  };
  block("Что произошло", a.happened);
  block("Почему", a.why);
  block("Где именно", a.where);
  if (Array.isArray(a.recommendations)) {
    if (a.recommendations.length) {
      const lines = a.recommendations.map((r) => [
        `• ${r.action}`,
        `  Основание: ${r.basis}`,
        `  Эффект: ${r.effect}`,
        `  Не подходит: ${r.limits}`,
      ].join("\n"));
      parts.push(`Что можно сделать:\n${lines.join("\n")}${a.recommendationNote ? `\n${a.recommendationNote}` : ""}`);
    } else if (a.recommendationsWithheld) {
      parts.push("Что можно сделать:\nНедостаточно данных для рекомендации.");
    }
  } else {
    block("Что можно сделать", a.actions);
  }
  block("Ограничения данных", a.limitations);
  return parts.join("\n\n");
}
