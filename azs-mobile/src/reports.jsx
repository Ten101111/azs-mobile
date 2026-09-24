// Справки (СП-04, 23.09.2026): актуальный выпуск, архив, PDF и Excel.
//
// Экран рисуется из JSON выпуска и ничего не пересчитывает: цифры на экране, в
// PDF и в Excel — одни и те же. Справка — зафиксированный выпуск недели, а не
// живые цифры: «на сейчас» — в Своде. Пока справки видит только администратор
// (решение Р-13). С 24.09.2026 справку собирает компьютер владельца по витрине
// ОХД, сервер её только показывает; «Сформировать заново» ставит запрос, который
// сборщик заберёт при ближайшем запуске.
import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Clock3, Download, FileText, RefreshCw } from "lucide-react";
import { AiChart } from "./aiAnalysis.jsx";

const NBSP = " ";
const UNITS = { руб: "₽", шт: "шт.", л: "л", т: "т", "%": "%" };
const STATUS_TITLES = { published: "актуальная", replaced: "заменена", not_formed: "не сформирована" };
const RECOMMENDATION_NOTE = "Рекомендации носят справочный характер; решение принимает руководитель.";

// Текст ИИ (СП-06) — только если сервер сверил его с цифрами выпуска; иначе шаблон.
function aiText(model) {
  const text = model.narrative;
  return text && text.source === "ai" ? text : null;
}

