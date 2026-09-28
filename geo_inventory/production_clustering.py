from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from statistics import median
from typing import Any, Callable, Iterable

try:  # The packaged desktop runtime includes NumPy; keep a safe fallback.
    import numpy as np
except ImportError:  # pragma: no cover - exercised by minimal deployments
    np = None

from .production import build_production_series


INDICATORS = [
    {"key": "initial_3m_oil_rate", "label": "初期3个月平均日产油", "unit": "bbl/d", "group": "产能", "priority": "recommended", "direction": 1, "help": "投产后前3个有效生产期平均值，比单月初产更稳健。"},
    {"key": "peak_oil_rate", "label": "最大日产油", "unit": "bbl/d", "group": "产能", "priority": "recommended", "direction": 1, "help": "识别井的产能上限；需结合措施时间避免把措施峰值当作原始产能。"},
    {"key": "late_3m_oil_rate", "label": "末期3个月平均日产油", "unit": "bbl/d", "group": "产能", "priority": "recommended", "direction": 1, "help": "最后3个有效期的稳定油率，停产井也保留其末期表现。"},
    {"key": "oil_decline_pct", "label": "油率递减幅度", "unit": "%", "group": "趋势", "priority": "recommended", "direction": -1, "help": "初期与末期三期均值的相对变化；负值表示递增。"},
    {"key": "trend_stability", "label": "油率趋势稳定性", "unit": "%", "group": "趋势", "priority": "recommended", "direction": 1, "help": "相邻期油率变化越平稳，得分越高。"},
    {"key": "late_water_cut", "label": "末期含水率", "unit": "%", "group": "水驱", "priority": "recommended", "direction": -1, "help": "最后3个有效期平均含水率。"},
    {"key": "water_cut_growth", "label": "含水上升幅度", "unit": "百分点", "group": "水驱", "priority": "recommended", "direction": -1, "help": "末期含水减初期含水；负值表示含水下降。"},
    {"key": "producing_months", "label": "有效生产期数", "unit": "月", "group": "时长", "priority": "recommended", "direction": 1, "help": "有记录的有效生产月份数，不等同于连续日历月。"},
    {"key": "uptime_ratio", "label": "生产时率", "unit": "%", "group": "时长", "priority": "recommended", "direction": 1, "help": "开井天数除以各有效期理论天数，缺开井天数时不可用。"},
    {"key": "cumulative_oil", "label": "累计产油", "unit": "kbbl", "group": "累产", "priority": "conditional", "direction": 1, "help": "受投产早晚影响显著，建议与生产时长同时使用。"},
    {"key": "cumulative_water", "label": "累计产水", "unit": "kbbl", "group": "累产", "priority": "conditional", "direction": -1, "help": "反映水侵/水驱响应，同样受生产年限影响。"},
    {"key": "pressure_change", "label": "压力变化", "unit": "原表单位", "group": "能量", "priority": "conditional", "direction": 1, "help": "末次有效压力减首次压力；需先确认压力类型和单位一致。"},
    {"key": "intervention_response", "label": "措施后油率响应", "unit": "%", "group": "措施", "priority": "conditional", "direction": 1, "help": "措施/修井/提液/补孔事件前后各3期平均油率变化。"},
    {"key": "event_count", "label": "措施与状态事件数", "unit": "次", "group": "措施", "priority": "conditional", "direction": 0, "help": "用于识别受人为干预较强的井，不直接代表高低产。"},
    {"key": "nearest_distance", "label": "最近邻井距", "unit": "m", "group": "空间", "priority": "conditional", "direction": 0, "help": "相同主力层优先的最近邻距离，用于识别加密井和空间相近井。"},
    {"key": "perforation_length", "label": "累计射孔厚度", "unit": "m MD", "group": "层位/完井", "priority": "conditional", "direction": 1, "help": "各射孔段MD厚度求和；分层结果同时保留在井清单中。"},
    {"key": "formation_count", "label": "开发层位数", "unit": "层", "group": "层位/完井", "priority": "conditional", "direction": 0, "help": "生产层系与射孔层段去重计数，多层合采井建议单独核查。"},
    {"key": "initial_gor", "label": "初期汽油比", "unit": "scf/bbl", "group": "流体", "priority": "conditional", "direction": 0, "help": "前3个有效期日产气÷日产油的平均值；仅在原始 MDB 有可用产气字段时参与。"},
]

