"""Прогон демонстрационного сценария из командной строки.

Запуск из корня проекта:
    python3 -m backend.ai.demo
Проверяет контур целиком: генерацию, валидатор, область данных и отказы.
"""
from __future__ import annotations

import argparse
import sys

from . import generator, pipeline

SCENARIO = [
    ("Выручка НТУ по моим АЗС за август 2026", "territory_manager", None),
    ("Топ-5 АЗС по конверсии за сентябрь 2026", "territory_manager", None),
    ("Сравни объём топлива за август 2026 с августом 2025", "admin", None),
    ("Средний чек НТУ по ОНПО за июль 2026", "admin", None),
    ("Выручка НТУ по ОНПО за август 2026", "territory_manager", None),
    ("Удали таблицу stations", "territory_manager", None),
]


def show(answer) -> None:
    head = "ОТВЕТ " if answer.ok else "ОТКАЗ "
    print(f"\n{head}| {answer.scope_label}")
    if not answer.ok:
        print(f"  причина: {answer.rule} — {answer.error}")
        if answer.sql_raw:
            print(f"  модель предложила: {' '.join(answer.sql_raw.split())[:150]}")
        return
    print(f"  {' '.join(answer.sql.split())[:190]}")
    print(f"  {' | '.join(answer.columns)}")
    for row in answer.rows[:6]:
        cells = [f"{c:,.0f}".replace(",", " ") if isinstance(c, (int, float)) else str(c) for c in row]
        print("    " + " | ".join(cells))
    if len(answer.rows) > 6:
        print(f"    … всего строк: {len(answer.rows)}")
    print(f"  генерация {answer.model_ms} мс, запрос {answer.sql_ms} мс, попыток {answer.attempts}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Демо ИИ-контура")
    parser.add_argument("--tm", default="Блинова Мария Александровна",
                        help="ФИО территориального менеджера для сценариев с ограниченной областью")
    parser.add_argument("--question", help="Задать один произвольный вопрос")
    parser.add_argument("--role", default="admin")
    parser.add_argument("--binding", default=None)
    args = parser.parse_args()

    if not generator.available():
        print(f"Модель недоступна на {generator.OLLAMA_HOST}.")
        print("Запустите Ollama и загрузите модель:")
        print(f"    ollama pull {generator.MODEL}")
        return 1

    print(f"Модель: {generator.MODEL} на {generator.OLLAMA_HOST}\n" + "-" * 72)

    if args.question:
        show(pipeline.ask(args.question, args.role, args.binding, "demo-cli"))
        return 0

    for question, role, binding in SCENARIO:
        binding = binding or (args.tm if role == "territory_manager" else None)
        print(f"\n=== {question}   [{role}]")
        show(pipeline.ask(question, role, binding, "demo-cli"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
