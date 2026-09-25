"""Состояние одного прогона агента: бюджет, результаты, шаги, трасса.

Всё, что модель «знает» о данных, лежит в `Workspace.results` — наборах
строк с идентификаторами r1, r2… (SQL) и p1, p2… (Python). Графики и
финальный текст ссылаются на эти идентификаторы, а не на числа из
головы модели: так число в ответе всегда можно проследить до запроса.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

DEPTHS = ("fast", "analyze", "deep")
TASK_TYPES = ("lookup", "compare", "trend", "diagnose", "anomaly", "opportunity", "whatif", "other")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Budget:
    """Сколько агенту разрешено сделать в выбранной глубине."""

    depth: str
    sql_calls: int
    python_calls: int
    charts: int
    model_turns: int
    row_limit: int
    schema_calls: int = 12

    used_sql: int = 0
    used_python: int = 0
    used_charts: int = 0
    used_turns: int = 0
    used_schema: int = 0

    @classmethod
    def for_depth(cls, depth: str, limits=None) -> "Budget":
        """Бюджет глубины; `limits` (backend/ai/limits.py) заменяет пределы роли, например администратора."""
        base = cls._standard(depth)
        if limits is None:
            return base
        if not getattr(limits, "unlimited", False):
            # Роль задаёт только строки уровня (ИИ-03); число шагов — общее для всех.
            if getattr(limits, "agent_rows", None):
                base.row_limit = int(limits.agent_rows)
            return base
        return cls(
            depth=base.depth,
            sql_calls=limits.sql_calls or base.sql_calls,
            python_calls=limits.python_calls or base.python_calls,
            charts=limits.charts or base.charts,
            model_turns=limits.model_turns or base.model_turns,
            row_limit=limits.agent_rows or base.row_limit,
            schema_calls=max(base.schema_calls, limits.sql_calls or 0),
        )

    @classmethod
    def _standard(cls, depth: str) -> "Budget":
        if depth == "deep":
            return cls(
                depth="deep",
                sql_calls=_env_int("AI_AGENT_DEEP_SQL", 10),
                python_calls=_env_int("AI_AGENT_DEEP_PY", 4),
                charts=_env_int("AI_AGENT_DEEP_CHARTS", 3),
                model_turns=_env_int("AI_AGENT_DEEP_TURNS", 18),
                row_limit=_env_int("AI_AGENT_DEEP_ROWS", 2000),
            )
        return cls(
            depth="analyze",
            sql_calls=_env_int("AI_AGENT_SQL", 5),
            python_calls=_env_int("AI_AGENT_PY", 2),
            charts=_env_int("AI_AGENT_CHARTS", 2),
            model_turns=_env_int("AI_AGENT_TURNS", 10),
            row_limit=_env_int("AI_AGENT_ROWS", 1000),
        )

    def remaining(self) -> dict:
        return {
            "sql": self.sql_calls - self.used_sql,
            "python": self.python_calls - self.used_python,
            "charts": self.charts - self.used_charts,
            "turns": self.model_turns - self.used_turns,
        }

    def exhausted(self) -> bool:
        return self.used_turns >= self.model_turns


@dataclass
class ResultSet:
    """Набор строк, полученный инструментом. Идентификатор виден модели."""

    id: str
    columns: list[str]
    rows: list[list[Any]]
    source: str                 # sql | python
    purpose: str = ""
    sql: str | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def column_index(self, name: str) -> int:
        wanted = (name or "").strip().lower()
        for index, column in enumerate(self.columns):
            if column.lower() == wanted:
                return index
        raise KeyError(name)

    def column(self, name: str) -> list[Any]:
        index = self.column_index(name)
        return [row[index] for row in self.rows]

    def preview(self, limit: int = 8) -> dict:
        """То, что модель видит после вызова: колонки, размер, первые строки."""
        return {
            "id": self.id,
            "columns": self.columns,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "rows": self.rows[:limit],
            "warnings": self.warnings,
        }

    def as_table(self, title: str = "") -> dict:
        return {
            "id": self.id,
            "title": title or self.purpose or self.id,
            "columns": self.columns,
            "rows": self.rows,
            "truncated": self.truncated,
        }


@dataclass
class Step:
    """Один измеренный шаг агента — то, что показывается в строке рассуждения."""

    key: str
    kind: str                 # plan | schema | sql | python | chart | write | check
    label: str                # человеческая подпись без внутренностей
    ms: int = 0
    ok: bool = True
    purpose: str = ""
    rows: int | None = None
    result_id: str | None = None
    sql: str | None = None    # уходит на клиент только admin/subadmin
    code: str | None = None   # то же правило, что для SQL
    output: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def public(self, with_code: bool) -> dict:
        item = {
            "key": self.key, "kind": self.kind, "label": self.label, "ms": self.ms,
            "ok": self.ok, "purpose": self.purpose, "rows": self.rows,
            "resultId": self.result_id, "warnings": self.warnings, "error": self.error,
        }
        if with_code:
            item["sql"] = self.sql
            item["code"] = self.code
            item["output"] = self.output
        return item


@dataclass
class Workspace:
    results: dict[str, ResultSet] = field(default_factory=dict)
    charts: list[dict] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    _seq: dict[str, int] = field(default_factory=dict)

    def next_id(self, prefix: str) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}{self._seq[prefix]}"

    def add(self, result: ResultSet) -> ResultSet:
        self.results[result.id] = result
        return result

    def get(self, result_id: str) -> ResultSet:
        key = (result_id or "").strip().lower()
        if key not in self.results:
            raise KeyError(f"нет результата {result_id!r}; есть: {', '.join(self.results) or 'ничего'}")
        return self.results[key]

    def summary(self) -> str:
        """Краткая сводка рабочего пространства для модели."""
        if not self.results:
            return "Результатов пока нет."
        lines = []
        for result in self.results.values():
            head = f"{result.id}: {result.purpose or result.source} — {result.row_count} строк"
            if result.truncated:
                head += " (обрезано)"
            lines.append(head + "; колонки: " + ", ".join(result.columns))
            if result.warnings:
                lines.append("   предупреждения: " + "; ".join(result.warnings))
        if self.charts:
            lines.append("Графики: " + ", ".join(f"{c['id']} ({c['type']})" for c in self.charts))
        return "\n".join(lines)


@dataclass
class Plan:
    standalone_question: str
    task_type: str = "other"
    depth: str = "analyze"
    steps: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    period: str = ""
    filters: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    reason: str = ""
    source: str = "model"      # model | heuristic | forced
    elapsed_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "standaloneQuestion": self.standalone_question, "taskType": self.task_type,
            "depth": self.depth, "steps": self.steps, "metrics": self.metrics,
            "period": self.period, "filters": self.filters, "missing": self.missing,
            "reason": self.reason, "source": self.source,
        }


# Строка под блоком рекомендаций (ИИ-16): рекомендация — не распоряжение.
RECOMMENDATION_NOTE = "Рекомендации носят справочный характер; решение принимает руководитель."


@dataclass
class Analysis:
    headline: str = ""
    happened: list[str] = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    # Что подтвердит гипотезу — по тексту пункта «почему» (ИИ-25), и поля
    # рекомендаций от модели: действие, основание, эффект, ограничения… (ИИ-16).
    checks: dict[str, str] = field(default_factory=dict)
    recs: list[dict] = field(default_factory=list)
    # Итог разметки кодом: типы пунктов, принятые рекомендации, сколько снято.
    claims: dict = field(default_factory=dict)
    recommendations: list[dict] | None = None
    withheld: int = 0
    recs_asked: bool = True

    def as_dict(self) -> dict:
        out = {
            "headline": self.headline, "happened": self.happened, "why": self.why,
            "where": self.where, "actions": self.actions, "limitations": self.limitations,
        }
        if self.claims:
            out["claims"] = self.claims
        if self.recommendations is not None:
            out["recommendations"] = self.recommendations
            if self.recommendations:
                out["recommendationNote"] = RECOMMENDATION_NOTE
            elif self.withheld and self.recs_asked:
                # «Недостаточно данных для рекомендации» — только если о них просили.
                out["recommendationsWithheld"] = self.withheld
        return out

    def model_view(self) -> dict:
        """Черновик в формате finish — чтобы попросить модель исправить его."""
        why = [{"text": t, "check": self.checks[t]} if self.checks.get(t) else t for t in self.why]
        return {
            "headline": self.headline, "happened": self.happened, "why": why, "where": self.where,
            "actions": self.recs or self.actions, "limitations": self.limitations,
        }

    def text(self) -> str:
        parts = [self.headline] + self.happened
        return " ".join(p.strip() for p in parts if p and p.strip())

    def empty(self) -> bool:
        return not (self.headline or self.happened or self.why or self.where or self.actions)


@dataclass
class AgentOutcome:
    """Результат прогона агента — то, что pipeline переложит в Answer."""

    ok: bool
    plan: Plan
    analysis: Analysis
    workspace: Workspace
    model: str | None
    model_ms: int = 0
    sql_ms: int = 0
    turns: int = 0
    error: str | None = None
    rule: str | None = None
    grounding: dict = field(default_factory=dict)
    frame: dict = field(default_factory=dict)
    # Для журнала (ИИ-03): почему закончился сбор данных и сколько было вызовов инструментов.
    stop_reason: str = ""
    tool_calls: int = 0
    # Сколько раз старые результаты сжимались, чтобы уложиться в контекст модели.
    context_trims: int = 0

    @property
    def main_result(self) -> ResultSet | None:
        """Главная таблица ответа: последний SQL-результат с данными."""
        candidates = [r for r in self.workspace.results.values() if r.rows and r.source != "file_text"]
        return candidates[-1] if candidates else None