# Only additive, same-window production quantities are exposed to the map's
# composition glyph.  Gas is converted to barrel-of-oil equivalent before it
# is combined with liquid volumes; ratios and trend metrics are deliberately
# absent because they cannot form a physically meaningful whole.
COMPOSITION_SCHEMES = [
    {
        "key": "initial_3m_rate",
        "label": "初期3个月平均日产",
        "total_unit": "bbl-eq/d",
        "components": [
            {"phase": "oil", "label": "油", "metric_key": "initial_3m_oil_rate", "unit": "bbl/d", "color": "#2f9d78"},
            {"phase": "water", "label": "水", "metric_key": "initial_3m_water_rate", "unit": "bbl/d", "color": "#4b98d2"},
            {"phase": "gas", "label": "气当量", "metric_key": "initial_3m_gas_boe_rate", "unit": "boe/d", "color": "#e7a23e"},
        ],
    },
    {
        "key": "late_3m_rate",
        "label": "末期3个月平均日产",
        "total_unit": "bbl-eq/d",
        "components": [
            {"phase": "oil", "label": "油", "metric_key": "late_3m_oil_rate", "unit": "bbl/d", "color": "#2f9d78"},
            {"phase": "water", "label": "水", "metric_key": "late_3m_water_rate", "unit": "bbl/d", "color": "#4b98d2"},
            {"phase": "gas", "label": "气当量", "metric_key": "late_3m_gas_boe_rate", "unit": "boe/d", "color": "#e7a23e"},
        ],
    },
    {
        "key": "cumulative",
        "label": "累计产量",
        "total_unit": "kbbl-eq",
        "components": [
            {"phase": "oil", "label": "油", "metric_key": "cumulative_oil", "unit": "kbbl", "color": "#2f9d78"},
            {"phase": "water", "label": "水", "metric_key": "cumulative_water", "unit": "kbbl", "color": "#4b98d2"},
            {"phase": "gas", "label": "气当量", "metric_key": "cumulative_gas_kboe", "unit": "kboe", "color": "#e7a23e"},
        ],
    },
]

