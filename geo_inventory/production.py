from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Iterable


PRODUCTION_FIELD_CONTRACT = [
    {"key": "well", "label": "井名", "required": True, "examples": ["WELL", "WELL_NAME"]},
    {"key": "production_month", "label": "生产月份", "required": False, "examples": ["DATE", "PROD_MONTH"]},
    {"key": "onstream_date", "label": "投产时间", "required": False, "examples": ["FIRST_PRODUCTION_DATE"]},
    {"key": "days_on", "label": "开井天数", "required": False, "examples": ["DAYS_ON"]},
    {"key": "liquid_rate", "label": "日产液", "required": False, "examples": ["LIQUID_RATE", "QL"]},
    {"key": "oil_rate", "label": "日产油", "required": False, "examples": ["OIL_RATE", "QO"]},
    {"key": "water_rate", "label": "日产水", "required": False, "examples": ["WATER_RATE", "QW"]},
    {"key": "gas_rate", "label": "日产气", "required": False, "examples": ["GAS_RATE", "QG"]},
    {"key": "water_cut", "label": "含水率", "required": False, "examples": ["WATER_CUT", "WCT"]},
    {"key": "cumulative_oil", "label": "累产油", "required": False, "examples": ["CUM_OIL", "NP"]},
    {"key": "cumulative_water", "label": "累产水", "required": False, "examples": ["CUM_WATER", "WP"]},
    {"key": "cumulative_gas", "label": "累产气", "required": False, "examples": ["CUM_GAS", "GP"]},
    {"key": "pressure", "label": "压力", "required": False, "examples": ["BHP", "THP", "PRESSURE"]},
    {"key": "status", "label": "开关井状态", "required": False, "examples": ["STATUS", "OPEN_STATUS"]},
]


# These labels are shared by the production page, relationship search and the
# on-demand chart endpoint.  Values are produced for one selected well only;
# no full OFM history is held in browser memory.
PRODUCTION_SERIES_FIELDS = [
    {"key": "liquid_rate", "label": "日产液", "unit": "原表日率"},
    {"key": "oil_rate", "label": "日产油", "unit": "原表日率"},
    {"key": "water_rate", "label": "日产水", "unit": "原表日率"},
    {"key": "gas_rate", "label": "日产气", "unit": "原表日率"},
    {"key": "gas_oil_ratio", "label": "汽油比", "unit": "scf/bbl"},
    {"key": "water_cut", "label": "含水率", "unit": "%"},
    {"key": "monthly_oil", "label": "月产油", "unit": "原表体积"},
    {"key": "monthly_water", "label": "月产水", "unit": "原表体积"},
    {"key": "monthly_gas", "label": "月产气", "unit": "原表体积"},
    {"key": "monthly_liquid", "label": "月产液", "unit": "原表体积"},
    {"key": "cumulative_oil", "label": "累产油", "unit": "原表体积"},
    {"key": "cumulative_water", "label": "累产水", "unit": "原表体积"},
    {"key": "cumulative_gas", "label": "累产气", "unit": "原表体积"},
    {"key": "cumulative_liquid", "label": "累产液", "unit": "原表体积"},
    {"key": "pressure", "label": "压力", "unit": "原表压力单位"},
    {"key": "days_on", "label": "生产天数", "unit": "天"},
]

PRODUCTION_X_AXES = [
    {"key": "date", "label": "日期 / 年月日"},
    {"key": "month_index", "label": "有效生产月序号"},
    {"key": "cumulative_oil", "label": "累产油"},
    {"key": "cumulative_water", "label": "累产水"},
    {"key": "cumulative_liquid", "label": "累产液"},
    {"key": "cumulative_days", "label": "累计生产天数"},
]


def _date_key(value: Any) -> tuple:
    text = str(value or "")
    parts = [int(part) for part in re.findall(r"\d+", text)[:3]]
    parts.extend([99] * (3 - len(parts)))
    return (*parts[:3], text) if text else (9999, 99, 99, text)


def _latest_value(rows: list[dict[str, Any]], field: str) -> float | None:
    return next((row[field] for row in reversed(rows) if row.get(field) is not None), None)


