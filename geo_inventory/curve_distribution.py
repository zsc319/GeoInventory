from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from statistics import fmean
from typing import Any

from .db import utcnow
from .importers import parse_las, parse_las_curve_samples


SETTING_KEY = "curve_filter_v1"
MAX_SERIES = 24
MAX_VALUES_PER_SERIES = 20_000
# A broad curve histogram should remain interactive even when an old field
# contains hundreds of LAS files.  Explicitly selected wells are never capped.
MAX_SOURCE_LAS_PER_BROAD_DISTRIBUTION = 160

MODE_LABELS = {
    "single_curve": "单根曲线总体分布",
    "same_type": "同类曲线名称对比",
    "one_type_multiwell": "多井单类型曲线对比",
    "single_well_multitype": "单井多类型曲线对比",
    "multiwell_multitype": "多井多类型矩阵对比",
    "same_well_alternatives": "同井同类备选曲线对比",
    "layer_contrast": "层段内外分布对比",
    "free_compare": "自由曲线组合对比",
}


def default_curve_filter() -> dict[str, Any]:
    return {
        "active": False,
        "mnemonics": [],
        "type_keys": [],
        "classification": "all",
        "well_keys": [],
        "layer": {
            "mode": "all",
            "top_md": None,
            "base_md": None,
            "horizon_key": None,
            "offset_above": 25.0,
            "offset_below": 25.0,
            "top_horizon_key": None,
            "base_horizon_key": None,
        },
    }