INTERVENTION_TOKENS = (
    "措施", "修井", "提液", "补孔", "压裂", "酸化", "泵", "改层", "转层",
    "workover", "stim", "fract", "acid", "perfor", "pump", "lift",
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _mean(values: Iterable[Any]) -> float | None:
    numbers = [_number(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _date_key(value: Any) -> tuple[int, int, int]:
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:3]]
    parts.extend([1] * (3 - len(parts)))
    return tuple(parts[:3])


def _event_is_intervention(row: dict[str, Any]) -> bool:
    text = f"{row.get('event_type') or ''} {row.get('status') or ''}".lower()
    return any(token in text for token in INTERVENTION_TOKENS)


def _intervention_response(series: list[dict[str, Any]], events: list[dict[str, Any]]) -> tuple[float | None, int]:
    dated = [(row, _date_key(row.get("date"))) for row in series]
    responses = []
    intervention_count = 0
    for event in events:
        if not _event_is_intervention(event):
            continue
        intervention_count += 1
        marker = _date_key(event.get("event_date"))
        before = [row for row, key in dated if key < marker][-3:]
        after = [row for row, key in dated if key >= marker][:3]
        pre, post = _mean(row.get("oil_rate") for row in before), _mean(row.get("oil_rate") for row in after)
        if pre not in (None, 0) and post is not None:
            responses.append((post - pre) / abs(pre) * 100)
    return (_mean(responses), intervention_count)


def _trend_stability(oil_rates: list[float]) -> float | None:
    if len(oil_rates) < 3:
        return None
    changes = []
    for left, right in zip(oil_rates, oil_rates[1:]):
        base = max(abs(left), 1e-9)
        changes.append(abs(right - left) / base)
    return max(0.0, 100 * (1 - min(1.0, median(changes))))


def _segment_feature_values(series: list[dict[str, Any]], inherited: dict[str, Any]) -> dict[str, Any]:
    """Recalculate time-dependent features for one auditable production segment."""
    values = dict(inherited)
    oil_rates = [float(row["oil_rate"]) for row in series if _number(row.get("oil_rate")) is not None]
    water_rates = [float(row["water_rate"]) for row in series if _number(row.get("water_rate")) is not None]
    gas_rates = [float(row["gas_rate"]) for row in series if _number(row.get("gas_rate")) is not None]
    water_cuts = [float(row["water_cut"]) for row in series if _number(row.get("water_cut")) is not None]
    gas_oil_ratios = [float(row["gas_oil_ratio"]) for row in series if _number(row.get("gas_oil_ratio")) is not None]
    initial_oil, late_oil = _mean(oil_rates[:3]), _mean(oil_rates[-3:])
    initial_water_rate, late_water_rate = _mean(water_rates[:3]), _mean(water_rates[-3:])
    initial_gas_rate, late_gas_rate = _mean(gas_rates[:3]), _mean(gas_rates[-3:])
    initial_water, late_water = _mean(water_cuts[:3]), _mean(water_cuts[-3:])
    days = [_number(row.get("days_on")) for row in series]
    valid_days = [value for value in days if value is not None]
    pressure = [_number(row.get("pressure")) for row in series]
    pressure = [value for value in pressure if value is not None]

    def segment_volume(monthly_key: str, cumulative_key: str, divisor: float = 1000.0) -> float | None:
        monthly = [_number(row.get(monthly_key)) for row in series]
        monthly = [value for value in monthly if value is not None]
        if monthly:
            return sum(monthly) / divisor
        cumulative = [_number(row.get(cumulative_key)) for row in series]
        cumulative = [value for value in cumulative if value is not None]
        if len(cumulative) >= 2:
            return max(0.0, cumulative[-1] - cumulative[0]) / divisor
        return None

    values.update({
        "initial_3m_oil_rate": initial_oil,
        "initial_3m_water_rate": initial_water_rate,
        "initial_3m_gas_boe_rate": initial_gas_rate / 6000 if initial_gas_rate is not None else None,
        "peak_oil_rate": max(oil_rates) if oil_rates else None,
        "late_3m_oil_rate": late_oil,
        "late_3m_water_rate": late_water_rate,
        "late_3m_gas_boe_rate": late_gas_rate / 6000 if late_gas_rate is not None else None,
        "oil_decline_pct": ((initial_oil - late_oil) / abs(initial_oil) * 100) if initial_oil not in (None, 0) and late_oil is not None else None,
        "trend_stability": _trend_stability(oil_rates),
        "late_water_cut": late_water,
        "water_cut_growth": late_water - initial_water if late_water is not None and initial_water is not None else None,
        "producing_months": len(series),
        "uptime_ratio": min(100.0, sum(valid_days) / (len(valid_days) * 30.4375) * 100) if valid_days else None,
        "cumulative_oil": segment_volume("monthly_oil", "cumulative_oil"),
        "cumulative_water": segment_volume("monthly_water", "cumulative_water"),
        "cumulative_gas_kboe": segment_volume("monthly_gas", "cumulative_gas", 6_000_000),
        "pressure_change": pressure[-1] - pressure[0] if len(pressure) >= 2 else None,
        "initial_gor": _mean(gas_oil_ratios[:3]),
    })
    return values


def build_feature_dataset(
    monthly_rows: Iterable[dict[str, Any]],
    event_rows: Iterable[dict[str, Any]],
    interval_rows: Iterable[dict[str, Any]],
    well_rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    monthly_by_well: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by_well: dict[str, list[dict[str, Any]]] = defaultdict(list)
    intervals_by_well: dict[str, list[dict[str, Any]]] = defaultdict(list)
    well_by_key: dict[str, dict[str, Any]] = {}
    for row in monthly_rows:
        monthly_by_well[str(row.get("well_key") or "")].append(dict(row))
    for row in event_rows:
        events_by_well[str(row.get("well_key") or "")].append(dict(row))
    for row in interval_rows:
        intervals_by_well[str(row.get("well_key") or "")].append(dict(row))
    for row in well_rows:
        key = str(row.get("well_key") or row.get("normalized_key") or "")
        if key:
            well_by_key[key] = dict(row)

    keys = sorted(set(monthly_by_well) | set(events_by_well) | set(intervals_by_well))
    rows: list[dict[str, Any]] = []
    for key in keys:
        source = sorted(monthly_by_well.get(key, []), key=lambda row: (_date_key(row.get("production_month")), int(row.get("id") or 0)))
        series = build_production_series(source)
        oil_rates = [float(row["oil_rate"]) for row in series if _number(row.get("oil_rate")) is not None]
        water_rates = [float(row["water_rate"]) for row in series if _number(row.get("water_rate")) is not None]
        gas_rates = [float(row["gas_rate"]) for row in series if _number(row.get("gas_rate")) is not None]
        water_cuts = [float(row["water_cut"]) for row in series if _number(row.get("water_cut")) is not None]
        gas_oil_ratios = [float(row["gas_oil_ratio"]) for row in series if _number(row.get("gas_oil_ratio")) is not None]
        initial_oil, late_oil = _mean(oil_rates[:3]), _mean(oil_rates[-3:])
        initial_water_rate, late_water_rate = _mean(water_rates[:3]), _mean(water_rates[-3:])
        initial_gas_rate, late_gas_rate = _mean(gas_rates[:3]), _mean(gas_rates[-3:])
        initial_water, late_water = _mean(water_cuts[:3]), _mean(water_cuts[-3:])
        cum_oil = next((_number(row.get("cumulative_oil")) for row in reversed(series) if _number(row.get("cumulative_oil")) is not None), None)
        cum_water = next((_number(row.get("cumulative_water")) for row in reversed(series) if _number(row.get("cumulative_water")) is not None), None)
        cum_gas = next((_number(row.get("cumulative_gas")) for row in reversed(series) if _number(row.get("cumulative_gas")) is not None), None)
        days = [_number(row.get("days_on")) for row in series]
        valid_days = [value for value in days if value is not None]
        pressure = [_number(row.get("pressure")) for row in series]
        pressure = [value for value in pressure if value is not None]
        intervention_response, intervention_count = _intervention_response(series, events_by_well.get(key, []))
        intervals = intervals_by_well.get(key, [])
        well = well_by_key.get(key, {})
        formations = [str(value).strip() for value in [well.get("zone_name"), *(row.get("interval_name") for row in intervals)] if str(value or "").strip()]
        unique_formations = list(dict.fromkeys(formations))
        values = {
            "initial_3m_oil_rate": initial_oil,
            "initial_3m_water_rate": initial_water_rate,
            "initial_3m_gas_boe_rate": initial_gas_rate / 6000 if initial_gas_rate is not None else None,
            "peak_oil_rate": max(oil_rates) if oil_rates else None,
            "late_3m_oil_rate": late_oil,
            "late_3m_water_rate": late_water_rate,
            "late_3m_gas_boe_rate": late_gas_rate / 6000 if late_gas_rate is not None else None,
            "oil_decline_pct": ((initial_oil - late_oil) / abs(initial_oil) * 100) if initial_oil not in (None, 0) and late_oil is not None else None,
            "trend_stability": _trend_stability(oil_rates),
            "late_water_cut": late_water,
            "water_cut_growth": late_water - initial_water if late_water is not None and initial_water is not None else None,
            "producing_months": len(series),
            "uptime_ratio": min(100.0, sum(valid_days) / (len(valid_days) * 30.4375) * 100) if valid_days else None,
            "cumulative_oil": cum_oil / 1000 if cum_oil is not None else None,
            "cumulative_water": cum_water / 1000 if cum_water is not None else None,
            "cumulative_gas_kboe": cum_gas / 6_000_000 if cum_gas is not None else None,
            "pressure_change": pressure[-1] - pressure[0] if len(pressure) >= 2 else None,
            "intervention_response": intervention_response,
            "event_count": intervention_count or len(events_by_well.get(key, [])),
            "nearest_distance": None,
            "perforation_length": sum(max(0.0, (_number(row.get("base_md")) or 0) - (_number(row.get("top_md")) or 0)) for row in intervals),
            "formation_count": len(unique_formations),
            "initial_gor": _mean(gas_oil_ratios[:3]),
        }
        intervention_events = sorted(
            [row for row in events_by_well.get(key, []) if _event_is_intervention(row)],
            key=lambda row: _date_key(row.get("event_date")),
        )
        segment_samples = []
        intervention_date = intervention_events[0].get("event_date") if intervention_events else None
        if intervention_date:
            marker = _date_key(intervention_date)
            before = [row for row in series if _date_key(row.get("date")) < marker]
            after = [row for row in series if _date_key(row.get("date")) >= marker]
            if len(before) >= 3 and len(after) >= 3:
                before_values = _segment_feature_values(before, values)
                before_values.update({"intervention_response": None, "event_count": 0})
                after_values = _segment_feature_values(after, values)
                after_values.update({"intervention_response": intervention_response, "event_count": intervention_count})
                segment_samples = [
                    {"sample_key": f"{key}::pre", "segment": "措施前基线", "record_count": len(before), "start": before[0].get("date"), "end": before[-1].get("date"), "values": before_values},
                    {"sample_key": f"{key}::post", "segment": "措施后响应", "record_count": len(after), "start": after[0].get("date"), "end": after[-1].get("date"), "values": after_values},
                ]
        rows.append({
            "well_key": key,
            "well_name": next((row.get("well_name") for row in source + events_by_well.get(key, []) + intervals if row.get("well_name")), None) or well.get("well_name") or key,
            "x": _number(well.get("x")) if _number(well.get("x")) is not None else _number(well.get("surface_x")),
            "y": _number(well.get("y")) if _number(well.get("y")) is not None else _number(well.get("surface_y")),
            "well_type": well.get("well_type"), "status": well.get("status"),
            "formations": unique_formations, "record_count": len(series), "event_count": len(events_by_well.get(key, [])),
            "has_intervention": intervention_count > 0, "intervention_date": intervention_date,
            "segment_eligible": len(segment_samples) == 2, "segment_samples": segment_samples, "values": values,
        })

    # Compute a formation-aware nearest neighbour.  Same-formation wells are
    # preferred; if no such neighbour exists, the closest positioned well is used.
    for row in rows:
        if row["x"] is None or row["y"] is None:
            continue
        same_formation, all_distances = [], []
        own = set(row["formations"])
        for other in rows:
            if other is row or other["x"] is None or other["y"] is None:
                continue
            distance = math.hypot(row["x"] - other["x"], row["y"] - other["y"])
            all_distances.append(distance)
            if own and own.intersection(other["formations"]):
                same_formation.append(distance)
        candidates = same_formation or all_distances
        row["values"]["nearest_distance"] = min(candidates) if candidates else None

    indicators = []
    for definition in INDICATORS:
        values = [row["values"].get(definition["key"]) for row in rows]
        available = sum(value is not None for value in values)
        indicators.append({
            **definition,
            "available_wells": available,
            "coverage_pct": round(available / len(rows) * 100, 1) if rows else 0.0,
            "available": available > 0 and definition["priority"] != "missing",
            "selected": definition["priority"] == "recommended" and available >= max(2, math.ceil(len(rows) * 0.5)),
        })
    return {
        "wells": rows,
        "indicators": indicators,
        "composition_schemes": COMPOSITION_SCHEMES,
        "composition_note": "油、水按桶计；气相按 6,000 scf = 1 BOE 折算。仅用于同时间口径的产量组成展示。",
    }


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _indicator_grade(value: float | None, population: list[float], direction: int) -> str:
    if value is None or not population or direction == 0:
        return "参考" if value is not None else "缺失"
    q1, q2 = _quantile(population, 1 / 3), _quantile(population, 2 / 3)
    grade = "高" if value >= q2 else "中" if value >= q1 else "低"
    if direction < 0:
        grade = {"高": "低", "低": "高", "中": "中"}[grade]
    return grade


def _distance(left: list[float], right: list[float]) -> float:
    return sum((a - b) ** 2 for a, b in zip(left, right))


def _kmeans(
    matrix: list[list[float]], clusters: int, max_iterations: int,
    progress: Callable[[float, str], None] | None = None,
) -> tuple[list[int], list[list[float]], list[float]]:
    # Deterministic farthest-point seeding makes repeated geological reviews reproducible.
    if np is not None:
        values = np.asarray(matrix, dtype=float)
        center_indexes = [int(np.argmin(values.sum(axis=1)))]
        while len(center_indexes) < clusters:
            available = np.ones(len(values), dtype=bool)
            available[center_indexes] = False
            distances = ((values[:, None, :] - values[center_indexes][None, :, :]) ** 2).sum(axis=2)
            nearest = distances.min(axis=1)
            nearest[~available] = -1
            center_indexes.append(int(np.argmax(nearest)))
        centroids = values[center_indexes].copy()
        assignments = np.full(len(values), -1, dtype=int)
        history: list[float] = []
        for iteration in range(max_iterations):
            distances = ((values[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
            updated = distances.argmin(axis=1)
            history.append(round(float(distances[np.arange(len(values)), updated].sum()), 6))
            if np.array_equal(updated, assignments):
                break
            assignments = updated
            for cluster in range(clusters):
                members = values[assignments == cluster]
                if len(members):
                    centroids[cluster] = members.mean(axis=0)
            if progress and (iteration == 0 or (iteration + 1) % max(1, max_iterations // 20) == 0):
                progress((iteration + 1) / max_iterations, f"K-Means 第 {iteration + 1} / {max_iterations} 轮")
        return assignments.tolist(), centroids.tolist(), history

    center_indexes = [min(range(len(matrix)), key=lambda index: sum(matrix[index]))]
    while len(center_indexes) < clusters:
        center_indexes.append(max((index for index in range(len(matrix)) if index not in center_indexes), key=lambda index: min(_distance(matrix[index], matrix[chosen]) for chosen in center_indexes)))
    centroids = [matrix[index][:] for index in center_indexes]
    assignments = [-1] * len(matrix)
    history = []
    for iteration in range(max_iterations):
        updated = [min(range(clusters), key=lambda cluster: _distance(row, centroids[cluster])) for row in matrix]
        inertia = sum(_distance(row, centroids[cluster]) for row, cluster in zip(matrix, updated))
        history.append(round(inertia, 6))
        if updated == assignments:
            break
        assignments = updated
        for cluster in range(clusters):
            members = [row for row, assigned in zip(matrix, assignments) if assigned == cluster]
            if members:
                centroids[cluster] = [sum(column) / len(members) for column in zip(*members)]
        if progress and (iteration == 0 or (iteration + 1) % max(1, max_iterations // 20) == 0):
            progress((iteration + 1) / max_iterations, f"K-Means 第 {iteration + 1} / {max_iterations} 轮")
    return assignments, centroids, history


def _som(
    matrix: list[list[float]], clusters: int, max_iterations: int,
    learning_rate: float = 0.45, radius: float | None = None,
    topology: str = "line", progress: Callable[[float, str], None] | None = None,
) -> tuple[list[int], list[list[float]], list[float]]:
    """Train a deterministic one-dimensional SOM for auditable well typing."""
    center_indexes = [min(range(len(matrix)), key=lambda index: sum(matrix[index]))]
    while len(center_indexes) < clusters:
        center_indexes.append(max(
            (index for index in range(len(matrix)) if index not in center_indexes),
            key=lambda index: min(_distance(matrix[index], matrix[chosen]) for chosen in center_indexes),
        ))
    weights = [matrix[index][:] for index in center_indexes]
    initial_radius = max(0.5, float(radius if radius is not None else max(1.0, clusters / 2)))
    initial_rate = max(0.01, min(1.0, float(learning_rate)))
    history: list[float] = []
    epochs = max(5, min(max_iterations, 500))
    if np is not None:
        values = np.asarray(matrix, dtype=float)
        weights_array = np.asarray(weights, dtype=float)
        neuron_indexes = np.arange(clusters)
        for epoch in range(epochs):
            fraction = epoch / max(1, epochs - 1)
            rate = initial_rate * math.exp(-3.0 * fraction)
            neighborhood = max(0.15, initial_radius * math.exp(-3.0 * fraction))
            # Preserve the original online SOM update order exactly.  Only the
            # per-neuron distance and weight arithmetic is vectorized, so an
            # existing geological scheme does not change merely due to speed.
            for row in values:
                winner = int(np.argmin(((weights_array - row) ** 2).sum(axis=1)))
                grid_distance = np.abs(neuron_indexes - winner)
                if topology == "ring":
                    grid_distance = np.minimum(grid_distance, clusters - grid_distance)
                influence = np.exp(-(grid_distance ** 2) / (2 * neighborhood * neighborhood))
                weights_array += rate * influence[:, None] * (row - weights_array)
            distances = ((values[:, None, :] - weights_array[None, :, :]) ** 2).sum(axis=2)
            assignments_array = distances.argmin(axis=1)
            error = np.sqrt(distances[np.arange(len(values)), assignments_array]).mean()
            history.append(round(float(error), 6))
            if progress and (epoch == 0 or (epoch + 1) % max(1, epochs // 20) == 0 or epoch + 1 == epochs):
                progress((epoch + 1) / epochs, f"SOM 第 {epoch + 1} / {epochs} 轮")
        return assignments_array.tolist(), weights_array.tolist(), history

    for epoch in range(epochs):
        epoch_progress = epoch / max(1, epochs - 1)
        rate = initial_rate * math.exp(-3.0 * epoch_progress)
        neighborhood = max(0.15, initial_radius * math.exp(-3.0 * epoch_progress))
        for row in matrix:
            winner = min(range(clusters), key=lambda index: _distance(row, weights[index]))
            for index in range(clusters):
                grid_distance = abs(index - winner)
                if topology == "ring":
                    grid_distance = min(grid_distance, clusters - grid_distance)
                influence = math.exp(-(grid_distance ** 2) / (2 * neighborhood * neighborhood))
                weights[index] = [value + rate * influence * (target - value) for value, target in zip(weights[index], row)]
        assignments = [min(range(clusters), key=lambda index: _distance(row, weights[index])) for row in matrix]
        error = sum(math.sqrt(_distance(row, weights[index])) for row, index in zip(matrix, assignments)) / len(matrix)
        history.append(round(error, 6))
        if progress and (epoch == 0 or (epoch + 1) % max(1, epochs // 20) == 0 or epoch + 1 == epochs):
            progress((epoch + 1) / epochs, f"SOM 第 {epoch + 1} / {epochs} 轮")
    return assignments, weights, history


def _silhouette(matrix: list[list[float]], assignments: list[int]) -> float | None:
    if len(set(assignments)) < 2 or len(matrix) < 3:
        return None
    if np is not None:
        values = np.asarray(matrix, dtype=float)
        labels = np.asarray(assignments, dtype=int)
        # Bound quadratic memory on very large projects while keeping a fixed,
        # reproducible sample for comparable scheme quality statistics.
        sample_indexes = np.arange(len(values)) if len(values) <= 2000 else np.linspace(0, len(values) - 1, 2000, dtype=int)
        sampled = values[sample_indexes]
        squared = np.maximum(
            (sampled * sampled).sum(axis=1)[:, None] + (values * values).sum(axis=1)[None, :] - 2 * sampled @ values.T,
            0.0,
        )
        distances = np.sqrt(squared)
        scores = np.zeros(len(sample_indexes), dtype=float)
        clusters = np.unique(labels)
        for position, source_index in enumerate(sample_indexes):
            own_mask = labels == labels[source_index]
            own_count = int(own_mask.sum()) - 1
            a = float(distances[position, own_mask].sum() / own_count) if own_count > 0 else 0.0
            others = [float(distances[position, labels == cluster].mean()) for cluster in clusters if cluster != labels[source_index]]
            b = min(others) if others else 0.0
            scores[position] = (b - a) / max(a, b) if max(a, b) else 0.0
        return round(float(scores.mean()), 4)
    scores = []
    for index, row in enumerate(matrix):
        own = [math.sqrt(_distance(row, matrix[j])) for j in range(len(matrix)) if j != index and assignments[j] == assignments[index]]
        a = sum(own) / len(own) if own else 0.0
        other = []
        for cluster in set(assignments):
            if cluster == assignments[index]:
                continue
            distances = [math.sqrt(_distance(row, matrix[j])) for j in range(len(matrix)) if assignments[j] == cluster]
            if distances:
                other.append(sum(distances) / len(distances))
        b = min(other) if other else 0.0
        scores.append((b - a) / max(a, b) if max(a, b) else 0.0)
    return round(sum(scores) / len(scores), 4)


def _principal_components(matrix: list[list[float]]) -> list[list[float]]:
    """Return two deterministic PCA axes without adding a heavy dependency."""
    dimensions = len(matrix[0]) if matrix else 0
    if not dimensions:
        return [[], []]
    if np is not None:
        values = np.asarray(matrix, dtype=float)
        covariance = values.T @ values / max(1, len(values))
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        axes = []
        for index in np.argsort(eigenvalues)[::-1][:2]:
            axis = eigenvectors[:, index]
            pivot = int(np.argmax(np.abs(axis)))
            if axis[pivot] < 0:
                axis = -axis
            axes.append(axis.tolist())
        while len(axes) < 2:
            axes.append([0.0] * dimensions)
        return axes
    covariance = [[sum(row[i] * row[j] for row in matrix) / max(1, len(matrix)) for j in range(dimensions)] for i in range(dimensions)]

    def power(source: list[list[float]], seed_shift: int = 0) -> tuple[list[float], float]:
        vector = [1.0 / (index + 1 + seed_shift) for index in range(dimensions)]
        for _ in range(60):
            updated = [sum(source[i][j] * vector[j] for j in range(dimensions)) for i in range(dimensions)]
            norm = math.sqrt(sum(value * value for value in updated))
            if norm < 1e-12:
                vector = [1.0 if index == min(seed_shift, dimensions - 1) else 0.0 for index in range(dimensions)]
                break
            updated = [value / norm for value in updated]
            if sum(abs(left - right) for left, right in zip(updated, vector)) < 1e-9:
                vector = updated
                break
            vector = updated
        eigenvalue = sum(vector[i] * sum(source[i][j] * vector[j] for j in range(dimensions)) for i in range(dimensions))
        return vector, max(0.0, eigenvalue)

    first, first_value = power(covariance)
    deflated = [[covariance[i][j] - first_value * first[i] * first[j] for j in range(dimensions)] for i in range(dimensions)]
    second, _ = power(deflated, 1)
    return [first, second]


def train_and_evaluate(
    dataset: dict[str, Any], indicator_keys: list[str], training_keys: list[str],
    evaluation_keys: list[str], cluster_count: int = 3, max_iterations: int = 80,
    segmentation_mode: str = "split", model_type: str = "kmeans",
    model_parameters: dict[str, Any] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    if progress:
        progress(0.03, "核对指标与井清单")
    definitions = {row["key"]: row for row in dataset["indicators"]}
    selected = [key for key in indicator_keys if key in definitions and definitions[key]["available"]]
    by_key = {row["well_key"]: row for row in dataset["wells"]}
    training_wells = [by_key[key] for key in training_keys if key in by_key]
    evaluation = [by_key[key] for key in evaluation_keys if key in by_key]
    segmentation_mode = segmentation_mode if segmentation_mode in {"split", "whole", "pre_only"} else "split"
    training = []
    for well in training_wells:
        if int(well.get("record_count") or 0) < 3:
            continue
        segments = well.get("segment_samples") or []
        if segmentation_mode == "split" and len(segments) == 2:
            for segment in segments:
                training.append({**well, **segment, "well_key": well["well_key"], "well_name": well["well_name"]})
        elif segmentation_mode == "pre_only" and well.get("has_intervention"):
            if segments:
                segment = segments[0]
                training.append({**well, **segment, "well_key": well["well_key"], "well_name": well["well_name"]})
        else:
            training.append({**well, "sample_key": f"{well['well_key']}::whole", "segment": "整井", "record_count": well["record_count"]})
    checks = [
        {"key": "features", "label": "至少选择2个有效指标", "passed": len(selected) >= 2, "jump_to": "indicators"},
        {"key": "samples", "label": "有效训练样本不少于聚类数且至少3个", "passed": len(training) >= max(3, cluster_count), "jump_to": "dataset"},
        {"key": "evaluation", "label": "已选择待评价井", "passed": bool(evaluation), "jump_to": "evaluation"},
    ]
    if len(selected) >= 2 and training:
        complete = sum(all(row["values"].get(key) is not None for key in selected) for row in training)
        checks.append({"key": "completeness", "label": "至少50%训练井具备完整指标", "passed": complete / len(training) >= 0.5, "jump_to": "dataset"})
    else:
        checks.append({"key": "completeness", "label": "至少50%训练井具备完整指标", "passed": False, "jump_to": "dataset"})
    if not all(row["passed"] for row in checks):
        return {"ready": False, "self_check": checks, "jump_to": next(row["jump_to"] for row in checks if not row["passed"])}

    if progress:
        progress(0.12, f"构建 {len(training)} 个有效训练样本")

    medians, means, scales = {}, {}, {}
    for key in selected:
        values = [float(row["values"][key]) for row in training if row["values"].get(key) is not None]
        medians[key] = median(values)
        means[key] = sum(values) / len(values)
        variance = sum((value - means[key]) ** 2 for value in values) / len(values)
        scales[key] = math.sqrt(variance) or 1.0

    def vector(row: dict[str, Any]) -> list[float]:
        return [((row["values"].get(key) if row["values"].get(key) is not None else medians[key]) - means[key]) / scales[key] for key in selected]

    matrix = [vector(row) for row in training]
    if progress:
        progress(0.24, "完成缺失补齐与标准化")
    model_parameters = model_parameters or {}
    model_progress = (lambda fraction, stage: progress(0.24 + fraction * 0.5, stage)) if progress else None
    if model_type == "som":
        assignments, centroids, history = _som(
            matrix, cluster_count, max_iterations,
            float(model_parameters.get("learning_rate") or 0.45),
            float(model_parameters.get("radius") or max(1.0, cluster_count / 2)),
            str(model_parameters.get("topology") or "line"), model_progress,
        )
    else:
        assignments, centroids, history = _kmeans(matrix, cluster_count, max(5, min(max_iterations, 500)), model_progress)
    direction = [definitions[key].get("direction", 0) for key in selected]
    scores = [sum(value * sign for value, sign in zip(center, direction) if sign) for center in centroids]
    order = sorted(range(cluster_count), key=lambda cluster: scores[cluster], reverse=True)
    labels = ["高产稳产型", "中产均衡型", "低产待挖潜型"] if cluster_count == 3 else [f"产能类型 {index + 1}" for index in range(cluster_count)]
    cluster_names = {cluster: labels[min(rank, len(labels) - 1)] for rank, cluster in enumerate(order)}
    cluster_ranks = {cluster: rank + 1 for rank, cluster in enumerate(order)}
    populations = {key: [float(row["values"][key]) for row in training if row["values"].get(key) is not None] for key in selected}
    if progress:
        progress(0.79, "计算轮廓系数")
    silhouette = _silhouette(matrix, assignments)
    if progress:
        progress(0.88, "计算 PCA 二维投影")
    components = _principal_components(matrix)

    def projection(row_vector: list[float]) -> tuple[float, float]:
        coordinates = [sum(value * weight for value, weight in zip(row_vector, component)) for component in components]
        return coordinates[0] if coordinates else 0.0, coordinates[1] if len(coordinates) > 1 else 0.0

    def result_row(row: dict[str, Any], cluster: int, scope: str) -> dict[str, Any]:
        metrics = [{"key": key, "label": definitions[key]["label"], "unit": definitions[key]["unit"], "value": row["values"].get(key), "grade": _indicator_grade(row["values"].get(key), populations[key], definitions[key]["direction"])} for key in selected]
        row_vector = vector(row)
        x_plot, y_plot = projection(row_vector)
        rank = cluster_ranks[cluster]
        level = "相对较好" if rank == 1 else "相对偏弱" if rank == cluster_count else f"中间过渡（第 {rank} 级）"
        return {"well_key": row["well_key"], "well_name": row["well_name"], "scope": scope, "cluster": cluster, "category": cluster_names[cluster], "category_rank": rank, "category_level": level, "formations": row["formations"], "has_intervention": row["has_intervention"], "metrics": metrics, "x": row.get("x"), "y": row.get("y"), "x_plot": x_plot, "y_plot": y_plot}

    training_sample_results = []
    for row, cluster, row_vector in zip(training, assignments, matrix):
        x_plot, y_plot = projection(row_vector)
        training_sample_results.append({"sample_key": row["sample_key"], "well_key": row["well_key"], "well_name": row["well_name"], "segment": row.get("segment") or "整井", "cluster": cluster, "category": cluster_names[cluster], "x_plot": x_plot, "y_plot": y_plot})
    training_set = {row["well_key"] for row in training}
    results = []
    for row in training_wells:
        if row["well_key"] not in training_set:
            continue
        row_vector = vector(row)
        cluster = min(range(cluster_count), key=lambda index: _distance(row_vector, centroids[index]))
        results.append(result_row(row, cluster, "训练井"))
    for row in evaluation:
        if row["well_key"] in training_set:
            continue
        row_vector = vector(row)
        cluster = min(range(cluster_count), key=lambda index: _distance(row_vector, centroids[index]))
        results.append(result_row(row, cluster, "评价井"))
    counts = {name: sum(row["category"] == name for row in results) for name in cluster_names.values()}
    profiles = []
    for cluster in order:
        category = cluster_names[cluster]
        members = [row for row in results if row["cluster"] == cluster]
        metric_profiles = []
        for key in selected:
            values = [float(metric["value"]) for row in members for metric in row["metrics"] if metric["key"] == key and metric["value"] is not None]
            metric_profiles.append({
                "key": key, "label": definitions[key]["label"], "unit": definitions[key]["unit"],
                "min": min(values) if values else None, "q1": _quantile(values, 0.25) if values else None,
                "median": median(values) if values else None, "q3": _quantile(values, 0.75) if values else None,
                "max": max(values) if values else None,
            })
        profiles.append({"cluster": cluster, "category": category, "rank": cluster_ranks[cluster], "count": len(members), "metrics": metric_profiles})
    if progress:
        progress(0.92, "生成井级评价与二维投影")
    result = {
        "ready": True, "self_check": checks, "model": "SOM 自组织映射（标准化 + 邻域竞争学习）" if model_type == "som" else "K-Means（标准化 + 中位数补缺）",
        "model_type": model_type, "model_parameters": model_parameters,
        "selected_indicators": selected, "training_count": len(training_set), "training_sample_count": len(training), "evaluation_count": len(evaluation),
        "segmentation_mode": segmentation_mode,
        "cluster_count": cluster_count, "iterations": len(history), "loss_history": history,
        "inertia": history[-1] if history else None, "silhouette": silhouette,
        "cluster_counts": counts, "cluster_profiles": profiles, "results": results,
        "training_samples": training_sample_results,
        "projection": {"method": "PCA", "x_label": "主成分 1", "y_label": "主成分 2"},
        "category_order": [{"category": cluster_names[cluster], "cluster": cluster, "rank": cluster_ranks[cluster], "level": "相对较好" if cluster_ranks[cluster] == 1 else "相对偏弱" if cluster_ranks[cluster] == cluster_count else "中间过渡"} for cluster in order],
        "notes": ["类别按所选指标的有利方向排序：序号越小，综合产能特征相对越好；这不是储量分级。", "缺失值仅在模型计算中用训练集中位数补齐；结果表仍显示原始缺失。", "默认仅在首个明确措施事件前后各至少3个有效期时拆成两个训练样本；井级评价仍回到整井口径。"],
    }
    result["performance"] = {
        "engine": "NumPy 向量化计算" if np is not None else "Python 兼容计算",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "silhouette_sample_count": min(len(matrix), 2000),
    }
    if progress:
        progress(1.0, "训练与质量评价完成")
    return result