function capital(text) {
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

function planCaption(month) {
  if (month.closed) return `${capital(month.label)} — итоги месяца`;
  return `${capital(month.label)}: прошло ${month.daysPassed} из ${month.daysTotal} дней (${num(month.daysPct, 1)}${NBSP}%)`;
}

const STATUS_TONE = { норма: "up", внимание: "warn", критично: "down" };

function PlanStatus({ value }) {
  if (!value) return <span className="report-muted">—</span>;
  return <span className={`report-status ${STATUS_TONE[value] || ""}`}>{value}</span>;
}

// План месяца (СП-07): выполнение, % прошедших дней, отставание и темп; закрытый месяц — итоги.
function PlanMonth({ month, note }) {
  const toDate = !month.planComplete && !month.closed;
  return (
    <div className="report-plan-month">
      <h4 className="report-subtitle">{planCaption(month)}</h4>
      <div className="report-table-wrap">
        <table className="report-table report-plan">
          <thead>
            <tr>
              <th>Показатель</th>
              <th className="num opt">{month.closed ? "Факт за месяц" : "Факт с начала месяца"}</th>
              <th className="num opt">{toDate ? "План на дату" : "План месяца"}</th>
              <th className="num">{toDate ? "Выполнение на дату" : "Выполнение"}</th>
              {!toDate && <th className="num">{month.closed ? "Отклонение" : "Отставание"}</th>}
              {!month.closed && !toDate && <th className="num">Нужно в день</th>}
              {!month.closed && !toDate && <th className="num opt">Сейчас в день</th>}
            </tr>
          </thead>
          <tbody>
            {month.rows.map((r) => (
              <tr key={r.code}>
                <td>{r.title}, {unit(r.unit)}</td>
                <td className="num opt">{num(r.fact, r.decimals)}</td>
                <td className="num opt">{num(toDate ? r.planToDate : r.planMonth, r.decimals)}</td>
                <td className="num strong" data-label={toDate ? "выполнено на дату" : "выполнено"}>
                  {num(toDate ? r.pctToDate : r.pctMonth, 1)}{NBSP}%
                </td>
                {!toDate && <td className="num" data-label="к графику"><Delta value={r.gapPp} share /></td>}
                {!month.closed && !toDate && (
                  <td className="num" data-label="нужно в день">{r.needPerDay > 0 ? num(r.needPerDay, r.decimals) : "—"}</td>
                )}
                {!month.closed && !toDate && <td className="num opt">{num(r.pacePerDay, r.decimals)}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {month.onpo?.length > 0 && (
        <div className="report-table-wrap">
          <table className="report-table">
            <thead>
              <tr>
                <th>ОНПО</th>
                <th className="num opt">Топливо, % плана</th>
                <th className="num">НТУ, % плана</th>
                <th className="num">{toDate ? "ВД НТУ, % плана" : "Отставание по НТУ"}</th>
                {!toDate && <th className="num opt">ВД НТУ, % плана</th>}
                {!toDate && <th>Статус</th>}
              </tr>
            </thead>
            <tbody>
              {month.onpo.map((o) => (
                <tr key={o.name}>
                  <td>{o.name}</td>
                  <td className="num opt">{num(o.fuelPct, 1)}</td>
                  <td className="num">{num(o.ntuPct, 1)}</td>
                  <td className="num">{toDate ? num(o.vdPct, 1) : <Delta value={o.ntuGap} share />}</td>
                  {!toDate && <td className="num opt">{num(o.vdPct, 1)}</td>}
                  {!toDate && <td><PlanStatus value={o.status} /></td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {toDate && note ? <p className="report-note">{capital(note)}.</p> : null}
    </div>
  );
}

function PlanSection({ plan }) {
  if (!plan?.months?.length) return null;
  const open = plan.months.some((m) => !m.closed && m.planComplete);
  return (
    <Section
      title="План месяца"
      note={open ? "Отставание — % плана минус % прошедших дней. Статус по выручке НТУ: до 3 п. п. — норма, 3–8 — внимание, больше 8 — критично." : null}
    >
      {plan.months.map((month) => <PlanMonth key={month.month} month={month} note={plan.note} />)}
    </Section>
  );
}

// Сервис (СП-07, 24.09.2026): оценки клиентов в приложении, негатив, жалобы ЕГЛ.
// Цели уровня сервиса в витрине нет — ни цели, ни статуса «выполнен» не показываем.
const SERVICE_LOWER_BETTER = new Set(["negative", "negativeShare", "complaints", "quality"]);
const SERVICE_NOTE =
  "Средняя оценка — по всем оценкам клиентов в приложении, без правил методики, поэтому это не официальный уровень сервиса; цели в витрине нет. Значения — по всей сети, изменения — по сопоставимой базе. Качество сервиса — негатив и жалобы ЕГЛ на 100 тыс. чеков, меньше — лучше.";

function signed(value, decimals = 0) {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${num(value, decimals)}`;
}

// Изменение по виду: abs — разность («+3», «−0,004»), pp — п. п., pct — %. Цвет — «лучше/хуже».
function ServiceDelta({ value, kind, decimals, lowerBetter = false }) {
  if (value === null || value === undefined) return <span className="report-delta flat">—</span>;
  const text = kind === "abs" ? signed(value, decimals)
    : kind === "pp" ? `${signed(value, decimals)}${NBSP}п.${NBSP}п.` : delta(value);
  const good = value === 0 ? "flat" : (value > 0) !== lowerBetter ? "up" : "down";
  return <span className={`report-delta ${good}`}>{text}</span>;
}

function serviceMonthLine(month) {
  const head = month.closed ? `Итоги ${month.labelGen}` : `С начала ${month.labelGen} (по ${shortDate(month.to)})`;
  const parts = [`средняя оценка ${num(month.avg, 3)}`, `оценок ${num(month.ratings)}`, `негативных ${num(month.negative)}`];
  if (month.complaints !== null && month.complaints !== undefined) parts.push(`жалоб ЕГЛ ${num(month.complaints)}`);
  if (month.quality !== null && month.quality !== undefined) parts.push(`качество сервиса ${num(month.quality, 2)}`);
  return `${head}: ${parts.join(", ")}.`;
}

function ServiceSection({ service }) {
  if (!service?.metrics?.length) return null;
  return (
    <Section title="Сервис: оценки в приложении и жалобы" note={SERVICE_NOTE}>
      <div className="report-table-wrap">
        <table className="report-table report-metrics">
          <thead>
            <tr>
              <th>Показатель</th>
              <th className="num">Неделя</th>
              <th className="num opt">Прошлая неделя</th>
              <th className="num">Δ н/н</th>
              <th className="num opt">Прошлый год</th>
              <th className="num">Δ г/г</th>
            </tr>
          </thead>
          <tbody>
            {service.metrics.map((m) => {
              const lower = SERVICE_LOWER_BETTER.has(m.code);
              return (
                <tr key={m.code}>
                  <td>{m.title}{m.unit ? `, ${unit(m.unit)}` : ""}</td>
                  <td className="num strong">{num(m.value, m.decimals)}</td>
                  <td className="num opt">{num(m.prev, m.decimals)}</td>
                  <td className="num" data-label="н/н">
                    <ServiceDelta value={m.deltaPrev} kind={m.deltaKind} decimals={m.decimals} lowerBetter={lower} />
                  </td>
                  <td className="num opt">{num(m.lastYear, m.decimals)}</td>
                  <td className="num" data-label="г/г">
                    <ServiceDelta value={m.deltaYear} kind={m.deltaKind} decimals={m.decimals} lowerBetter={lower} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {(service.months || []).map((month) => <p className="report-text" key={month.month}>{serviceMonthLine(month)}</p>)}
      {service.categories?.length > 0 && (
        <>
          <h4 className="report-subtitle">Негатив по категориям</h4>
          <div className="report-table-wrap">
            <table className="report-table">
              <thead>
                <tr><th>Категория</th><th className="num">Неделя</th><th className="num">Прошлая неделя</th></tr>
              </thead>
              <tbody>
                {service.categories.map((c) => (
                  <tr key={c.code}><td>{c.title}</td><td className="num strong">{num(c.week)}</td><td className="num">{num(c.prev)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {service.onpo?.length > 0 && (
        <>
          <h4 className="report-subtitle">ОНПО: слабые оценки — сверху</h4>
          <div className="report-table-wrap">
            <table className="report-table">
              <thead>
                <tr>
                  <th>ОНПО</th>
                  <th className="num opt">АЗС с оценками</th>
                  <th className="num">Средняя оценка</th>
                  <th className="num">Δ н/н</th>
                  <th className="num">Негатив</th>
                  <th className="num opt">Жалоб ЕГЛ</th>
                  <th className="num opt">Качество сервиса</th>
                </tr>
              </thead>
              <tbody>
                {service.onpo.map((o) => (
                  <tr key={o.name}>
                    <td>{o.name}</td>
                    <td className="num opt">{num(o.stations)}</td>
                    <td className="num strong">{num(o.avg, 3)}</td>
                    <td className="num"><ServiceDelta value={o.avgDeltaPrev} kind="abs" decimals={3} /></td>
                    <td className="num">{num(o.negative)}</td>
                    <td className="num opt">{num(o.complaints)}</td>
                    <td className="num opt">{num(o.quality, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Section>
  );
}

function NegativeTable({ rows }) {
  return (
    <div className="report-table-wrap">
      <table className="report-table">
        <thead>
          <tr>
            <th>Объект</th>
            <th className="opt">Регион</th>
            <th>ОНПО</th>
            <th className="num">Негативных</th>
            <th className="num opt">Средняя</th>
            <th className="opt">Главная категория</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td>{r.label}</td>
              <td className="opt">{r.region}</td>
              <td>{r.onpo}</td>
              <td className="num strong">{num(r.negative)}</td>
              <td className="num opt">{num(r.avg, 3)}</td>
              <td className="opt">{r.category || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Recommendations({ items }) {
  return (
    <ul className="report-recs">
      {items.map((rec) => (
        <li key={rec.action}>
          <p className="report-rec-action">{rec.action}</p>
          <p className="report-rec-basis">Основание: {rec.basis}</p>
          <p className="report-rec-basis">Ожидаемый эффект: {rec.effect}</p>
          <p className="report-rec-basis">Ограничения: {rec.limits}</p>
        </li>
      ))}
    </ul>
  );
}

function unit(code) {
  return UNITS[code] || code || "";
}

function num(value, decimals = 0) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
    .format(value)
    .replace("-", "−");
}

function delta(value, share = false) {
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${num(value, 1)}${NBSP}${share ? `п.${NBSP}п.` : "%"}`;
}

function tone(value) {
  if (value === null || value === undefined || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

function Delta({ value, share }) {
  return <span className={`report-delta ${tone(value)}`}>{delta(value, share)}</span>;
}

function stamp(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  const day = new Intl.DateTimeFormat("ru-RU", { weekday: "short", timeZone: "Europe/Moscow" }).format(date);
  const text = new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow",
  }).format(date);
  return `${day} ${text.replace(",", "")} МСК`;
}

function shortDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}

const chartFmt = { cell: (v) => num(v), decimals: () => 0, int: (v) => num(v) };

function Section({ title, children, note }) {
  return (
    <section className="report-section">
      <h3 className="report-section-title">{title}</h3>
      {children}
      {note ? <p className="report-note">{note}</p> : null}
    </section>
  );
}

function MetricsTable({ metrics }) {
  return (
    <div className="report-table-wrap">
      <table className="report-table report-metrics">
        <thead>
          <tr>
            <th>Показатель</th>
            <th className="num">Неделя</th>
            <th className="num opt">Прошлая неделя</th>
            <th className="num">Δ н/н</th>
            <th className="num opt">Прошлый год</th>
            <th className="num">Δ г/г</th>
          </tr>
        </thead>
        <tbody>
          {metrics.map((m) => (
            <tr key={m.code}>
              <td>{m.title}, {unit(m.unit)}</td>
              <td className="num strong">{num(m.value, m.decimals)}</td>
              <td className="num opt">{num(m.prev, m.decimals)}</td>
              <td className="num" data-label="н/н"><Delta value={m.deltaPrev} share={m.isShare} /></td>
              <td className="num opt">{num(m.lastYear, m.decimals)}</td>
              <td className="num" data-label="г/г"><Delta value={m.deltaYear} share={m.isShare} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OnpoTable({ rows, total, fuelUnit }) {
  const all = [...rows, { ...total, name: "Итого по сети", total: true }];
  return (
    <div className="report-table-wrap">
      <table className="report-table">
        <thead>
          <tr>
            <th>ОНПО</th>
            <th className="num">АЗС</th>
            <th className="num">Топливо, {unit(fuelUnit)}</th>
            <th className="num">Δ н/н</th>
            <th className="num">Δ г/г</th>
            <th className="num">Выручка НТУ, ₽</th>
            <th className="num">Δ н/н</th>
            <th className="num">Δ г/г</th>
            <th className="num">Конверсия НТУ</th>
          </tr>
        </thead>
        <tbody>
          {all.map((r) => (
            <tr key={r.name} className={r.total ? "total" : ""}>
              <td>{r.name}</td>
              <td className="num">{num(r.stations)}</td>
              <td className="num">{num(r.fuel)}</td>
              <td className="num"><Delta value={r.fuelDeltaPrev} /></td>
              <td className="num"><Delta value={r.fuelDeltaYear} /></td>
              <td className="num">{num(r.ntu)}</td>
              <td className="num"><Delta value={r.ntuDeltaPrev} /></td>
              <td className="num"><Delta value={r.ntuDeltaYear} /></td>
              <td className="num">{r.conversion === null || r.conversion === undefined ? "—" : `${num(r.conversion, 1)}${NBSP}%`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StationTable({ rows, fuelUnit, idle = false }) {
  return (
    <div className="report-table-wrap">
      <table className="report-table">
        <thead>
          <tr>
            <th>Объект</th>
            <th className="opt">Регион</th>
            <th>ОНПО</th>
            {idle ? <th className="num">Дней без продаж</th> : (
              <>
                <th className="num">Топливо, {unit(fuelUnit)}</th>
                <th className="num opt">Прошлая неделя</th>
                <th className="num">Δ н/н</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td>{r.label}</td>
              <td className="opt">{r.region}</td>
              <td>{r.onpo}</td>
              {idle ? <td className="num">{r.idleDays}</td> : (
                <>
                  <td className="num">{num(r.fuel)}</td>
                  <td className="num opt">{num(r.fuelPrev)}</td>
                  <td className="num"><Delta value={r.delta} /></td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function daysWord(n) {
  if (n % 10 === 1 && n % 100 !== 11) return `${n} день`;
  if (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) return `${n} дня`;
  return `${n} дней`;
}

function Attention({ attention, thresholds, fuelUnit }) {
  const drop = num(thresholds?.fuelDropPct ?? 20);
  return (
    <>
      <h4 className="report-subtitle">
        Падение топлива к прошлой неделе на {drop}{NBSP}% и больше
        <span>{attention.dropsTotal ? `всего ${attention.dropsTotal}, показаны ${attention.drops.length} с наибольшим падением` : "нет"}</span>
      </h4>
      {attention.drops.length > 0 && <StationTable rows={attention.drops} fuelUnit={fuelUnit} />}
      <h4 className="report-subtitle">
        Без продаж {daysWord(thresholds?.noSalesDays ?? 2)} и больше
        <span>{attention.noSales.length ? `${attention.noSales.length}` : "нет"}</span>
      </h4>
      {attention.noSales.length > 0 && <StationTable rows={attention.noSales} fuelUnit={fuelUnit} idle />}
      {attention.negativeTotal !== undefined && (
        <>
          <h4 className="report-subtitle">
            {num(thresholds?.negativeMin ?? 2)} и больше негативных оценок в приложении за неделю
            <span>{attention.negativeTotal ? `всего ${attention.negativeTotal}, показаны ${attention.negative.length} с наибольшим числом` : "нет"}</span>
          </h4>
          {attention.negative.length > 0 && <NegativeTable rows={attention.negative} />}
        </>
      )}
      {attention.leaders.length > 0 && (
        <>
          <h4 className="report-subtitle">Лидеры роста топлива к прошлой неделе</h4>
          <StationTable rows={attention.leaders} fuelUnit={fuelUnit} />
        </>
      )}
    </>
  );
}

function IssueView({ issue, isCurrent, onDownload }) {
  const model = issue.model;
  const passport = model.passport;
  const ai = aiText(model);
  const headline = ai?.headline?.length ? ai.headline : model.headline;
  // Подпись оси — понедельник недели, как в PDF: «27.07–02.08» не помещается на телефоне.
  const mondays = (model.dynamics?.weeks || []).map((w) => shortDate(w.from).slice(0, 5));
  const charts = (model.dynamics?.charts || []).map((c) => ({
    id: c.code, type: "line", title: c.title, series: c.series, unit: unit(c.unit),
    x: mondays.length === c.x.length ? mondays : c.x,
  }));
  return (
    <article className="report-issue">
      <header className="report-issue-head">
        <div>
          <h3>Неделя {model.week.label}</h3>
          <p>
            Данные по {shortDate(passport.latestDate)} · сформирована {stamp(passport.generatedAt)} · версия {issue.version}
            {issue.status !== "published" ? ` · ${STATUS_TITLES[issue.status] || issue.status}` : ""}
            {!isCurrent && issue.status === "published" ? " · из архива" : ""}
          </p>
          {model.reason ? <p className="report-reissue">Перевыпуск: {model.reason}</p> : null}
        </div>
        <div className="report-downloads">
          {issue.hasPdf && (
            <a className="report-button" href={`/api/reports/${issue.id}/file/pdf`} download onClick={() => onDownload("pdf")}>
              <Download size={15} aria-hidden="true" /> PDF
            </a>
          )}
          {issue.hasXlsx && (
            <a className="report-button" href={`/api/reports/${issue.id}/file/xlsx`} download onClick={() => onDownload("xlsx")}>
              <Download size={15} aria-hidden="true" /> Excel
            </a>
          )}
        </div>
      </header>

      <Section title="Главное" note={ai ? "Текст написал ИИ по цифрам этой справки; каждое число сверено с выпуском." : null}>
        <div className="report-headline">
          {headline.map((line) => <p key={line}>{line}</p>)}
        </div>
      </Section>

      <Section
        title="Цифры недели"
        note="Значения — по всей сети; изменения — по сопоставимой базе: объекты с данными за все 7 дней в обоих периодах. Для долей — в процентных пунктах."
      >
        <MetricsTable metrics={model.metrics} />
      </Section>

      <PlanSection plan={model.plan} />

      <ServiceSection service={model.service} />

      {charts.length > 0 && (
        <Section title="Динамика 8 недель" note="Подпись — понедельник недели. Прошлый год — те же недели со сдвигом на 364 дня.">
          <div className="report-charts">
            {charts.map((chart) => <AiChart key={chart.id} chart={chart} fmt={chartFmt} />)}
          </div>
        </Section>
      )}

      <Section title="ОНПО сети">
        <OnpoTable rows={model.onpo} total={model.onpoTotal} fuelUnit={model.fuelUnit} />
      </Section>

      {ai?.attention?.length > 0 && (
        <Section title="На что обратить внимание">
          <div className="report-headline">
            {ai.attention.map((line) => <p key={line}>{line}</p>)}
          </div>
        </Section>
      )}

      <Section title="Зоны внимания" note="Падения и дни без продаж — только объекты с данными за все 7 дней в обеих неделях; объекты с малой базой не ранжируются. Негатив — оценки «1» и «2» в приложении за неделю.">
        <Attention attention={model.attention} thresholds={passport.thresholds} fuelUnit={model.fuelUnit} />
      </Section>

      {ai?.recommendations?.length > 0 && (
        <Section title="Рекомендации (справочно)" note={RECOMMENDATION_NOTE}>
          <Recommendations items={ai.recommendations} />
        </Section>
      )}

      {model.pending?.length > 0 && (
        <Section title="Что пока не входит">
          <p className="report-text">
            {model.pending.join(", ")} — появятся в справке после дополнения витрины. Простои топлива в справку не входят,
            пока их данные не актуальны.
          </p>
        </Section>
      )}

      <details className="report-passport">
        <summary>Паспорт данных</summary>
        <ul>
          {(model.passportLines || []).map((line) => <li key={line}>{line}</li>)}
        </ul>
      </details>
    </article>
  );
}

function RegenerateForm({ target, request, onDone, onCancel }) {
  const [reason, setReason] = useState("");
  const [state, setState] = useState({ busy: false, error: "" });

  async function submit(event) {
    event.preventDefault();
    if (reason.trim().length < 3) {
      setState({ busy: false, error: "Укажите причину перевыпуска." });
      return;
    }
    setState({ busy: true, error: "" });
    try {
      await request("/api/reports/generate", {
        method: "POST",
        body: JSON.stringify({ reason: reason.trim(), week: target.iso || "" }),
      });
      setState({ busy: false, error: "" });
      onDone();
    } catch (error) {
      setState({ busy: false, error: error.message || "Не удалось поставить запрос на перевыпуск." });
    }
  }

  return (
    <form className="report-regenerate" onSubmit={submit}>
      <label className="ui-field">
        <span>
          Причина перевыпуска справки за {target.label} — видна в выпуске и в архиве. Справку пересоберёт по витрине ОХД
          компьютер с доступом к ОХД при ближайшем запуске.
        </span>
        <input
          className="ui-input"
          value={reason}
          maxLength={300}
          autoFocus
          onChange={(event) => setReason(event.target.value)}
          placeholder="Например: догрузились данные за воскресенье"
        />
      </label>
      <div className="report-regenerate-actions">
        <button type="submit" className="ui-button" disabled={state.busy}>
          {state.busy ? "Отправляю…" : "Запросить новую версию"}
        </button>
        <button type="button" className="ui-button ghost" onClick={onCancel} disabled={state.busy}>Отмена</button>
      </div>
      {state.error ? <p className="report-error" role="alert">{state.error}</p> : null}
    </form>
  );
}

export function ReportsScreen({ request, track, onBack }) {
  const [state, setState] = useState({ status: "loading", data: null, error: "" });
  const [selectedId, setSelectedId] = useState(null);
  const [issues, setIssues] = useState({});
  const [regenerating, setRegenerating] = useState(false);

  const load = useCallback((focusId = null) => {
    setState((previous) => ({ ...previous, status: previous.data ? "refreshing" : "loading", error: "" }));
    request("/api/reports")
      .then((data) => {
        setState({ status: "ready", data, error: "" });
        setIssues({});
        // После перевыпуска старой недели показываем новую версию, а не текущую справку.
        const focus = focusId && focusId !== data?.current?.id ? focusId : null;
        setSelectedId(focus);
        if (focus) {
          request(`/api/reports/${focus}`)
            .then((issue) => setIssues((previous) => ({ ...previous, [focus]: issue })))
            .catch(() => setSelectedId(null));
        }
      })
      .catch((error) => {
        setState({
          status: "error",
          data: null,
          error: error.status === 403 ? "Справки пока доступны только администратору." : "Не удалось загрузить справки.",
        });
      });
  }, [request]);

  useEffect(() => { load(); }, [load]);

  const data = state.data;
  const current = data?.current || null;
  const shown = selectedId ? issues[selectedId] : current;
  // Перевыпуск — той недели, что сейчас на экране; если свежая неделя не сформирована — её.
  const reissue = data?.pending && !selectedId
    ? { iso: data.pending.week, label: `${shortDate(data.pending.weekFrom)}–${shortDate(data.pending.weekTo)}` }
    : shown
    ? { iso: shown.week, label: `${shortDate(shown.weekFrom)}–${shortDate(shown.weekTo)}` }
    : { iso: data?.expectedWeek?.iso || "", label: data?.expectedWeek?.label || "прошлую неделю" };

  useEffect(() => {
    if (shown?.id) track?.("report_view", "reports", `${shown.week} v${shown.version}`);
  }, [shown?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  function open(item) {
    if (current && item.id === current.id) {
      setSelectedId(null);
      return;
    }
    setSelectedId(item.id);
    if (!issues[item.id]) {
      request(`/api/reports/${item.id}`)
        .then((issue) => setIssues((previous) => ({ ...previous, [item.id]: issue })))
        .catch(() => {});
    }
  }

  const archive = useMemo(() => data?.archive || [], [data]);

  return (
    <section className="report-pane" aria-labelledby="reports-title">
      <div className="admin-head report-head">
        <div>
          <h2 id="reports-title">Еженедельная справка по сети</h2>
          <p>{data?.schedule || "Выпуск недели с понедельника по воскресенье"}</p>
        </div>
        <div className="admin-head-actions">
          {data?.canGenerate && !regenerating ? (
            <button className="admin-refresh" type="button" onClick={() => setRegenerating(true)}>
              <RefreshCw size={15} aria-hidden="true" />
              <span>Сформировать заново</span>
            </button>
          ) : null}
          {onBack ? (
            <button className="admin-back" type="button" onClick={onBack}>На главную</button>
          ) : null}
        </div>
      </div>

      {regenerating && (
        <RegenerateForm
          target={reissue}
          request={request}
          onCancel={() => setRegenerating(false)}
          onDone={() => {
            setRegenerating(false);
            track?.("report_regenerate", "reports", reissue.iso);
            load();
          }}
        />
      )}

      {state.status === "loading" && <p className="report-muted">Загружаю справки…</p>}
      {state.status === "error" && <p className="report-error" role="alert">{state.error}</p>}

      {data?.pending && (
        <div className="report-pending" role="status">
          <AlertTriangle size={16} aria-hidden="true" />
          <p>
            Справка за {shortDate(data.pending.weekFrom)}–{shortDate(data.pending.weekTo)} ещё не сформирована: {data.pending.reason}.
            {current ? " Ниже — последняя сформированная справка." : ""}
          </p>
        </div>
      )}

      {data?.request && (
        <div className="report-pending info" role="status">
          <Clock3 size={16} aria-hidden="true" />
          <p>
            Перевыпуск справки за {data.request.weekFrom ? `${shortDate(data.request.weekFrom)}–${shortDate(data.request.weekTo)}` : data.request.week}
            {" "}запрошен: «{data.request.reason}». Новую версию соберёт по витрине ОХД компьютер с доступом к ОХД при
            ближайшем запуске (каждый час с 07:10 до 21:10 МСК).
          </p>
        </div>
      )}

      {data && !current && !data.pending && (
        <p className="report-muted">
          Справок пока нет. Первая сформируется в понедельник утром, когда в витрине будут данные за всю прошлую неделю.
        </p>
      )}

      {selectedId && !shown && <p className="report-muted">Открываю выпуск…</p>}
      {shown?.model && (
        <IssueView
          issue={shown}
          isCurrent={Boolean(current && shown.id === current.id)}
          onDownload={(kind) => track?.("report_download", "reports", `${kind} ${shown.week} v${shown.version}`)}
        />
      )}

      {archive.length > 0 && (
        <section className="report-archive" aria-labelledby="reports-archive-title">
          <h3 className="report-section-title" id="reports-archive-title">Архив выпусков</h3>
          <ul>
            {archive.map((item) => {
              const active = Boolean(shown && shown.id === item.id);
              return (
                <li key={item.id}>
                  <button type="button" className={active ? "active" : ""} aria-current={active ? "true" : undefined} onClick={() => open(item)}>
                    <FileText size={16} aria-hidden="true" />
                    <span className="report-archive-week">{shortDate(item.weekFrom)}–{shortDate(item.weekTo)}</span>
                    <span className="report-archive-meta">
                      версия {item.version}{item.status !== "published" ? ` · ${STATUS_TITLES[item.status] || item.status}` : ""}
                      {item.reason ? ` · ${item.reason}` : ""}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </section>
  );
}
