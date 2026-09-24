"""run_python — вычисления над уже прочитанными результатами.

Код модели выполняется в отдельном процессе с лимитами процессорного времени,
памяти, времени по стене и объёма вывода. В процесс не передаются ни
переменные окружения (там реквизиты витрины), ни доступ к базе: на вход идут
только результаты rN как DataFrame, на выход — печать и переменная `result`.
Импорт из кода модели разрешён только из белого списка.

Это защита от ошибок модели и от случайного выхода за пределы задачи, а не
граница безопасности против целенаправленной атаки: контур и так работает
с локальной моделью внутри периметра. Файлы, сеть, окружение и база из
песочницы недоступны по построению.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from .state import ResultSet, Step
from .tools import ToolContext, ToolError, tool

TIMEOUT_S = float(os.environ.get("AI_PY_TIMEOUT", "20"))
CPU_S = int(os.environ.get("AI_PY_CPU", "10"))
MEMORY_MB = int(os.environ.get("AI_PY_MEMORY_MB", "1536"))
OUTPUT_LIMIT = int(os.environ.get("AI_PY_OUTPUT", "20000"))
RESULT_ROWS = int(os.environ.get("AI_PY_RESULT_ROWS", "500"))

ALLOWED_MODULES = {
    "pandas", "numpy", "math", "statistics", "json", "datetime", "re", "itertools",
    "collections", "functools", "operator", "decimal", "fractions", "calendar", "bisect",
    "heapq", "string", "textwrap", "copy", "numbers", "typing", "dataclasses", "scipy",
    "dateutil", "pytz", "zoneinfo", "warnings", "analytics",
}

PROJECT_ROOT = Path(__file__).resolve().parents[3]

RUNNER = r'''
import json, sys, io, builtins, traceback
ROOT, INPUT, OUTPUT = sys.argv[1], sys.argv[2], sys.argv[3]
CPU_S, MEMORY_MB, OUTPUT_LIMIT, RESULT_ROWS = int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]), int(sys.argv[7])
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S, CPU_S + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000, 1_000_000))
    try:
        limit = MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass
except Exception:
    pass
sys.path.insert(0, ROOT)
import math, statistics, datetime, re, itertools, collections, functools
import numpy as np
import pandas as pd
from backend.ai.agent import analytics

with open(INPUT, encoding="utf-8") as fh:
    payload = json.load(fh)

ALLOWED = set(payload["allowed"])
_real_import = builtins.__import__

def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    frame = sys._getframe(1)
    if frame.f_code.co_filename == "<agent>":
        top = (name or "").split(".")[0]
        if top not in ALLOWED:
            raise ImportError(f"импорт «{name}» в песочнице запрещён; доступны: pandas, numpy, math, statistics и стандартные утилиты")
    return _real_import(name, globals, locals, fromlist, level)

SAFE_BUILTINS = {k: getattr(builtins, k) for k in (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min",
    "next", "pow", "print", "range", "repr", "reversed", "round", "set", "slice", "sorted", "str",
    "sum", "tuple", "zip", "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
    "Exception", "ArithmeticError", "StopIteration", "True", "False", "None", "hasattr", "getattr",
    "callable", "chr", "ord", "hash", "id", "type", "object", "NotImplemented", "Ellipsis",
)}
SAFE_BUILTINS["__import__"] = _guarded_import

namespace = {"__builtins__": SAFE_BUILTINS, "pd": pd, "np": np, "math": math,
             "statistics": statistics, "datetime": datetime, "re": re, "json": json,
             "itertools": itertools, "collections": collections, "functools": functools,
             "result": None}
namespace.update(analytics.HELPERS)
import re as _re
ID_NAME_RE = _re.compile(r"номер|кссс|ksss|\bкод|code|\bid\b", _re.I)

def _looks_like_identifier(name, series):
    if ID_NAME_RE.search(str(name)):
        return True
    strings = [v for v in series.dropna().tolist()[:50] if isinstance(v, str)]
    # Ведущий ноль — код, а не число: «02098».
    return any(len(v) > 1 and v[0] == "0" and v.isdigit() for v in strings)

for rid, table in payload["inputs"].items():
    frame_df = pd.DataFrame(table["rows"], columns=table["columns"])
    for column in frame_df.columns:
        if _looks_like_identifier(column, frame_df[column]):
            frame_df[column] = frame_df[column].astype("string")
            continue
        converted = pd.to_numeric(frame_df[column], errors="coerce")
        # Числовая колонка, если почти все непустые значения приводятся к числу.
        filled = frame_df[column].notna().sum()
        if filled and converted.notna().sum() >= 0.9 * filled:
            frame_df[column] = converted
    namespace[rid] = frame_df

out = io.StringIO()
real_stdout = sys.stdout
sys.stdout = out
error = None
try:
    code = compile(payload["code"], "<agent>", "exec")
    exec(code, namespace)
except SystemExit:
    pass
except BaseException as exc:
    tb = traceback.extract_tb(exc.__traceback__)
    where = next((f"строка {f.lineno}" for f in reversed(tb) if f.filename == "<agent>"), "")
    error = f"{type(exc).__name__}: {exc}" + (f" ({where})" if where else "")
finally:
    sys.stdout = real_stdout

def to_table(value):
    if value is None:
        return None
    if isinstance(value, pd.DataFrame):
        df = value.head(RESULT_ROWS).reset_index() if not isinstance(value.index, pd.RangeIndex) else value.head(RESULT_ROWS)
        cols = [str(c) for c in df.columns]
        rows = json.loads(df.to_json(orient="values", date_format="iso", default_handler=str))
        return {"columns": cols, "rows": rows, "truncated": len(value) > RESULT_ROWS}
    if isinstance(value, pd.Series):
        df = value.reset_index()
        df.columns = [str(c) for c in df.columns]
        return to_table(df)
    if isinstance(value, dict):
        if value and all(isinstance(v, (list, tuple)) for v in value.values()):
            try:
                return to_table(pd.DataFrame(value))
            except Exception:
                pass
        flat = json.loads(json.dumps(value, default=str))
        return {"columns": ["показатель", "значение"],
                "rows": [[k, v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)] for k, v in flat.items()],
                "truncated": False, "object": flat}
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, dict) for v in value):
            try:
                return to_table(pd.DataFrame(list(value)))
            except Exception:
                pass
        return {"columns": ["значение"], "rows": [[json.loads(json.dumps(v, default=str))] for v in list(value)[:RESULT_ROWS]], "truncated": len(value) > RESULT_ROWS}
    return {"columns": ["значение"], "rows": [[json.loads(json.dumps(value, default=str))]], "truncated": False}

result_table = None
result_error = None
try:
    result_table = to_table(namespace.get("result"))
except Exception as err:
    result_error = f"result не удалось сериализовать: {err}"

text = out.getvalue()
if len(text) > OUTPUT_LIMIT:
    text = text[:OUTPUT_LIMIT] + "\n… (вывод обрезан)"
with open(OUTPUT, "w", encoding="utf-8") as fh:
    json.dump({"stdout": text, "error": error or result_error, "result": result_table}, fh, ensure_ascii=False, default=str)
'''


def execute(code: str, inputs: dict[str, ResultSet], timeout_s: float = TIMEOUT_S) -> dict:
    """Запустить код в подпроцессе. Возвращает stdout, ошибку и таблицу result."""
    with tempfile.TemporaryDirectory(prefix="ai-py-") as tmp:
        tmp_path = Path(tmp)
        runner = tmp_path / "runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        payload = {
            "code": code,
            "allowed": sorted(ALLOWED_MODULES),
            "inputs": {rid: {"columns": rs.columns, "rows": rs.rows} for rid, rs in inputs.items()},
        }
        (tmp_path / "in.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        out_file = tmp_path / "out.json"
        env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
               "HOME": tmp, "TMPDIR": tmp, "MPLBACKEND": "Agg", "OMP_NUM_THREADS": "2",
               "OPENBLAS_NUM_THREADS": "2"}
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(runner), str(PROJECT_ROOT), str(tmp_path / "in.json"),
                 str(out_file), str(CPU_S), str(MEMORY_MB), str(OUTPUT_LIMIT), str(RESULT_ROWS)],
                capture_output=True, text=True, timeout=timeout_s, env=env, cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return {"stdout": "", "error": f"расчёт остановлен по времени ({timeout_s:.0f} с)",
                    "result": None, "elapsed_ms": int((time.monotonic() - started) * 1000)}
        elapsed = int((time.monotonic() - started) * 1000)
        if not out_file.exists():
            tail = (proc.stderr or "").strip()[-1500:]
            return {"stdout": (proc.stdout or "")[:OUTPUT_LIMIT], "error": tail or f"процесс завершился с кодом {proc.returncode}",
                    "result": None, "elapsed_ms": elapsed}
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            return {"stdout": "", "error": f"вывод не прочитан: {err}", "result": None, "elapsed_ms": elapsed}
        data["elapsed_ms"] = elapsed
        return data


@tool(
    "run_python",
    "Выполнить Python над результатами rN (они доступны как pandas DataFrame с теми же именами). "
    "Есть pd, np, math и готовые функции: pct_change, trend, seasonality, outliers, decompose, pareto, "
    "describe, correlation, regression, elasticity, whatif_lift, forecast_ab. "
    "Печатай выводы print(); таблицу или словарь для ответа положи в переменную result — она станет набором pN. "
    "Файлы, сеть и база недоступны.",
    {"type": "object",
     "properties": {
         "code": {"type": "string", "description": "код Python"},
         "inputs": {"type": "array", "items": {"type": "string"}, "description": "какие результаты нужны, например [\"r1\", \"r2\"]"},
         "purpose": {"type": "string", "description": "что считаем, коротко и по-русски"},
     },
     "required": ["code", "purpose"]},
    kind="python",
)
def run_python(ctx: ToolContext, code: str, purpose: str = "", inputs: list[str] | None = None) -> dict:
    budget = ctx.budget
    if budget.used_python >= budget.python_calls:
        raise ToolError(f"лимит вычислений Python исчерпан ({budget.python_calls}); переходи к finish")
    budget.used_python += 1
    wanted = [i.strip().lower() for i in (inputs or []) if i and i.strip()]
    if not wanted:
        wanted = list(ctx.workspace.results)
    frames = {}
    for rid in wanted:
        try:
            frames[rid] = ctx.workspace.get(rid)
        except KeyError as err:
            raise ToolError(str(err)) from err
    short = " ".join((purpose or "").split())[:90] or "вычисления"
    step = Step(key=ctx.workspace.next_id("s"), kind="python", label=f"Считаю: {short}", purpose=purpose, code=code)
    ctx.workspace.steps.append(step)
    ctx.emit(step, "active")
    outcome = execute(textwrap.dedent(code), frames, timeout_s=ctx.python_timeout_s or TIMEOUT_S)
    step.ms = outcome.get("elapsed_ms", 0)
    step.output = (outcome.get("stdout") or "")[:4000]
    if outcome.get("error"):
        step.ok = False
        step.error = str(outcome["error"])[-1500:]
        step.label = f"Расчёт не удался: {short}"
        ctx.emit(step, "failed")
        return {"error": step.error, "stdout": step.output,
                "hint": "Исправь код: проверь имена колонок (они как в результате rN, с русскими псевдонимами) и типы."}
    response: dict = {"stdout": step.output}
    table = outcome.get("result")
    if table and table.get("columns"):
        rs = ResultSet(id=ctx.workspace.next_id("p"), columns=[str(c) for c in table["columns"]],
                       rows=table["rows"], source="python", purpose=purpose,
                       truncated=bool(table.get("truncated")), elapsed_ms=step.ms)
        ctx.workspace.add(rs)
        step.result_id = rs.id
        step.rows = rs.row_count
        response["result"] = rs.preview(limit=15)
        if table.get("object") is not None:
            response["result"]["object"] = table["object"]
    step.label = f"Посчитал: {short}"
    ctx.emit(step, "done")
    return response
