"""Аналитические примитивы для песочницы run_python.

Функции получают pandas.DataFrame из результатов SQL и возвращают словари
или DataFrame — то, что модель может положить в `result`. Всё, что здесь
считается, детерминировано и проверяемо: модель выбирает, что применить,
а формулы не придумывает.

Доступны в песочнице по имени без импорта: pct_change, trend, seasonality,
outliers, decompose, pareto, describe, correlation, regression, elasticity,
whatif_lift, forecast_ab.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _f(value: Any, digits: int = 2) -> float | None:
    try:
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def pct_change(new: Any, old: Any) -> dict:
    """Абсолютное и относительное изменение new к old."""
    try:
        new_f, old_f = float(new), float(old)
    except (TypeError, ValueError):
        return {"abs": None, "pct": None}
    pct = None if old_f == 0 else (new_f / old_f - 1.0) * 100.0
    return {"new": _f(new_f), "old": _f(old_f), "abs": _f(new_f - old_f), "pct": _f(pct, 1)}


def trend(df: pd.DataFrame, x: str, y: str, window: int = 3) -> dict:
    """Общий тренд ряда: начало/конец, минимум/максимум, наклон, переломы, скользящее."""
    data = df[[x, y]].copy()
    data[y] = _num(data[y])
    data = data.dropna().sort_values(x).reset_index(drop=True)
    if data.empty:
        return {"points": 0}
    values = data[y].to_numpy(dtype=float)
    labels = data[x].astype(str).tolist()
    n = len(values)
    slope = float(np.polyfit(np.arange(n), values, 1)[0]) if n >= 2 else 0.0
    mean = float(values.mean()) if n else 0.0
    first, last = float(values[0]), float(values[-1])
    total = pct_change(last, first)
    steps = np.diff(values) if n >= 2 else np.array([])
    step_pct = [
        (labels[i + 1], _f(steps[i]), _f(100.0 * steps[i] / values[i], 1) if values[i] else None)
        for i in range(len(steps))
    ]
    breaks = sorted(step_pct, key=lambda item: -abs(item[1] or 0))[:3]
    rolling = pd.Series(values).rolling(window, min_periods=1).mean().round(2).tolist()
    cagr = None
    if n >= 2 and first > 0 and last > 0:
        cagr = _f((math.pow(last / first, 1.0 / (n - 1)) - 1.0) * 100.0, 2)
    return {
        "points": n,
        "first": {"x": labels[0], "y": _f(first)},
        "last": {"x": labels[-1], "y": _f(last)},
        "min": {"x": labels[int(values.argmin())], "y": _f(values.min())},
        "max": {"x": labels[int(values.argmax())], "y": _f(values.max())},
        "mean": _f(mean),
        "total_change": total,
        "slope_per_step": _f(slope),
        "slope_pct_of_mean": _f(100.0 * slope / mean, 2) if mean else None,
        "avg_step_growth_pct": cagr,
        "direction": "рост" if slope > 0.005 * abs(mean) else ("снижение" if slope < -0.005 * abs(mean) else "стабильно"),
        "largest_moves": [{"x": a, "abs": b, "pct": c} for a, b, c in breaks],
        "rolling": [{"x": labels[i], "y": rolling[i]} for i in range(n)],
    }


def seasonality(df: pd.DataFrame, x: str, y: str, period: int = 12) -> dict:
    """Сезонный профиль: среднее по позиции в цикле относительно общего среднего."""
    data = df[[x, y]].copy()
    data[y] = _num(data[y])
    data = data.dropna().sort_values(x).reset_index(drop=True)
    if len(data) < period:
        return {"enough": False, "points": len(data), "period": period}
    mean = float(data[y].mean())
    data["pos"] = np.arange(len(data)) % period
    profile = data.groupby("pos")[y].mean()
    detrended = data[y] - data[y].rolling(period, center=True, min_periods=1).mean()
    return {
        "enough": True,
        "period": period,
        "index": [{"pos": int(p), "index": _f(v / mean, 3) if mean else None} for p, v in profile.items()],
        "amplitude_pct": _f(100.0 * (profile.max() - profile.min()) / mean, 1) if mean else None,
        "residual_std": _f(float(detrended.std())),
    }


def outliers(df: pd.DataFrame, value: str, group: Iterable[str] | str | None = None,
             method: str = "iqr", threshold: float = 1.5, side: str = "both") -> pd.DataFrame:
    """Выбросы относительно сопоставимой группы: медиана, отклонение, флаг.

    method: iqr (межквартильный размах), z (стандартное отклонение), pct (перцентили).
    side: low — только хуже группы, high — только лучше, both — оба.
    """
    data = df.copy()
    data[value] = _num(data[value])
    if isinstance(group, str):
        group = [group]
    keys = list(group or [])
    if keys:
        grouped = data.groupby(keys)[value]
        data["group_median"] = grouped.transform("median")
        data["group_n"] = grouped.transform("count")
        q1 = grouped.transform(lambda s: s.quantile(0.25))
        q3 = grouped.transform(lambda s: s.quantile(0.75))
        std = grouped.transform("std")
        p10 = grouped.transform(lambda s: s.quantile(0.10))
        p90 = grouped.transform(lambda s: s.quantile(0.90))
    else:
        data["group_median"] = data[value].median()
        data["group_n"] = data[value].count()
        q1, q3 = data[value].quantile(0.25), data[value].quantile(0.75)
        std = data[value].std()
        p10, p90 = data[value].quantile(0.10), data[value].quantile(0.90)
    data["deviation"] = data[value] - data["group_median"]
    data["deviation_pct"] = np.where(
        data["group_median"] != 0, 100.0 * data["deviation"] / data["group_median"], np.nan
    )
    if method == "z":
        data["score"] = np.where(std > 0, data["deviation"] / std, 0.0)
        low = data["score"] < -threshold
        high = data["score"] > threshold
    elif method == "pct":
        data["score"] = data["deviation_pct"]
        low = data[value] < p10
        high = data[value] > p90
    else:
        iqr = q3 - q1
        data["score"] = np.where(iqr > 0, data["deviation"] / iqr, 0.0)
        low = data[value] < q1 - threshold * iqr
        high = data[value] > q3 + threshold * iqr
    if side == "low":
        flag = low
    elif side == "high":
        flag = high
    else:
        flag = low | high
    data["outlier"] = flag
    data["direction"] = np.where(low, "ниже группы", np.where(high, "выше группы", ""))
    for col in ("group_median", "deviation", "deviation_pct", "score"):
        data[col] = data[col].astype(float).round(2)
    return data.sort_values("score").reset_index(drop=True)


def _log_mean(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return (a + b) / 2.0
    if a == b:
        return a
    return (a - b) / (math.log(a) - math.log(b))


def decompose(base: dict, current: dict, factors: list[str], total: str | None = None) -> dict:
    """Разложение изменения показателя = произведение факторов (LMDI).

    base/current — словари {фактор: значение} за два периода; произведение
    факторов должно давать показатель (например, трафик = АЗС × чеков на АЗС).
    Возвращает вклад каждого фактора в абсолютное изменение и в процентах.
    """
    b_total = float(np.prod([float(base[f]) for f in factors])) if total is None else float(base[total])
    c_total = float(np.prod([float(current[f]) for f in factors])) if total is None else float(current[total])
    change = c_total - b_total
    weight = _log_mean(c_total, b_total)
    parts = []
    explained = 0.0
    for name in factors:
        b, c = float(base[name]), float(current[name])
        if b > 0 and c > 0:
            contribution = weight * math.log(c / b)
        else:
            contribution = 0.0
        explained += contribution
        parts.append({
            "factor": name, "base": _f(b), "current": _f(c),
            "change_pct": _f((c / b - 1) * 100.0, 1) if b else None,
            "contribution": _f(contribution),
            "share_of_change_pct": _f(100.0 * contribution / change, 1) if change else None,
        })
    return {
        "total_base": _f(b_total), "total_current": _f(c_total),
        "change": _f(change), "change_pct": _f((c_total / b_total - 1) * 100.0, 1) if b_total else None,
        "factors": parts,
        "residual": _f(change - explained),
        "note": "Вклады показывают, какие драйверы двигались вместе с показателем; это разложение, а не доказательство причины.",
    }


def pareto(df: pd.DataFrame, label: str, value: str, share: float = 0.8, ascending: bool = True) -> dict:
    """Концентрация эффекта: сколько объектов дают долю share от суммы отрицательных (или положительных) значений."""
    data = df[[label, value]].copy()
    data[value] = _num(data[value])
    data = data.dropna()
    subset = data[data[value] < 0] if ascending else data[data[value] > 0]
    if subset.empty:
        return {"objects": 0, "total": 0.0, "top": []}
    subset = subset.sort_values(value, ascending=ascending).reset_index(drop=True)
    total = float(subset[value].sum())
    subset["cum_share"] = subset[value].cumsum() / total
    cutoff = int((subset["cum_share"] < share).sum()) + 1
    top = subset.head(cutoff)
    return {
        "objects": int(len(subset)),
        "objects_all": int(len(data)),
        "total": _f(total),
        "count_for_share": int(cutoff),
        "share": share,
        "top": [{"label": str(r[label]), "value": _f(r[value]), "cum_share": _f(r["cum_share"], 3)}
                for _, r in top.iterrows()],
    }


def describe(df: pd.DataFrame, value: str) -> dict:
    s = _num(df[value]).dropna()
    if s.empty:
        return {"count": 0}
    return {
        "count": int(s.count()), "mean": _f(s.mean()), "median": _f(s.median()),
        "std": _f(s.std()), "min": _f(s.min()), "max": _f(s.max()),
        "p10": _f(s.quantile(0.1)), "p25": _f(s.quantile(0.25)),
        "p75": _f(s.quantile(0.75)), "p90": _f(s.quantile(0.9)),
    }


def correlation(df: pd.DataFrame, x: str, y: str) -> dict:
    data = df[[x, y]].apply(_num).dropna()
    if len(data) < 3:
        return {"n": int(len(data)), "pearson": None}
    r = float(data[x].corr(data[y]))
    return {"n": int(len(data)), "pearson": _f(r, 3),
            "strength": "сильная" if abs(r) >= 0.7 else ("умеренная" if abs(r) >= 0.4 else "слабая"),
            "note": "Корреляция не доказывает причинность."}


def regression(df: pd.DataFrame, x: str, y: str) -> dict:
    data = df[[x, y]].apply(_num).dropna()
    if len(data) < 3:
        return {"n": int(len(data))}
    xs, ys = data[x].to_numpy(dtype=float), data[y].to_numpy(dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    pred = slope * xs + intercept
    ss_res = float(((ys - pred) ** 2).sum())
    ss_tot = float(((ys - ys.mean()) ** 2).sum())
    return {"n": int(len(data)), "slope": _f(slope, 4), "intercept": _f(intercept),
            "r2": _f(1 - ss_res / ss_tot, 3) if ss_tot else None}


def elasticity(df: pd.DataFrame, price: str, quantity: str) -> dict:
    """Ценовая эластичность: наклон в логарифмах."""
    data = df[[price, quantity]].apply(_num).dropna()
    data = data[(data[price] > 0) & (data[quantity] > 0)]
    if len(data) < 3:
        return {"n": int(len(data)), "elasticity": None}
    slope = np.polyfit(np.log(data[price]), np.log(data[quantity]), 1)[0]
    return {"n": int(len(data)), "elasticity": _f(slope, 3),
            "note": "Оценка по наблюдаемым данным без контроля прочих факторов."}


def whatif_lift(df: pd.DataFrame, value: str, target: float | str, base: str | None = None,
                label: str | None = None, group: Iterable[str] | str | None = None,
                percent: bool | None = None) -> dict:
    """Сценарий: поднять показатель до целевого уровня там, где он ниже.

    value — показатель-отношение (например, конверсия), base — база, к которой
    он применяется (например, чеки): эффект = (target − value) × base.
    target — число или имя агрегата по группе: 'median', 'p75', 'mean'.
    """
    data = df.copy()
    data[value] = _num(data[value])
    if base:
        data[base] = _num(data[base])
    if isinstance(group, str):
        group = [group]
    keys = list(group or [])
    if isinstance(target, str):
        agg = {"median": "median", "mean": "mean"}.get(target)
        if agg:
            data["target"] = data.groupby(keys)[value].transform(agg) if keys else data[value].agg(agg)
        elif target.startswith("p") and target[1:].isdigit():
            q = int(target[1:]) / 100.0
            data["target"] = (data.groupby(keys)[value].transform(lambda s: s.quantile(q))
                              if keys else data[value].quantile(q))
        else:
            raise ValueError("target: число, 'median', 'mean' или 'pNN'")
    else:
        data["target"] = float(target)
    below = data[data[value] < data["target"]].copy()
    below["gap"] = below["target"] - below[value]
    if base:
        below["effect"] = below["gap"] * below[base]
        if percent is None:
            lowered = value.lower()
            percent = "%" in lowered or "конверс" in lowered or "доля" in lowered
        if percent:
            # Проценты применяются к базе как доли.
            below["effect"] = below["effect"] / 100.0
    else:
        below["effect"] = below["gap"]
    total_effect = float(below["effect"].sum()) if not below.empty else 0.0
    baseline = float(data[base].sum()) if base else float(data[value].sum())
    rows = below.sort_values("effect", ascending=False)
    cols = ([label] if label else []) + [value, "target", "gap", "effect"]
    return {
        "objects_below": int(len(below)), "objects_all": int(len(data)),
        "baseline": _f(baseline), "effect": _f(total_effect),
        "effect_pct_of_baseline": _f(100.0 * total_effect / baseline, 1) if baseline else None,
        "assumptions": [
            f"целевой уровень: {target}" + (f" по группе {keys}" if keys else ""),
            "прочие условия неизменны; эффект линейный, без учёта ёмкости спроса",
        ],
        "top": rows[cols].head(15).round(2).to_dict(orient="records"),
    }


def forecast_ab(fact_mtd: float, days_passed: int, days_in_month: int,
                ly_full: float | None = None, ly_same_days: float | None = None,
                plan: float | None = None) -> dict:
    """Прогноз на месяц по методике: метод А (темп) и метод Б (профиль прошлого года)."""
    if days_passed < 8:
        return {"allowed": False, "reason": "прогноз не строится в первые 7 дней месяца (методика, раздел 5)"}
    if days_passed <= 0 or days_in_month <= 0:
        return {"allowed": False, "reason": "неверные дни"}
    pace = fact_mtd / days_passed
    method_a = pace * days_in_month
    out = {
        "allowed": True,
        "fact_mtd": _f(fact_mtd), "days_passed": days_passed, "days_in_month": days_in_month,
        "pace_per_day": _f(pace),
        "method_a": _f(method_a),
        "method_b": None, "k_catchup": None,
        "note": "Метод Б основной, метод А — нижняя граница; результат — расчётная оценка, не обещание.",
    }
    if ly_full and ly_same_days:
        k = ly_full / ly_same_days
        out["k_catchup"] = _f(k, 3)
        out["method_b"] = _f(fact_mtd * k)
    else:
        out["note"] += " Сопоставимого периода прошлого года нет — остаётся только нижняя граница."
    if plan:
        out["plan"] = _f(plan)
        out["method_a_pct_plan"] = _f(100.0 * method_a / plan, 1)
        if out["method_b"]:
            out["method_b_pct_plan"] = _f(100.0 * out["method_b"] / plan, 1)
        remaining_days = days_in_month - days_passed
        if remaining_days > 0:
            need = (plan - fact_mtd) / remaining_days
            out["required_pace"] = _f(need)
            out["required_pace_vs_current_pct"] = _f((need / pace - 1) * 100.0, 1) if pace else None
    return out


HELPERS = {
    name: obj for name, obj in globals().items()
    if callable(obj) and not name.startswith("_") and name not in {"Any", "Iterable"}
}
