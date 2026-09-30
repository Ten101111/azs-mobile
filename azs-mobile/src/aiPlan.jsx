// Режим планирования (ИИ-23): карточка плана до выполнения — цель, ожидаемый результат,
// подзадачи с порядком и зависимостями, риски, критерии успеха. Человек правит пункты
// (текст, порядок, удаление, добавление) и нажимает «Выполнить»; ИИ идёт по пунктам,
// а после ответа видно, что выполнено и почему пропущено остальное.
//
// Проверку правок делает сервер (backend/ai/agent/planning.py): пункт вне контура
// («удалить», «отправить») и зависимость «снизу» не принимаются. Здесь — только те же
// подсказки заранее, чтобы кнопка не обещала невозможного.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown, ArrowUp, Check, CircleDashed, ListChecks, LoaderCircle, Minus, Play, Plus, X,
} from "lucide-react";

// План бывает только у «Среднего» и «Высокого»: «Лёгкий» — один запрос, у «Авто» уровень
// ещё не известен (решение владельца 25.09.2026 — отдельная кнопка «План» у поля ввода).
export const PLAN_DEPTHS = ["analyze", "deep"];

// Кнопка «План» у поля ввода: включает показ плана перед выполнением.
export function AiPlanToggle({ on, onChange, disabled = false }) {
  return (
    <button
      type="button"
      className={on ? "ai-plan-toggle on" : "ai-plan-toggle"}
      aria-pressed={on}
      disabled={disabled}
      onClick={() => onChange(!on)}
      title={on ? "План включён: ИИ покажет план и начнёт после вашего подтверждения"
        : "Показать план перед выполнением — его можно поправить до запуска"}
    >
      <ListChecks size={15} aria-hidden="true" />
      <span className="ai-plan-toggle-label">План</span>
    </button>
  );
}

const KIND_TITLES = { sql: "Витрина", python: "Расчёт", chart: "График", file: "Файл", text: "Вывод" };
const DEPTH_NAMES = { analyze: "Средний", deep: "Высокий" };
// Те же слова, что у сервера: «ИИ-аналитик только читает витрину, считает, строит графики».
const OUTSIDE = /(удал|стер|очист|измен\S* (данн|запис|таблиц|витрин|справочн)|исправ\S* (данн|витрин)|запиш|сохран\S* в (баз|витрин|систем)|обнов\S* (данн|витрин|справочн|баз)|отправ|пошл|разошл|письм|почт|e-?mail|телеграм|telegram|интернет|сайт|скача|загруз\S* из)/i;
const MAX_SUBTASKS = 10;

function editable(card) {
  return {
    goal: card.goal || "",
    expected: card.expected || "",
    subtasks: (card.subtasks || []).map((s) => ({ id: s.id, title: s.title, kind: s.kind, after: [...(s.after || [])], origin: s.origin })),
  };
}

function problems(draft) {
  const out = {};
  draft.subtasks.forEach((subtask, index) => {
    const earlier = new Set(draft.subtasks.slice(0, index).map((s) => s.id));
    if ((subtask.title || "").trim().length < 3) out[subtask.id] = "Пустой пункт — допишите или удалите";
    else if (OUTSIDE.test(subtask.title)) out[subtask.id] = "Вне возможностей ИИ-аналитика: он только читает витрину, считает и строит графики";
    else {
      const late = subtask.after.find((ref) => draft.subtasks.some((s) => s.id === ref) && !earlier.has(ref));
      if (late) {
        const number = draft.subtasks.findIndex((s) => s.id === late) + 1;
        out[subtask.id] = `Опирается на пункт ${number}, который стоит ниже — поменяйте порядок`;
      }
    }
  });
  return out;
}

function StatusIcon({ state }) {
  if (state === "done") return <Check size={14} className="ai-plan-status done" aria-label="выполнен" />;
  if (state === "failed") return <X size={14} className="ai-plan-status failed" aria-label="не удался" />;
  if (state === "skipped") return <Minus size={14} className="ai-plan-status skipped" aria-label="пропущен" />;
  if (state === "active") return <LoaderCircle size={14} className="ai-plan-status active" aria-label="выполняется" />;
  return <CircleDashed size={14} className="ai-plan-status" aria-label="ждёт" />;
}

// Текст пункта переносится, а не обрезается: на телефоне пункты длиннее строки.
function AutoTitle({ value, onChange, disabled, label }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const fit = () => {
      node.style.height = "auto";
      node.style.height = `${node.scrollHeight}px`;
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [value]);
  return (
    <textarea ref={ref} rows={1} className="ai-plan-title-input" value={value} disabled={disabled} maxLength={200}
      aria-label={label} onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => { if (event.key === "Enter") event.preventDefault(); }} />
  );
}