def _text_list(values: Any, *, upper: bool = False) -> list[str]:
    result: list[str] = []
    for value in values if isinstance(values, list) else []:
        text = str(value or "").strip()
        if text:
            text = text.upper() if upper else text
            if text not in result:
                result.append(text)
    return result


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def sanitize_curve_filter(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload or {}
    layer_source = source.get("layer") if isinstance(source.get("layer"), dict) else {}
    mode = str(layer_source.get("mode") or "all")
    if mode not in {"all", "md", "horizon_window", "between_horizons"}:
        mode = "all"
    classification = str(source.get("classification") or "all")
    if classification not in {"all", "classified", "confirmed", "automatic", "unclassified"}:
        classification = "all"
    layer = {
        "mode": mode,
        "top_md": _number(layer_source.get("top_md")),
        "base_md": _number(layer_source.get("base_md")),
        "horizon_key": str(layer_source.get("horizon_key") or "").strip() or None,
        "offset_above": max(0.0, _number(layer_source.get("offset_above")) or 0.0),
        "offset_below": max(0.0, _number(layer_source.get("offset_below")) or 0.0),
        "top_horizon_key": str(layer_source.get("top_horizon_key") or "").strip() or None,
        "base_horizon_key": str(layer_source.get("base_horizon_key") or "").strip() or None,
    }
    result = {
        "active": bool(source.get("active")),
        "mnemonics": _text_list(source.get("mnemonics"), upper=True),
        "type_keys": _text_list(source.get("type_keys")),
        "classification": classification,
        "well_keys": _text_list(source.get("well_keys"), upper=True),
        "layer": layer,
    }
    has_condition = bool(
        result["mnemonics"]
        or result["type_keys"]
        or result["classification"] != "all"
        or result["well_keys"]
        or result["layer"]["mode"] != "all"
    )
    result["active"] = result["active"] and has_condition
    return result


def load_curve_filter(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value_json FROM workspace_settings WHERE setting_key=?", (SETTING_KEY,)).fetchone()
    if not row:
        return default_curve_filter()
    try:
        return sanitize_curve_filter(json.loads(row["value_json"]))
    except (TypeError, ValueError):
        return default_curve_filter()


def save_curve_filter(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    result = sanitize_curve_filter(payload)
    conn.execute(
        """INSERT INTO workspace_settings(setting_key,value_json,updated_at) VALUES(?,?,?)
           ON CONFLICT(setting_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
        (SETTING_KEY, json.dumps(result, ensure_ascii=False), utcnow()),
    )
    conn.commit()
    return result


def curve_filter_options(
    workbench: dict[str, Any],
    wells: list[dict[str, Any]],
    horizons: list[dict[str, Any]],
) -> dict[str, Any]:
    type_index = {row["type_key"]: row for row in workbench.get("types", [])}
    curves = []
    for row in workbench.get("curves", []):
        assignment = row.get("assignment") or {}
        type_key = assignment.get("type_key") or (row.get("suggestion") or {}).get("type_key")
        curve_type = type_index.get(type_key, {})
        curves.append({
            "mnemonic": row["mnemonic"],
            "well_count": row.get("well_count", 0),
            "units": row.get("units", []),
            "type_key": type_key,
            "type_name": curve_type.get("name") or "未分类",
            "classification": assignment.get("status") or ("automatic" if type_key else "unclassified"),
        })
    return {
        "curves": curves,
        "types": [row for row in workbench.get("types", []) if row.get("type_key") != "unclassified"],
        "wells": [{"well_key": row["project_key"], "well_name": row.get("canonical_name") or row["project_key"]} for row in wells],
        "horizons": [row for row in horizons if row.get("kind") == "regular_surface"],
    }


def _classification_matches(status: str, type_key: str | None, wanted: str) -> bool:
    classified = bool(type_key and type_key != "unclassified")
    if wanted == "classified":
        return classified
    if wanted == "unclassified":
        return not classified
    if wanted == "confirmed":
        return classified and status == "confirmed"
    if wanted == "automatic":
        return classified and status == "automatic"
    return True


def _depth_factor_to_m(unit: str | None) -> float | None:
    value = str(unit or "").strip().upper().replace(" ", "")
    if value in {"M", "METER", "METERS", "METRE", "METRES"}:
        return 1.0
    if value in {"FT", "FEET", "FOOT", "F"}:
        return 0.3048
    return None


def _project_horizon_hits(conn: sqlite3.Connection, project_root: str) -> dict[tuple[str, str], float]:
    return {
        (row["horizon_key"], row["well_key"]): float(row["intersection_md"])
        for row in conn.execute(
            """SELECT horizon_key,well_key,intersection_md FROM horizon_well_hits
               WHERE project_root=? AND hit=1 AND intersection_md IS NOT NULL""",
            (project_root,),
        )
    }


def _interval_for_well(
    layer: dict[str, Any],
    well_key: str,
    depth_unit: str | None,
    horizon_hits: dict[tuple[str, str], float],
) -> tuple[float, float] | None:
    mode = layer.get("mode", "all")
    if mode == "all":
        return -math.inf, math.inf
    if mode == "md":
        top, base = _number(layer.get("top_md")), _number(layer.get("base_md"))
    else:
        factor = _depth_factor_to_m(depth_unit) or 1.0
        if mode == "horizon_window":
            center_m = horizon_hits.get((str(layer.get("horizon_key")), well_key))
            if center_m is None:
                return None
            top = (center_m - float(layer.get("offset_above") or 0.0)) / factor
            base = (center_m + float(layer.get("offset_below") or 0.0)) / factor
        elif mode == "between_horizons":
            top_m = horizon_hits.get((str(layer.get("top_horizon_key")), well_key))
            base_m = horizon_hits.get((str(layer.get("base_horizon_key")), well_key))
            if top_m is None or base_m is None:
                return None
            top, base = top_m / factor, base_m / factor
        else:
            return -math.inf, math.inf
    if top is None or base is None:
        return None
    return (min(top, base), max(top, base))


@lru_cache(maxsize=64)
def _read_sampled_las(path: str, modified_ns: int, size: int) -> dict[str, Any]:
    del modified_ns, size
    return parse_las(path, include_samples=True, sample_limit=4096)


@lru_cache(maxsize=256)
def _read_targeted_las(path: str, modified_ns: int, size: int, mnemonic_key: str) -> dict[str, Any]:
    """Read only requested curve columns; cache by source revision and set."""
    del modified_ns, size
    return parse_las_curve_samples(path, set(filter(None, mnemonic_key.split("|"))), sample_limit=4096)


def _profile_paths(conn: sqlite3.Connection, snapshot: dict[str, Any], profile: dict[str, Any]) -> dict[tuple[str, str], str]:
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    by_name: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute(
        """SELECT filename,file_path FROM project_catalog_items
           WHERE project_root=? AND category_key='well_logs' AND representative=1 ORDER BY file_path""",
        (project_root,),
    ):
        by_name[str(row["filename"]).upper()].append(row["file_path"])
    result: dict[tuple[str, str], str] = {}
    for well in profile.get("wells", []):
        direct = str(well.get("file_path") or "")
        if direct and Path(direct).is_file():
            result[(well.get("well_key"), str(well.get("filename") or ""))] = direct
            continue
        candidates = by_name.get(str(well.get("filename") or "").upper(), [])
        if candidates:
            result[(well.get("well_key"), str(well.get("filename") or ""))] = candidates[0]
    return result


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] * (upper - position) + values[upper] * (position - lower)


def _thin(values: list[float], limit: int = MAX_VALUES_PER_SERIES) -> list[float]:
    if len(values) <= limit:
        return values
    stride = len(values) / limit
    return [values[min(len(values) - 1, int(index * stride))] for index in range(limit)]


def _evenly_spaced_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Stable, field-wide sampling without retaining LAS values in memory."""
    if len(rows) <= limit:
        return rows
    if limit <= 1:
        return [rows[0]]
    step = (len(rows) - 1) / (limit - 1)
    indexes = {round(index * step) for index in range(limit)}
    return [row for index, row in enumerate(rows) if index in indexes]


def _series_stats(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    mean = fmean(ordered)
    variance = fmean([(value - mean) ** 2 for value in ordered]) if len(ordered) > 1 else 0.0
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p05": _quantile(ordered, 0.05),
        "p25": _quantile(ordered, 0.25),
        "p50": _quantile(ordered, 0.50),
        "p75": _quantile(ordered, 0.75),
        "p95": _quantile(ordered, 0.95),
        "max": ordered[-1],
        "mean": mean,
        "std": math.sqrt(variance),
    }


def _deduplicate_well_mnemonic(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid weighting a well twice when the same mnemonic exists in batches."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        key = (row["well_key"], row["mnemonic"])
        current = best.get(key)
        row_points = len(row["inside_values"]) + len(row["outside_values"])
        current_points = len(current["inside_values"]) + len(current["outside_values"]) if current else -1
        if current is None or row_points > current_points:
            best[key] = row
    return list(best.values())


def _make_series(records: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], dict[str, Any]] = {}

    def add(key: tuple[str, ...], title: str, subtitle: str, record: dict[str, Any], values: list[float] | None = None) -> None:
        row = groups.setdefault(key, {
            "title": title,
            "subtitle": subtitle,
            "unit": record.get("unit") or "未标注单位",
            "color": record.get("color") or "#53c7b7",
            "mnemonics": set(),
            "wells": set(),
            "values": [],
        })
        row["mnemonics"].add(record["mnemonic"])
        row["wells"].add(record["well_key"])
        row["values"].extend(values if values is not None else record["inside_values"])

    if mode == "single_curve":
        for row in _deduplicate_well_mnemonic(records):
            add((row["mnemonic"],), row["mnemonic"], f"{row['type_name']} · 汇总多井", row)
    elif mode == "same_type":
        for row in _deduplicate_well_mnemonic(records):
            add((row["mnemonic"],), row["mnemonic"], f"{row['type_name']} · 名称版本", row)
    elif mode == "one_type_multiwell":
        best: dict[str, dict[str, Any]] = {}
        for row in records:
            current = best.get(row["well_key"])
            if current is None or len(row["inside_values"]) > len(current["inside_values"]):
                best[row["well_key"]] = row
        for row in best.values():
            add((row["well_key"],), row["well_name"], f"{row['mnemonic']} · {row['type_name']}", row)
    elif mode == "single_well_multitype":
        for row in _deduplicate_well_mnemonic(records):
            add((row["mnemonic"],), row["type_name"], row["mnemonic"], row)
    elif mode == "multiwell_multitype":
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for row in records:
            key = (row["well_key"], row["type_key"] or row["mnemonic"])
            current = best.get(key)
            if current is None or len(row["inside_values"]) > len(current["inside_values"]):
                best[key] = row
        for row in best.values():
            add((row["well_key"], row["type_key"] or row["mnemonic"]), f"{row['well_name']} · {row['type_name']}", row["mnemonic"], row)
    elif mode == "same_well_alternatives":
        for row in records:
            add((row["mnemonic"], row["filename"]), row["mnemonic"], f"{row['well_name']} · {row['filename']}", row)
    elif mode == "layer_contrast":
        for row in _deduplicate_well_mnemonic(records):
            add(("inside",), "层段内", f"{row['mnemonic']} · 选定层段", row, row["inside_values"])
            add(("outside",), "层段外", f"{row['mnemonic']} · 其余深度", row, row["outside_values"])
    else:
        for row in _deduplicate_well_mnemonic(records):
            add((row["well_key"], row["mnemonic"]), f"{row['well_name']} · {row['mnemonic']}", row["type_name"], row)
    result = []
    for row in groups.values():
        values = _thin([value for value in row.pop("values") if math.isfinite(value)])
        if not values:
            continue
        row["mnemonics"] = sorted(row["mnemonics"])
        row["wells"] = sorted(row["wells"])
        row["values"] = values
        result.append(row)
    return result


def _histogram_groups(series: list[dict[str, Any]], bins: int) -> list[dict[str, Any]]:
    by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in series:
        by_unit[row["unit"]].append(row)
    output = []
    for unit, rows in by_unit.items():
        all_values = sorted(value for row in rows for value in row["values"])
        if not all_values:
            continue
        display_min = _quantile(all_values, 0.01)
        display_max = _quantile(all_values, 0.99)
        if display_min is None or display_max is None:
            continue
        if math.isclose(display_min, display_max):
            padding = abs(display_min) * 0.05 or 1.0
            display_min -= padding
            display_max += padding
        width = (display_max - display_min) / bins
        rendered = []
        for row in rows:
            counts = [0] * bins
            for value in row["values"]:
                index = min(bins - 1, max(0, int((value - display_min) / width)))
                counts[index] += 1
            total = max(1, sum(counts))
            rendered.append({
                **{key: row[key] for key in ("title", "subtitle", "unit", "color", "mnemonics", "wells")},
                "stats": _series_stats(row["values"]),
                "histogram": [
                    {
                        "x0": display_min + index * width,
                        "x1": display_min + (index + 1) * width,
                        "count": count,
                        "ratio": count / total,
                    }
                    for index, count in enumerate(counts)
                ],
            })
        output.append({
            "unit": unit,
            "display_min": display_min,
            "display_max": display_max,
            "full_min": all_values[0],
            "full_max": all_values[-1],
            "series": rendered,
        })
    return output


def curve_distribution(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    profile: dict[str, Any],
    workbench: dict[str, Any],
    well_filter_keys: set[str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    mode = str(payload.get("mode") or "single_curve")
    if mode not in MODE_LABELS:
        raise ValueError("未知的曲线分布对比场景")
    bins = max(12, min(60, int(payload.get("bins") or 28)))
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    # Coverage-detail popovers describe the row the user clicked. They follow
    # the global well filter but intentionally ignore the separate curve/layer
    # filter, otherwise a valid coverage row could open an empty histogram.
    rules = default_curve_filter() if payload.get("coverage_detail") else load_curve_filter(conn)
    assignments = {
        row["mnemonic"]: dict(row)
        for row in conn.execute("SELECT mnemonic,type_key,status FROM curve_type_assignments WHERE project_root=?", (project_root,))
    }
    type_index = {row["type_key"]: row for row in workbench.get("types", [])}
    request_mnemonics = set(_text_list(payload.get("mnemonics"), upper=True))
    request_types = set(_text_list(payload.get("type_keys")))
    request_wells = set(_text_list(payload.get("well_keys"), upper=True))
    allowed_wells = set(well_filter_keys)
    if rules.get("active") and rules.get("well_keys"):
        allowed_wells &= set(rules["well_keys"])
    if request_wells:
        allowed_wells &= request_wells
    filter_mnemonics = set(rules.get("mnemonics", [])) if rules.get("active") else set()
    filter_types = set(rules.get("type_keys", [])) if rules.get("active") else set()
    classification = rules.get("classification", "all") if rules.get("active") else "all"
    layer = rules.get("layer", default_curve_filter()["layer"]) if rules.get("active") else default_curve_filter()["layer"]
    if mode == "layer_contrast" and layer.get("mode") == "all":
        raise ValueError("层段内外对比需要先在“曲线筛选”中设置 MD 或层位层段")
    horizon_hits = _project_horizon_hits(conn, project_root)
    paths = _profile_paths(conn, snapshot, profile)
    suggested_types = {
        str(row.get("mnemonic") or "").upper(): ((row.get("suggestion") or {}).get("type_key"))
        for row in workbench.get("curves", [])
    }
    records: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    unknown_depth_units: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for well in profile.get("wells", []):
        well_key = str(well.get("well_key") or "").upper()
        if not well_key or well_key not in allowed_wells:
            continue
        depth_unit = well.get("depth_unit")
        interval = _interval_for_well(layer, well_key, depth_unit, horizon_hits)
        if interval is None:
            continue
        if layer.get("mode") in {"horizon_window", "between_horizons"} and _depth_factor_to_m(depth_unit) is None:
            unknown_depth_units.add(well_key)
        relevant: list[tuple[dict[str, Any], str, str | None, str]] = []
        for curve in well.get("curves", []):
            mnemonic = str(curve.get("mnemonic") or "").upper()
            if not mnemonic:
                continue
            assignment = assignments.get(mnemonic, {})
            type_key = assignment.get("type_key") or suggested_types.get(mnemonic)
            status = assignment.get("status") or ("automatic" if type_key else "unclassified")
            if filter_mnemonics and mnemonic not in filter_mnemonics:
                continue
            if filter_types and type_key not in filter_types:
                continue
            if not _classification_matches(status, type_key, classification):
                continue
            if request_mnemonics and mnemonic not in request_mnemonics:
                continue
            if request_types and type_key not in request_types:
                continue
            relevant.append((curve, mnemonic, type_key, status))
        if relevant:
            candidates.append({
                "well": well, "well_key": well_key, "depth_unit": depth_unit,
                "interval": interval, "relevant": relevant,
                "path": paths.get((well.get("well_key"), str(well.get("filename") or ""))),
            })

    # The expensive operation is reading the ~A values, not inspecting the
    # profile.  For broad views take a stable, evenly spaced sample of LAS
    # sources.  A user-specified well list remains exact by design.
    candidates.sort(key=lambda row: (str(row["well_key"]), str(row["well"].get("filename") or "")))
    candidate_count = len(candidates)
    source_sampled = bool(not request_wells and candidate_count > MAX_SOURCE_LAS_PER_BROAD_DISTRIBUTION)
    if source_sampled:
        candidates = _evenly_spaced_rows(candidates, MAX_SOURCE_LAS_PER_BROAD_DISTRIBUTION)

    for candidate in candidates:
        well = candidate["well"]
        well_key = str(candidate["well_key"])
        depth_unit = candidate["depth_unit"]
        interval = candidate["interval"]
        path = candidate["path"]
        parsed_curves: dict[str, dict[str, Any]] = {}
        if path:
            try:
                stat = Path(path).stat()
                mnemonic_key = "|".join(sorted({mnemonic for _, mnemonic, _, _ in candidate["relevant"]}))
                parsed = _read_targeted_las(path, stat.st_mtime_ns, stat.st_size, mnemonic_key)
                parsed_curves = {str(row.get("mnemonic") or "").upper(): row for row in parsed.get("curves", [])}
                depth_unit = parsed.get("depth_unit") or depth_unit
                interval = _interval_for_well(layer, well_key, depth_unit, horizon_hits)
                if interval is None:
                    continue
            except (OSError, ValueError) as exc:
                unavailable.append({"well": well_key, "file": str(well.get("filename") or path), "reason": str(exc)})
                continue
        for curve, mnemonic, type_key, status in candidate["relevant"]:
            sampled = curve.get("samples") or parsed_curves.get(mnemonic, {}).get("samples") or []
            if not sampled:
                unavailable.append({"well": well_key, "file": str(well.get("filename") or ""), "reason": f"{mnemonic} 无可读取样点"})
                continue
            top, base = interval
            inside = [float(row["value"]) for row in sampled if top <= float(row["md"]) <= base and _number(row.get("value")) is not None]
            outside = [float(row["value"]) for row in sampled if not (top <= float(row["md"]) <= base) and _number(row.get("value")) is not None]
            if not inside and mode != "layer_contrast":
                continue
            curve_type = type_index.get(type_key, {})
            source_curve = parsed_curves.get(mnemonic) or curve
            records.append({
                "well_key": well_key,
                "well_name": well.get("well_name") or well_key,
                "filename": str(well.get("filename") or "代表 LAS"),
                "mnemonic": mnemonic,
                "type_key": type_key,
                "type_name": curve_type.get("name") or "未分类",
                "classification": status,
                "unit": source_curve.get("unit") or curve.get("unit") or "未标注单位",
                "color": curve_type.get("color") or "#53c7b7",
                "inside_values": inside,
                "outside_values": outside,
            })
    if mode in {"single_curve", "layer_contrast"} and not request_mnemonics and records:
        first = records[0]["mnemonic"]
        records = [row for row in records if row["mnemonic"] == first]
    if mode in {"same_type", "one_type_multiwell", "same_well_alternatives"} and not request_types and records:
        first = next((row["type_key"] for row in records if row["type_key"]), None)
        if first:
            records = [row for row in records if row["type_key"] == first]
    if mode in {"single_well_multitype", "same_well_alternatives"} and not request_wells and records:
        first = records[0]["well_key"]
        records = [row for row in records if row["well_key"] == first]
    series = _make_series(records, mode)
    truncated = len(series) > MAX_SERIES
    series = sorted(series, key=lambda row: (row["unit"], row["title"]))[:MAX_SERIES]
    groups = _histogram_groups(series, bins)
    return {
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "groups": groups,
        "meta": {
            "well_filter_wells": len(well_filter_keys),
            "matched_wells": len({row["well_key"] for row in records}),
            "matched_mnemonics": len({row["mnemonic"] for row in records}),
            "series": sum(len(group["series"]) for group in groups),
            "sample_points": sum(item["stats"]["count"] for group in groups for item in group["series"]),
            "source_files_candidate": candidate_count,
            "source_files_read": len(candidates),
            "source_sampled": source_sampled,
            "curve_filter_active": bool(rules.get("active")),
            "layer_mode": layer.get("mode", "all"),
            "truncated": truncated,
            "unavailable_count": len(unavailable),
            "unknown_depth_unit_wells": sorted(unknown_depth_units),
        },
        "warnings": ([f"系列超过 {MAX_SERIES} 条，仅显示前 {MAX_SERIES} 条"] if truncated else [])
        + ([f"为保持响应速度，已从 {candidate_count} 个候选 LAS 中均匀读取 {len(candidates)} 个；该数值分布为代表样本"] if source_sampled else [])
        + ([f"{len(unavailable)} 个井-曲线对象没有可读取的原始样点"] if unavailable else [])
        + ([f"{len(unknown_depth_units)} 口井的 LAS 深度单位未标注，层位 MD 暂按米解释"] if unknown_depth_units else []),
        "unavailable": unavailable[:20],
        "filter": rules,
    }