def _normalized_water_cut(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100 if 0 <= value <= 1 else value


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def build_production_series(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consolidate a selected well's production/pressure rows by date.

    OFM imports can hold an oil/water record and a pressure test as separate
    rows for the same date.  This preserves the last non-null value for every
    source field, then derives cumulative values from monthly volumes only
    where an explicit cumulative series is absent.  It deliberately accepts
    only one well's queried rows.
    """
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in rows:
        item = dict(source)
        date = str(item.get("production_month") or "").strip()
        if date:
            by_date[date].append(item)
    series: list[dict[str, Any]] = []
    cumulative_oil = cumulative_water = cumulative_gas = cumulative_days = 0.0
    explicit_oil = explicit_water = explicit_gas = None
    for month_index, date in enumerate(sorted(by_date, key=_date_key), 1):
        records = sorted(by_date[date], key=lambda row: int(row.get("id") or 0))
        point: dict[str, Any] = {"date": date, "month_index": month_index}
        for field in ("days_on", "liquid_rate", "oil_rate", "water_rate", "gas_rate", "water_cut", "monthly_oil", "monthly_water", "monthly_gas", "pressure", "status"):
            value = next((row.get(field) for row in reversed(records) if row.get(field) is not None), None)
            point[field] = _number(value) if field != "status" else value
        point["water_cut"] = _normalized_water_cut(point.get("water_cut"))
        if point.get("liquid_rate") is None and any(point.get(name) is not None for name in ("oil_rate", "water_rate")):
            point["liquid_rate"] = sum(point.get(name) or 0.0 for name in ("oil_rate", "water_rate"))
        if point.get("water_cut") is None and point.get("liquid_rate") not in (None, 0) and point.get("water_rate") is not None:
            point["water_cut"] = point["water_rate"] / point["liquid_rate"] * 100
        point["gas_oil_ratio"] = point["gas_rate"] / point["oil_rate"] if point.get("gas_rate") is not None and point.get("oil_rate") not in (None, 0) else None
        point["monthly_liquid"] = sum(point.get(name) or 0.0 for name in ("monthly_oil", "monthly_water")) if any(point.get(name) is not None for name in ("monthly_oil", "monthly_water")) else None
        cumulative_oil += point.get("monthly_oil") or 0.0
        cumulative_water += point.get("monthly_water") or 0.0
        cumulative_gas += point.get("monthly_gas") or 0.0
        cumulative_days += point.get("days_on") or 0.0
        raw_cum_oil = next((_number(row.get("cumulative_oil")) for row in reversed(records) if _number(row.get("cumulative_oil")) is not None), None)
        raw_cum_water = next((_number(row.get("cumulative_water")) for row in reversed(records) if _number(row.get("cumulative_water")) is not None), None)
        raw_cum_gas = next((_number(row.get("cumulative_gas")) for row in reversed(records) if _number(row.get("cumulative_gas")) is not None), None)
        explicit_oil = raw_cum_oil if raw_cum_oil is not None else explicit_oil
        explicit_water = raw_cum_water if raw_cum_water is not None else explicit_water
        explicit_gas = raw_cum_gas if raw_cum_gas is not None else explicit_gas
        point["cumulative_oil"] = explicit_oil if explicit_oil is not None else cumulative_oil
        point["cumulative_water"] = explicit_water if explicit_water is not None else cumulative_water
        point["cumulative_gas"] = explicit_gas if explicit_gas is not None else cumulative_gas
        point["cumulative_liquid"] = point["cumulative_oil"] + point["cumulative_water"]
        point["cumulative_days"] = cumulative_days
        series.append(point)
    return series


def _linear_regression(points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    if len(points) < 2:
        return None
    count = float(len(points))
    x_mean = sum(point[0] for point in points) / count
    y_mean = sum(point[1] for point in points) / count
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    if denominator <= 1e-12:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator
    intercept = y_mean - slope * x_mean
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in points)
    return intercept, slope, sse


def fit_hyperbolic_decline(series: Iterable[dict[str, Any]], metric: str) -> dict[str, Any]:
    """Fit exponential/hyperbolic decline to a positive rate series.

    Uses a deterministic grid search over b=0–2 and least-squares in the
    transformed domain.  It is an initial screening fit, not a reserves
    forecast or a replacement for OFM DCA case settings.
    """
    source = list(series)
    points = [(float(index), _number(row.get(metric))) for index, row in enumerate(source) if (_number(row.get(metric)) or 0) > 0]
    if len(points) < 4:
        return {"available": False, "reason": "至少需要 4 个正值生产期才能进行递减拟合", "metric": metric, "sample_count": len(points)}
    candidates: list[dict[str, Any]] = []
    for step in range(0, 201):
        b = step / 100
        transformed = [(time, math.log(rate) if b == 0 else rate ** (-b)) for time, rate in points]
        fit = _linear_regression(transformed)
        if not fit:
            continue
        intercept, slope, _ = fit
        if b == 0:
            if slope >= 0:
                continue
            qi, decline = math.exp(intercept), -slope
            predicted = [qi * math.exp(-decline * time) for time, _ in points]
        else:
            if intercept <= 0 or slope < 0:
                continue
            qi = intercept ** (-1 / b)
            decline = slope / (b * intercept)
            predicted = [qi / ((1 + b * decline * time) ** (1 / b)) for time, _ in points]
        observed = [rate for _, rate in points]
        mean_value = sum(observed) / len(observed)
        sse = sum((actual - estimate) ** 2 for actual, estimate in zip(observed, predicted))
        sst = sum((actual - mean_value) ** 2 for actual in observed)
        r_squared = 1 - sse / sst if sst > 1e-12 else None
        candidates.append({"b": b, "qi": qi, "decline_per_month": decline, "r_squared": r_squared, "sse": sse, "predicted": predicted})
    if not candidates:
        return {"available": False, "reason": "该序列未呈现可用于单段递减模型的正向衰减趋势", "metric": metric, "sample_count": len(points)}
    best = min(candidates, key=lambda row: row["sse"])
    return {
        "available": True, "metric": metric, "sample_count": len(points),
        "model": "指数递减" if best["b"] == 0 else "双曲递减",
        "b": round(best["b"], 3), "qi": best["qi"],
        "decline_per_month": best["decline_per_month"],
        "r_squared": best["r_squared"] if best["r_squared"] is None else round(best["r_squared"], 4),
        "points": [{"month_index": int(time) + 1, "observed": rate, "fitted": fitted} for (time, rate), fitted in zip(points, best["predicted"])],
        "note": "采用 b=0–2 的单段网格拟合；仅用于初步核查递减形态。存在措施、关井、补孔、注采调整或多段开发时，应分段拟合并由工程人员复核。",
    }


def summarize_production(
    monthly_rows: Iterable[dict[str, Any]],
    event_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build explainable per-well summaries without changing source values."""

    monthly_by_well: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by_well: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in monthly_rows:
        monthly_by_well[str(row.get("well_key") or "")].append(dict(row))
    for row in event_rows:
        events_by_well[str(row.get("well_key") or "")].append(dict(row))

    result = []
    for key in sorted(set(monthly_by_well) | set(events_by_well)):
        rows = sorted(monthly_by_well[key], key=lambda row: _date_key(row.get("production_month")))
        events = sorted(events_by_well[key], key=lambda row: _date_key(row.get("event_date")))
        name = next((row.get("well_name") for row in rows + events if row.get("well_name")), key)
        initial = next((row for row in rows if any(row.get(field) is not None for field in ("liquid_rate", "oil_rate", "water_rate", "monthly_oil", "monthly_water"))), rows[0] if rows else {})
        days = initial.get("days_on")
        oil_rate = initial.get("oil_rate")
        water_rate = initial.get("water_rate")
        liquid_rate = initial.get("liquid_rate")
        if oil_rate is None and initial.get("monthly_oil") is not None and days:
            oil_rate = initial["monthly_oil"] / days
        if water_rate is None and initial.get("monthly_water") is not None and days:
            water_rate = initial["monthly_water"] / days
        if liquid_rate is None and oil_rate is not None and water_rate is not None:
            liquid_rate = oil_rate + water_rate
        water_cut = _normalized_water_cut(initial.get("water_cut"))
        if water_cut is None and liquid_rate not in (None, 0) and water_rate is not None:
            water_cut = water_rate / liquid_rate * 100
        cumulative_oil = _latest_value(rows, "cumulative_oil")
        cumulative_water = _latest_value(rows, "cumulative_water")
        if cumulative_oil is None and any(row.get("monthly_oil") is not None for row in rows):
            cumulative_oil = sum(row.get("monthly_oil") or 0 for row in rows)
        if cumulative_water is None and any(row.get("monthly_water") is not None for row in rows):
            cumulative_water = sum(row.get("monthly_water") or 0 for row in rows)
        pressures = [row["pressure"] for row in rows if row.get("pressure") is not None]
        onstream = next((row.get("event_date") for row in events if "投产" in str(row.get("event_type") or "")), None)
        if not onstream:
            onstream = next((row.get("production_month") for row in rows if row.get("production_month")), None)
        status_events = [row for row in events if row.get("event_type") or row.get("status")]
        result.append({
            "well_key": key,
            "well_name": name,
            "onstream_date": onstream,
            "first_production_month": initial.get("production_month"),
            "last_production_month": next((row.get("production_month") for row in reversed(rows) if row.get("production_month")), None),
            "production_months": len({row.get("production_month") for row in rows if row.get("production_month")}),
            "initial_liquid_rate": liquid_rate,
            "initial_oil_rate": oil_rate,
            "initial_water_cut": water_cut,
            "cumulative_oil": cumulative_oil,
            "cumulative_water": cumulative_water,
            "pressure_first": pressures[0] if pressures else None,
            "pressure_latest": pressures[-1] if pressures else None,
            "pressure_min": min(pressures) if pressures else None,
            "pressure_max": max(pressures) if pressures else None,
            "pressure_change": pressures[-1] - pressures[0] if len(pressures) >= 2 else None,
            "event_count": len(status_events),
            "latest_status": next((row.get("status") for row in reversed(rows + events) if row.get("status")), None),
        })
    return result