function Lines({ title, items }) {
  if (!items?.length) return null;
  return (
    <div className="ai-plan-field">
      <span className="ai-plan-label">{title}</span>
      <ul className="ai-plan-bullets">{items.map((item) => <li key={item}>{item}</li>)}</ul>
    </div>
  );
}

// Карточка плана: проект (правится), выполняется (прогресс по пунктам), отменён.
export function AiPlanCard({ card, running = false, live = {}, liveNote = "", error = "", onRun, onCancel, onRepeat }) {
  const [draft, setDraft] = useState(() => editable(card));
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { setDraft(editable(card)); }, [card]);
  const cancelled = card.status === "cancelled";
  const locked = running || cancelled || busy;
  const issues = useMemo(() => problems(draft), [draft]);
  const numbers = Object.fromEntries(draft.subtasks.map((s, index) => [s.id, index + 1]));
  const changed = JSON.stringify(editable(card)) !== JSON.stringify(draft);

  function update(id, patch) {
    setDraft((current) => ({ ...current, subtasks: current.subtasks.map((s) => (s.id === id ? { ...s, ...patch } : s)) }));
  }
  function move(index, shift) {
    setDraft((current) => {
      const list = [...current.subtasks];
      const target = index + shift;
      if (target < 0 || target >= list.length) return current;
      [list[index], list[target]] = [list[target], list[index]];
      return { ...current, subtasks: list };
    });
  }
  function remove(id) {
    setDraft((current) => ({
      ...current,
      subtasks: current.subtasks.filter((s) => s.id !== id).map((s) => ({ ...s, after: s.after.filter((ref) => ref !== id) })),
    }));
  }
  function add() {
    const title = adding.trim();
    if (title.length < 3 || draft.subtasks.length >= MAX_SUBTASKS) return;
    setDraft((current) => ({ ...current, subtasks: [...current.subtasks, { id: `new-${Date.now()}`, title, kind: "", after: [], origin: "user" }] }));
    setAdding("");
  }
  async function cancel() {
    setBusy(true);
    try { await onCancel?.(); } finally { setBusy(false); }
  }

  if (cancelled) {
    return (
      <section className="ai-plan cancelled" aria-label="План отменён">
        <div className="ai-plan-head">
          <ListChecks size={16} aria-hidden="true" />
          <strong>План отменён</strong>
          <span className="ai-plan-sub">ИИ ничего не выполнял</span>
        </div>
        <ol className="ai-plan-list readonly">
          {(card.subtasks || []).map((s, index) => (
            <li key={s.id}><span className="ai-plan-num">{index + 1}</span><span className="ai-plan-title">{s.title}</span></li>
          ))}
        </ol>
        {onRepeat && (
          <div className="ai-plan-actions">
            <button type="button" className="ui-button ghost" onClick={onRepeat}>Задать вопрос снова</button>
          </div>
        )}
      </section>
    );
  }

  const blocked = Object.keys(issues).length > 0 || !draft.subtasks.length;
  return (
    <section className={running ? "ai-plan running" : "ai-plan"} aria-label="План анализа">
      <div className="ai-plan-head">
        <ListChecks size={16} aria-hidden="true" />
        <strong>План анализа</strong>
        {card.depth && <span className="ai-chip">{`«${DEPTH_NAMES[card.depth] || card.depth}»`}</span>}
        <span className="ai-plan-sub">
          {running ? "Выполняю по плану" : "ИИ начнёт после вашего подтверждения"}
        </span>
      </div>

      <label className="ai-plan-field">
        <span className="ai-plan-label">Цель</span>
        <textarea className="ui-textarea ai-plan-input" rows={2} value={draft.goal} disabled={locked} maxLength={300}
          onChange={(event) => setDraft((current) => ({ ...current, goal: event.target.value }))} />
      </label>
      <label className="ai-plan-field">
        <span className="ai-plan-label">Ожидаемый результат</span>
        <textarea className="ui-textarea ai-plan-input" rows={2} value={draft.expected} disabled={locked} maxLength={300}
          onChange={(event) => setDraft((current) => ({ ...current, expected: event.target.value }))} />
      </label>

      <div className="ai-plan-field">
        <span className="ai-plan-label">{`Подзадачи по порядку · ${draft.subtasks.length}`}</span>
        <ol className="ai-plan-list">
          {draft.subtasks.map((subtask, index) => {
            const after = subtask.after.filter((ref) => numbers[ref]).map((ref) => numbers[ref]);
            const state = running ? live[subtask.id] || "pending" : null;
            return (
              <li key={subtask.id} className={issues[subtask.id] ? "invalid" : ""}>
                <span className="ai-plan-num">{state ? <StatusIcon state={state} /> : index + 1}</span>
                <div className="ai-plan-main">
                  <AutoTitle value={subtask.title} disabled={locked} label={`Пункт ${index + 1}`}
                    onChange={(title) => update(subtask.id, { title })} />
                  <span className="ai-plan-meta">
                    <span className={`ai-plan-kind is-${subtask.kind || "user"}`}>
                      {subtask.origin === "user" && !card.subtasks?.some((s) => s.id === subtask.id) ? "Ваш пункт" : KIND_TITLES[subtask.kind] || "Ваш пункт"}
                    </span>
                    {after.length > 0 && <span>{`после ${after.join(", ")}`}</span>}
                  </span>
                  {issues[subtask.id] && <span className="ai-plan-issue">{issues[subtask.id]}</span>}
                </div>
                {!locked && (
                  <span className="ai-plan-controls">
                    <button type="button" className="ai-plan-icon" onClick={() => move(index, -1)} disabled={index === 0} aria-label={`Поднять пункт ${index + 1}`}><ArrowUp size={14} /></button>
                    <button type="button" className="ai-plan-icon" onClick={() => move(index, 1)} disabled={index === draft.subtasks.length - 1} aria-label={`Опустить пункт ${index + 1}`}><ArrowDown size={14} /></button>
                    <button type="button" className="ai-plan-icon" onClick={() => remove(subtask.id)} aria-label={`Убрать пункт ${index + 1}`}><X size={14} /></button>
                  </span>
                )}
              </li>
            );
          })}
        </ol>
        {!locked && draft.subtasks.length < MAX_SUBTASKS && (
          <div className="ai-plan-add">
            <input className="ui-input" value={adding} maxLength={200} placeholder="Добавить пункт: например, сравнить с тем же месяцем прошлого года"
              onChange={(event) => setAdding(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); add(); } }} />
            <button type="button" className="ui-button ghost" onClick={add} disabled={adding.trim().length < 3}>
              <Plus size={15} /> Добавить
            </button>
          </div>
        )}
      </div>

      <div className="ai-plan-cols">
        <Lines title="Риски" items={card.risks} />
        <Lines title="Критерии успеха" items={card.success} />
      </div>
      {(card.period || card.filters?.length) && (
        <p className="ai-plan-scope">
          {[card.period && `Период: ${card.period}`, card.filters?.length && `Фильтры: ${card.filters.join("; ")}`].filter(Boolean).join(" · ")}
        </p>
      )}

      {error && <p className="ai-plan-error" role="alert">{error}</p>}
      {running ? (
        <p className="ai-plan-live" role="status"><LoaderCircle size={14} aria-hidden="true" />{liveNote || "Выполняю план"}</p>
      ) : (
        <div className="ai-plan-actions">
          <button type="button" className="ui-button" disabled={blocked || busy} onClick={() => onRun?.({ ...card, ...draft })}>
            <Play size={15} /> Выполнить
          </button>
          <button type="button" className="ui-button ghost" disabled={busy} onClick={cancel}>Отменить</button>
          {changed && <span className="ai-plan-changed">План изменён — ИИ выполнит его в вашей редакции</span>}
        </div>
      )}
    </section>
  );
}

// После ответа: что выполнено по плану и почему пропущено остальное.
export function AiPlanSummary({ card }) {
  const [open, setOpen] = useState(false);
  if (!card || card.status !== "done") return null;
  const total = (card.subtasks || []).length;
  const left = (card.subtasks || []).filter((s) => s.status !== "done").length;
  const edits = (card.changes || []).length;
  return (
    <div className={left ? "ai-plan-summary warn" : "ai-plan-summary"}>
      <button type="button" className="ai-plan-summary-line" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <ListChecks size={15} aria-hidden="true" />
        <strong>{`По плану: выполнено ${card.done ?? total - left} из ${total}`}</strong>
        {left > 0 && <span>{`отклонений: ${left}`}</span>}
        {edits > 0 && <span>{`ваших правок: ${edits}`}</span>}
      </button>
      {open && (
        <ol className="ai-plan-list readonly">
          {(card.subtasks || []).map((s) => (
            <li key={s.id}>
              <span className="ai-plan-num"><StatusIcon state={s.status} /></span>
              <div className="ai-plan-main">
                <span className="ai-plan-title">{s.title}{s.origin === "user" ? " · ваш пункт" : ""}</span>
                {s.note && <span className="ai-plan-note">{s.note}</span>}
              </div>
            </li>
          ))}
          {(card.extra || []).length > 0 && (
            <li className="extra">
              <span className="ai-plan-num"><Plus size={14} aria-hidden="true" /></span>
              <div className="ai-plan-main">
                <span className="ai-plan-title">Сверх плана</span>
                <span className="ai-plan-note">{card.extra.join("; ")}</span>
              </div>
            </li>
          )}
        </ol>
      )}
    </div>
  );
}
