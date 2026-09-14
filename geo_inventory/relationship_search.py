from __future__ import annotations

import json
import hashlib
import math
import re
import shlex
import sqlite3
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from .curve_analysis import DEFAULT_CURVE_TYPES, is_time_depth_mnemonic, suggest_curve_type
from .global_filter import horizon_options
from .importers import identity_similarity, normalize_header, normalize_well_name, parse_las
from .project_catalog import parse_dev_stations
from .production import PRODUCTION_SERIES_FIELDS, summarize_production


_AUXILIARY_EXTENSIONS = {".xml", ".prj", ".shx", ".dbf", ".sbn", ".sbx"}


def _tokens(query: str) -> list[str]:
    return [value.strip() for value in re.split(r"[,，;；\n]+", query or "") if value.strip()]


def _layer_key(value: str) -> str:
    stem = Path(str(value or "")).stem.upper()
    stem = re.sub(r"(?:[_\- ]+(?:V|VER|REV)\d+)$", "", stem)
    stem = re.sub(r"(?:[_\- ]+20\d{2})$", "", stem)
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", stem)


def _canonical_layer_lookup(conn: sqlite3.Connection, project_root: str) -> tuple[dict[str, str], dict[str, str]]:
    """Read the shared layer vocabulary without importing the horizon UI module."""
    groups = {
        row["canonical_key"]: row["canonical_name"]
        for row in conn.execute(
            "SELECT canonical_key,canonical_name FROM horizon_name_groups WHERE project_root=?", (project_root,)
        )
    }
    aliases = {
        row["alias_key"]: row["canonical_key"]
        for row in conn.execute(
            "SELECT alias_key,canonical_key FROM horizon_name_aliases WHERE project_root=?", (project_root,)
        )
    }
    return aliases, groups


def _canonical_layer(key: str, aliases: dict[str, str], groups: dict[str, str]) -> tuple[str, str | None]:
    canonical_key = aliases.get(key, key)
    return canonical_key, groups.get(canonical_key)


def _well_parts(value: str) -> tuple[str, tuple[str, ...]]:
    key = normalize_well_name(value)
    numbers = tuple(re.findall(r"\d+", key))
    letters = re.sub(r"\d+", "", key)
    letters = re.sub(r"(?:EXP|DEV|LOGS?|TZ\d*)$", "", letters)
    consonants = re.sub(r"[AEIOU]", "", letters)
    return consonants or letters, numbers


def well_match_score(query: str, candidate: str) -> tuple[float, list[str]]:
    left, right = normalize_well_name(query), normalize_well_name(candidate)
    if not left or not right:
        return 0.0, []
    if left == right:
        return 1.0, ["标准化井名完全一致"]
    score = SequenceMatcher(None, left, right).ratio()
    reasons: list[str] = []
    left_prefix, left_numbers = _well_parts(query)
    right_prefix, right_numbers = _well_parts(candidate)
    if left_numbers and left_numbers == right_numbers:
        score = max(score, 0.78)
        reasons.append("井号数字一致")
        if left.startswith(right) or right.startswith(left):
            score = max(score, 0.90)
            reasons.append("一个名称是另一个名称的扩展写法")
        if left_prefix and right_prefix and left_prefix == right_prefix:
            score = max(score, 0.96)
            reasons.append("省略元音及批次后前缀一致")
        elif left_prefix and right_prefix and left_prefix[0] == right_prefix[0]:
            score = max(score, 0.88)
            reasons.append("同井号且区块前缀首字母一致")
    if not reasons and score >= 0.7:
        reasons.append("井名字符相似")
    return min(1.0, score), reasons


@lru_cache(maxsize=64)
def _parse_well_tops_cached(path_text: str, modified_ns: int) -> tuple[dict[str, Any], ...]:
    del modified_ns
    path = Path(path_text)
    header: list[str] = []
    rows: list[dict[str, Any]] = []
    in_header = False
    indexes: dict[str, int] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            upper = line.upper()
            if upper == "BEGIN HEADER":
                header = []
                indexes = {}
                in_header = True
                continue
            if upper == "END HEADER":
                in_header = False
                indexes = {normalize_header(name): index for index, name in enumerate(header)}
                continue
            if in_header:
                header.append(line)
                continue
            if not indexes:
                continue
            try:
                values = shlex.split(line, posix=True)
            except ValueError:
                continue
            def get(name: str) -> str | None:
                index = indexes.get(normalize_header(name))
                return values[index].strip() if index is not None and index < len(values) else None
            well, surface = get("Well"), get("Surface")
            if not well or not surface:
                continue
            def number(name: str) -> float | None:
                try:
                    value = get(name)
                    return float(value) if value not in (None, "") else None
                except ValueError:
                    return None
            rows.append({
                "well": well, "well_key": normalize_well_name(well), "surface": surface,
                "surface_key": _layer_key(surface), "type": get("Type") or "未标注",
                "md": number("MD"), "z": number("Z"), "x": number("X"), "y": number("Y"),
                "interpreter": get("Interpreter"), "source_path": path_text,
                "source_file": path.name,
            })
    return tuple(rows)


def parse_well_tops(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    try:
        return [dict(row) for row in _parse_well_tops_cached(str(source), source.stat().st_mtime_ns)]
    except OSError:
        return []


@lru_cache(maxsize=48)
def _parse_las_cached(path_text: str, modified_ns: int, size: int) -> dict[str, Any]:
    del modified_ns, size
    return parse_las(path_text, include_samples=True, sample_limit=4096)


def _read_las(path: str) -> dict[str, Any]:
    source = Path(path)
    stat = source.stat()
    return _parse_las_cached(str(source), stat.st_mtime_ns, stat.st_size)


def _curve_types(conn: sqlite3.Connection, project_root: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    rows = [dict(row) for row in conn.execute(
        "SELECT type_key,name,canonical_role,color,aliases_json FROM curve_types WHERE project_root=?",
        (project_root,),
    )]
    if not rows:
        rows = [{
            "type_key": item["key"], "name": item["name"], "canonical_role": item["role"],
            "color": item["color"], "aliases_json": json.dumps(item["aliases"]),
        } for item in DEFAULT_CURVE_TYPES]
    by_key, aliases = {}, {}
    for row in rows:
        try:
            row["aliases"] = json.loads(row.get("aliases_json") or "[]")
        except json.JSONDecodeError:
            row["aliases"] = []
        by_key[row["type_key"]] = row
        for alias in [row.get("canonical_role"), row.get("name"), row["type_key"], *row["aliases"]]:
            if alias:
                aliases[normalize_header(alias)] = row["type_key"]
    return by_key, aliases


def _curve_type_for(mnemonic: str, description: str | None, assignments: dict[str, str]) -> str | None:
    assigned = assignments.get(mnemonic.upper())
    return assigned or suggest_curve_type(mnemonic, description).get("type_key")


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _sample_stats(samples: list[dict[str, float]], bins: int = 28) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    values = [float(row["value"]) for row in samples if math.isfinite(float(row["value"]))]
    if not values:
        return None, []
    low, high = min(values), max(values)
    display_low, display_high = _quantile(values, .01), _quantile(values, .99)
    if display_low is None or display_high is None or display_high <= display_low:
        display_low, display_high = low, high
    stats = {
        "count": len(values), "min": low, "max": high, "mean": mean(values),
        "std": pstdev(values) if len(values) > 1 else 0.0,
        "p05": _quantile(values, .05), "p50": _quantile(values, .50), "p95": _quantile(values, .95),
        "display_min": display_low, "display_max": display_high,
    }
    if display_high == display_low:
        return stats, [{"x0": display_low, "x1": display_high, "count": len(values), "ratio": 1.0}]
    width = (display_high - display_low) / bins
    counts = [0] * bins
    for value in values:
        counts[max(0, min(bins - 1, int((value - display_low) / width)))] += 1
    return stats, [{
        "x0": display_low + index * width, "x1": display_low + (index + 1) * width,
        "count": count, "ratio": count / len(values),
    } for index, count in enumerate(counts)]


def _valid_curve_sample(row: dict[str, Any]) -> bool:
    try:
        value = float(row["value"])
    except (KeyError, TypeError, ValueError):
        return False
    if not math.isfinite(value) or abs(value) >= 1e29:
        return False
    return not any(abs(value - sentinel) <= max(1e-6, abs(sentinel) * 1e-7) for sentinel in (-99999.0, -9999.0, -999.25, -999.0, 999.25, 9999.0, 99999.0))


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _dev_file_rows(well: dict[str, Any]) -> list[dict[str, Any]]:
    """Read every linked DEV once, retaining enough evidence for copy detection."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in well.get("_dev_paths") or []:
        path = Path(raw_path)
        identity = str(path.resolve()).lower()
        if identity in seen:
            continue
        seen.add(identity)
        stations: list[dict[str, float]] = []
        error = None
        try:
            stations = parse_dev_stations(path) if path.is_file() else []
            if len(stations) < 2:
                error = "空文件或有效测点少于 2 个"
        except (OSError, ValueError) as exc:
            error = str(exc)
        rows.append({
            "path": str(path), "file": path.name, "bytes": path.stat().st_size if path.is_file() else None,
            "sha256": _sha256(path) if path.is_file() else None,
            "station_count": len(stations), "valid": len(stations) >= 2, "error": error,
            "min_md": stations[0]["md"] if stations else None,
            "max_md": stations[-1]["md"] if stations else None,
            "max_tvd": max((row["tvd"] for row in stations), default=None), "_stations": stations,
        })
    return sorted(rows, key=lambda row: row["path"].lower())


def _dev_comparisons(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    fields = ("md", "x", "y", "z", "tvd")
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            base = {"left_file": left["file"], "left_path": left["path"], "right_file": right["file"], "right_path": right["path"]}
            if not left["valid"] or not right["valid"]:
                comparisons.append({**base, "status": "unavailable", "label": "至少一份 DEV 为空或无法解析", "station_equal": False})
                continue
            if left["sha256"] and left["sha256"] == right["sha256"]:
                comparisons.append({**base, "status": "byte_identical", "label": "文件字节完全一致 · 可视为直接拷贝件", "station_equal": True})
                continue
            if left["station_count"] != right["station_count"]:
                comparisons.append({**base, "status": "different", "label": "有效测点数不同 · 不是完全拷贝件", "station_equal": False,
                                    "left_station_count": left["station_count"], "right_station_count": right["station_count"]})
                continue
            deltas = {field: max(abs(float(a[field]) - float(b[field])) for a, b in zip(left["_stations"], right["_stations"])) for field in fields}
            station_equal = all(value <= 1e-7 for value in deltas.values())
            comparisons.append({
                **base,
                "status": "station_identical" if station_equal else "different",
                "label": "测点内容完全一致 · 文件格式或空白不同" if station_equal else "测点内容存在差异 · 非完全拷贝件",
                "station_equal": station_equal,
                "max_md_delta": round(deltas["md"], 7), "max_xyz_delta": round(max(deltas["x"], deltas["y"], deltas["z"]), 7),
                "max_tvd_delta": round(deltas["tvd"], 7),
            })
    return comparisons


def _trajectory(
    well: dict[str, Any], interval: dict[str, Any] | None,
    dev_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    dev_rows = dev_rows if dev_rows is not None else _dev_file_rows(well)
    valid_rows = [row for row in dev_rows if row["valid"]]
    comparisons = _dev_comparisons(dev_rows)
    if not valid_rows:
        return {
            "exists": False, "station_count": 0, "files": [{key: value for key, value in row.items() if key != "_stations"} for row in dev_rows],
            "comparisons": comparisons, "method": "没有可用 DEV，不能可靠完成 MD→TVD/空间位置转换",
            "comparison_method": "先比较 SHA-256 是否字节一致；否则逐测点比较 MD / X / Y / Z / TVD，容差 1e-7。",
        }
    selected = sorted(valid_rows, key=lambda row: (-row["station_count"], row["path"].lower()))[0]
    stations = selected["_stations"]
    # Projection comparison is a deliberately on-demand operation.  Keep a
    # representative path only (first/last plus evenly spaced stations) so a
    # four-well comparison never retains a complete legacy DEV in browser RAM.
    point_limit = 360
    stride = max(1, math.ceil(len(stations) / point_limit))
    map_points = [
        {key: float(point[key]) for key in ("md", "x", "y", "z", "tvd")}
        for index, point in enumerate(stations)
        if index % stride == 0 or index == len(stations) - 1
    ]
    def interpolate(md: float | None) -> dict[str, float] | None:
        if md is None or md < stations[0]["md"] or md > stations[-1]["md"]:
            return None
        right_index = next((index for index, row in enumerate(stations) if row["md"] >= md), len(stations) - 1)
        if right_index == 0:
            row = stations[0]
            return {key: row[key] for key in ("md", "x", "y", "z", "tvd")}
        left, right = stations[right_index - 1], stations[right_index]
        fraction = 0 if right["md"] == left["md"] else (md - left["md"]) / (right["md"] - left["md"])
        return {"md": md, **{key: left[key] + (right[key] - left[key]) * fraction for key in ("x", "y", "z", "tvd")}}
    return {
        "exists": True, "source": selected["path"], "station_count": selected["station_count"],
        "min_md": selected["min_md"], "max_md": selected["max_md"], "max_tvd": selected["max_tvd"],
        "interval_top": interpolate(interval.get("top_md") if interval else None),
        "interval_base": interpolate(interval.get("base_md") if interval else None),
        "map_points": map_points,
        "map_point_method": f"用于投影绘制的 {len(map_points)} 个抽稀测点（原始有效测点 {len(stations)} 个）",
        "files": [{**{key: value for key, value in row.items() if key != "_stations"}, "selected": row["path"] == selected["path"]} for row in dev_rows],
        "comparisons": comparisons, "method": "从有效测点最多的 DEV 进行线性插值：MD 转换为 TVD / XYZ。",
        "comparison_method": "先比较 SHA-256 是否字节一致；否则逐测点比较 MD / X / Y / Z / TVD，容差 1e-7。",
    }


def _interpolate_trajectory_station(stations: list[dict[str, float]], md: float) -> dict[str, float] | None:
    """Linearly interpolate one absolute-XY trajectory at a requested MD."""
    if not stations or md < stations[0]["md"] or md > stations[-1]["md"]:
        return None
    right_index = next((index for index, row in enumerate(stations) if row["md"] >= md), len(stations) - 1)
    if right_index == 0:
        return {key: float(stations[0][key]) for key in ("md", "x", "y", "z", "tvd")}
    left, right = stations[right_index - 1], stations[right_index]
    fraction = 0 if right["md"] == left["md"] else (md - left["md"]) / (right["md"] - left["md"])
    return {"md": md, **{
        key: float(left[key]) + (float(right[key]) - float(left[key])) * fraction
        for key in ("x", "y", "z", "tvd")
    }}


def _ofm_trajectory(conn: sqlite3.Connection, well_keys: set[str]) -> tuple[dict[str, Any], list[dict[str, float]]]:
    """Build an absolute-coordinate OFM trajectory without reading MDB again.

    OFM_DATA_Deviation keeps MD/TVD and XDELT/YDELT, so each point is made
    spatially comparable with an external Petrel DEV by adding MAESTRA X/Y.
    Z uses KB - TVD when available.  Comparison itself uses X/Y/TVD, avoiding
    a false mismatch when a legacy DEV uses a different vertical datum.
    """
    if not well_keys:
        return {"exists": False, "station_count": 0}, []
    placeholders = ",".join("?" for _ in well_keys)
    rows = [dict(row) for row in conn.execute(
        f"""SELECT od.well_key,od.well_name,od.md,od.tvd,od.x_offset,od.y_offset,
                   ow.x AS wellhead_x,ow.y AS wellhead_y,ow.kb_elevation,
                   s.filename AS source_file
            FROM ofm_deviation_stations od
            JOIN sources s ON s.id=od.source_id
            LEFT JOIN ofm_wells ow ON ow.source_id=od.source_id AND ow.well_key=od.well_key
            WHERE od.well_key IN ({placeholders})
            ORDER BY od.md,od.id""", tuple(well_keys)
    )]
    valid = [row for row in rows if all(row.get(field) is not None for field in ("md", "tvd", "x_offset", "y_offset", "wellhead_x", "wellhead_y"))]
    if not valid:
        return {
            "exists": False, "station_count": len(rows), "usable_station_count": 0,
            "method": "MDB 中没有可与井口坐标合成绝对 XY 的完整轨迹测点。",
        }, []
    first = valid[0]
    kb = float(first["kb_elevation"]) if first.get("kb_elevation") is not None else 0.0
    stations = [{
        "md": float(row["md"]),
        "x": float(row["wellhead_x"]) + float(row["x_offset"]),
        "y": float(row["wellhead_y"]) + float(row["y_offset"]),
        "z": kb - float(row["tvd"]),
        "tvd": float(row["tvd"]),
    } for row in valid]
    point_limit = 240
    stride = max(1, math.ceil(len(stations) / point_limit))
    summary = {
        "exists": True, "well_key": first["well_key"], "well_name": first["well_name"],
        "source_file": first["source_file"], "station_count": len(rows), "usable_station_count": len(stations),
        "min_md": stations[0]["md"], "max_md": stations[-1]["md"],
        "min_tvd": min(row["tvd"] for row in stations), "max_tvd": max(row["tvd"] for row in stations),
        "wellhead_x": first["wellhead_x"], "wellhead_y": first["wellhead_y"],
        "kb_elevation": first.get("kb_elevation"),
        "map_points": [row for index, row in enumerate(stations) if index % stride == 0 or index == len(stations) - 1],
        "method": "绝对 XY = MDB MAESTRA 井口 X/Y + OFM_DATA_Deviation XDELT/YDELT；轨迹对比以绝对 X/Y 与 TVD 为准。",
    }
    return summary, stations


def _compare_external_dev_to_ofm(external_rows: list[dict[str, Any]], ofm_stations: list[dict[str, float]]) -> list[dict[str, Any]]:
    """Compare every valid external DEV to the MDB trajectory on common MD."""
    results: list[dict[str, Any]] = []
    if not ofm_stations:
        return results
    for external in external_rows:
        base = {
            "external_file": external["file"], "external_path": external["path"],
            "external_station_count": external["station_count"], "mdb_station_count": len(ofm_stations),
        }
        stations = external.get("_stations") or []
        if not external.get("valid") or len(stations) < 2:
            results.append({**base, "status": "unavailable", "label": "外部 DEV 为空、测点不足或无法解析"})
            continue
        start, stop = max(float(stations[0]["md"]), ofm_stations[0]["md"]), min(float(stations[-1]["md"]), ofm_stations[-1]["md"])
        if stop <= start:
            results.append({**base, "status": "no_overlap", "label": "与 MDB 轨迹没有共同 MD 段", "common_md_start": start, "common_md_stop": stop})
            continue
        # Fixed dense sampling compares shapes even when station spacing differs.
        sample_count = min(201, max(41, int((stop - start) / 10) + 1))
        distances, xy_distances, tvd_deltas = [], [], []
        for index in range(sample_count):
            md = start + (stop - start) * index / (sample_count - 1)
            left, right = _interpolate_trajectory_station(stations, md), _interpolate_trajectory_station(ofm_stations, md)
            if not left or not right:
                continue
            xy_delta = math.hypot(left["x"] - right["x"], left["y"] - right["y"])
            tvd_delta = abs(left["tvd"] - right["tvd"])
            xy_distances.append(xy_delta)
            tvd_deltas.append(tvd_delta)
            distances.append(math.hypot(xy_delta, tvd_delta))
        if not distances:
            results.append({**base, "status": "unavailable", "label": "共同 MD 段无法插值比较"})
            continue
        max_distance, mean_distance = max(distances), sum(distances) / len(distances)
        max_xy, max_tvd = max(xy_distances), max(tvd_deltas)
        if max_distance <= .05:
            status, label = "consistent", "与 MDB 轨迹高度一致（最大三维差 ≤ 0.05 m）"
        elif max_distance <= 2:
            status, label = "near", "与 MDB 轨迹接近（建议复核坐标/基准）"
        elif max_distance <= 10:
            status, label = "different", "与 MDB 轨迹存在可见差异"
        else:
            status, label = "different", "与 MDB 轨迹差异明显"
        results.append({
            **base, "status": status, "label": label, "sample_count": len(distances),
            "common_md_start": round(start, 3), "common_md_stop": round(stop, 3),
            "mean_3d_distance": round(mean_distance, 4), "max_3d_distance": round(max_distance, 4),
            "max_xy_distance": round(max_xy, 4), "max_tvd_delta": round(max_tvd, 4),
            "method": "共同 MD 段线性插值后比较绝对 X/Y 和 TVD；外部 Z 不参与，以避免 KB / 海拔基准不同造成误判。",
        })
    return results


def _best_wells(query: str, wells: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    candidates = []
    for well in wells:
        score, reasons = well_match_score(query, well["canonical_name"])
        key_score, key_reasons = well_match_score(query, well["project_key"])
        if key_score > score:
            score, reasons = key_score, key_reasons
        candidates.append({
            "project_key": well["project_key"], "id": well["id"], "name": well["canonical_name"],
            "score": round(score, 3), "reasons": reasons, "source_types": well["source_types"],
            "x": well.get("x"), "y": well.get("y"),
        })
    return sorted(candidates, key=lambda row: (row["score"], len(row["source_types"])), reverse=True)[:limit]


def _catalog_matches(conn: sqlite3.Connection, project_root: str, tokens: list[str]) -> list[dict[str, Any]]:
    if not tokens:
        return []
    candidates = [dict(row) for row in conn.execute(
        """SELECT id,filename,relative_path,file_path,category_key,bytes,representative
             FROM project_catalog_items WHERE project_root=?""", (project_root,)
    )]
    matches: list[dict[str, Any]] = []
    for row in candidates:
        best_score, reason = 0.0, None
        filename_lower = row["filename"].lower()
        relative_lower = row["relative_path"].lower()
        filename_compact = re.sub(r"[^a-z0-9]", "", filename_lower)
        for token in tokens:
            token_lower = token.lower()
            token_compact = re.sub(r"[^a-z0-9]", "", token_lower)
            token_digits = re.findall(r"\d+", token_lower)
            if token_lower in filename_lower or token_lower in relative_lower:
                score, token_reason = 1.0, "精确包含"
            elif token_compact and token_compact in filename_compact:
                score, token_reason = .94, "忽略符号"
            else:
                score = max(
                    identity_similarity(Path(token).stem, Path(row["filename"]).stem),
                    SequenceMatcher(None, token_compact, filename_compact).ratio() if token_compact else 0.0,
                )
                if token_digits and re.findall(r"\d+", filename_lower) != token_digits:
                    score = 0.0
                token_reason = "名称模糊匹配"
            if score > best_score:
                best_score, reason = score, token_reason
        if best_score >= .72:
            matches.append({**row, "match_score": round(best_score, 3), "match_reason": reason})
    return sorted(matches, key=lambda row: (-row["match_score"], not row["representative"], row["filename"].upper()))[:30]


def _single_relationship_search(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    wells: list[dict[str, Any]],
    query: str,
    forced_layer: str | None = None,
    forced_well: bool = False,
) -> dict[str, Any]:
    tokens = _tokens(query)
    if not tokens:
        raise ValueError("请输入井名、层名或曲线名")
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    layer_aliases, canonical_layers = _canonical_layer_lookup(conn, project_root)
    type_rows, curve_aliases = _curve_types(conn, project_root)
    assignments = {row["mnemonic"].upper(): row["type_key"] for row in conn.execute(
        "SELECT mnemonic,type_key FROM curve_type_assignments WHERE project_root=?", (project_root,)
    )}

    first_candidates = _best_wells(tokens[0], wells)
    first_exact_curve = normalize_header(tokens[0]) in curve_aliases
    select_well = bool(first_candidates and first_candidates[0]["score"] >= .72 and (
        forced_well or ((re.search(r"\d", tokens[0]) or first_candidates[0]["score"] >= .92)
        and not (len(tokens) == 1 and first_exact_curve and first_candidates[0]["score"] < .98))
    ))
    selected = next((row for row in wells if select_well and row["project_key"] == first_candidates[0]["project_key"]), None)
    remaining = tokens[1:] if selected else tokens

    requested_curve, requested_type = None, None
    if forced_layer is None:
        for token in reversed(remaining):
            key = normalize_header(token)
            if key in curve_aliases:
                requested_curve, requested_type = token, curve_aliases[key]
                break
            assigned_type = assignments.get(key)
            suggestion = suggest_curve_type(token)
            if assigned_type or suggestion.get("type_key"):
                requested_curve, requested_type = token, assigned_type or suggestion["type_key"]
                break
        layer_tokens = [token for token in remaining if token != requested_curve]
        requested_layer = layer_tokens[0] if layer_tokens else None
    else:
        requested_layer = forced_layer

    top_files = [dict(row) for row in conn.execute(
        """SELECT file_path,filename,modified_at FROM project_catalog_items
           WHERE project_root=? AND category_key='well_tops' ORDER BY modified_at DESC""", (project_root,)
    ) if Path(row["filename"]).suffix.lower() not in _AUXILIARY_EXTENSIONS]
    all_tops: list[dict[str, Any]] = []
    for item in top_files[:120]:
        for row in parse_well_tops(item["file_path"]):
            year = re.search(r"20\d{2}", item["filename"])
            version = re.search(r"(?i)(?:^|[_\- ])(V\d+|REV\d+)", item["filename"])
            unified_key, unified_name = _canonical_layer(row["surface_key"], layer_aliases, canonical_layers)
            row.update({
                "year": int(year.group()) if year else None, "version": version.group(1).upper() if version else None,
                "unified_key": unified_key, "unified_name": unified_name or row["surface"],
            })
            all_tops.append(row)
    well_tops = [row for row in all_tops if selected and row["well_key"] == selected["project_key"]]
    well_tops.sort(key=lambda row: (row.get("md") is None, row.get("md") or 0, -(row.get("year") or 0)))

    structural_objects = []
    catalog_horizons = [dict(row) for row in conn.execute(
        """SELECT id,filename,file_path,relative_path,representative FROM project_catalog_items
           WHERE project_root=? AND category_key='horizons' ORDER BY representative DESC,filename COLLATE NOCASE""",
        (project_root,),
    )]
    option_by_path = {str(Path(row["path"]).resolve()).lower(): row for row in horizon_options(snapshot) if row.get("path")}
    for row in catalog_horizons:
        if Path(row["filename"]).suffix.lower() in _AUXILIARY_EXTENSIONS:
            continue
        option = option_by_path.get(str(Path(row["file_path"]).resolve()).lower())
        raw_layer_key = _layer_key(Path(row["filename"]).stem)
        unified_key, unified_name = _canonical_layer(raw_layer_key, layer_aliases, canonical_layers)
        structural_objects.append({
            "id": option.get("id") if option else f"surface-catalog:{row['id']}", "name": Path(row["filename"]).stem,
            "file": row["filename"], "path": row["file_path"], "kind": option.get("kind") if option else "catalog_horizon",
            "computable": bool(option and option.get("kind") == "regular_surface"),
            "raw_layer_key": raw_layer_key, "unified_key": unified_key, "unified_name": unified_name or Path(row["filename"]).stem,
        })

    layer_key, canonical_query_name = _canonical_layer(_layer_key(requested_layer or ""), layer_aliases, canonical_layers)
    requested_layer_display = canonical_query_name or requested_layer
    global_top_matches = [row for row in all_tops if layer_key and row["unified_key"] == layer_key]
    top_matches = [row for row in well_tops if layer_key and row["unified_key"] == layer_key]
    structural_matches = [row for row in structural_objects if layer_key and row["unified_key"] == layer_key]
    top_matches.sort(key=lambda row: (row.get("year") or 0, row.get("md") is not None), reverse=True)
    interval = None
    if top_matches:
        chosen = top_matches[0]
        deeper = [row for row in well_tops if row.get("md") is not None and chosen.get("md") is not None and row["md"] > chosen["md"] + .01 and row["unified_key"] != chosen["unified_key"]]
        same_source = [row for row in deeper if row["source_file"] == chosen["source_file"]]
        base = (same_source or deeper or [None])[0]
        interval = {
            "top_md": chosen.get("md"), "base_md": base.get("md") if base else None,
            "top_name": chosen["unified_name"], "base_name": base.get("unified_name") if base else None,
            "source": chosen["source_file"], "method": "Well Top 顶界至同版本下一更深层顶；不是地层解释厚度" if base else "只有层顶，底界暂取测井或 DEV 终止深度",
        }

    structural_hits = []
    for obj in structural_matches:
        if selected:
            hit = conn.execute(
                "SELECT * FROM horizon_well_hits WHERE project_root=? AND horizon_key=? AND well_key=?",
                (project_root, obj["id"], selected["project_key"]),
            ).fetchone()
            structural_hits.append({**obj, "computed": bool(hit), **(dict(hit) if hit else {})})
        else:
            structural_hits.append({**obj, "computed": False, "requires_well": True})
    selected_top_layer_count = len({row["unified_key"] for row in well_tops})
    selected_structural_rows = []
    if selected:
        selected_structural_rows = [dict(row) for row in conn.execute(
            """SELECT horizon_key,hit,intersection_md,surface_z,method,computed_at
               FROM horizon_well_hits WHERE project_root=? AND well_key=?""",
            (project_root, selected["project_key"]),
        )]
    selected_structural_hit_count = sum(bool(row.get("hit")) for row in selected_structural_rows)
    if selected:
        if interval is None:
            hit = next((row for row in structural_hits if row.get("hit") and row.get("intersection_md") is not None), None)
            if hit:
                interval = {
                    "top_md": hit["intersection_md"] - 25, "base_md": hit["intersection_md"] + 25,
                    "top_name": requested_layer_display, "base_name": None, "source": hit["file"],
                    "method": "构造面交会 MD 上下各 25 m 的统计窗；不是地层顶底界",
                }

    las_files_checked, las_errors, curve_rows = [], [], []
    if selected:
        for path in list(dict.fromkeys(selected.get("_las_paths") or []))[:6]:
            try:
                parsed = _read_las(path)
            except (OSError, ValueError) as exc:
                las_errors.append({"file": Path(path).name, "error": str(exc)})
                continue
            las_files_checked.append(Path(path).name)
            for curve in parsed.get("curves", [])[1:]:
                mnemonic = curve.get("mnemonic") or ""
                if is_time_depth_mnemonic(mnemonic) or not curve.get("sample_count"):
                    continue
                type_key = _curve_type_for(mnemonic, curve.get("description"), assignments)
                curve_rows.append({
                    **curve, "type_key": type_key, "type_name": type_rows.get(type_key, {}).get("name") if type_key else "未分类",
                    "file": Path(path).name, "path": path, "log_start": parsed.get("start"), "log_stop": parsed.get("stop"),
                    "depth_unit": parsed.get("depth_unit"),
                })

    curve_key = normalize_header(requested_curve or "")
    matching_curves = [row for row in curve_rows if requested_curve and (
        normalize_header(row["mnemonic"]) == curve_key or (requested_type and row.get("type_key") == requested_type)
    )]
    analysis_curve = None
    selection_mode = "requested" if requested_curve else None
    if matching_curves:
        top_md, base_md = (interval or {}).get("top_md"), (interval or {}).get("base_md")
        for row in matching_curves:
            samples = row.get("samples") or []
            row["interval_sample_count"] = sum(1 for sample in samples if (top_md is None or sample["md"] >= top_md) and (base_md is None or sample["md"] <= base_md))
        analysis_curve = max(matching_curves, key=lambda row: (row["interval_sample_count"], row.get("sample_count") or 0))
    elif selected and not requested_curve and curve_rows:
        # A well-only query should be immediately useful. Prefer the common GR
        # family for the preview, then other classified physical logs, while
        # still returning every original mnemonic (including unclassified).
        priority = {"gamma_ray": 6, "sp": 5, "density": 4, "neutron": 3, "sonic": 2, "caliper": 1}
        analysis_curve = max(curve_rows, key=lambda row: (priority.get(row.get("type_key"), 0), row.get("sample_count") or 0))
        analysis_curve["interval_sample_count"] = len(analysis_curve.get("samples") or [])
        selection_mode = "default_preview"
    curve_samples = [row for row in ((analysis_curve or {}).get("samples") or []) if _valid_curve_sample(row)]
    interval_samples = curve_samples
    if interval:
        interval_samples = [row for row in curve_samples if (interval.get("top_md") is None or row["md"] >= interval["top_md"]) and (interval.get("base_md") is None or row["md"] <= interval["base_md"])]
    stats, histogram = _sample_stats(interval_samples)

    if interval and interval.get("base_md") is None:
        fallback = (analysis_curve or {}).get("log_stop")
        if fallback is None and selected:
            fallback = selected.get("max_survey_md") or selected.get("total_depth")
        interval["base_md"] = fallback
        if fallback is not None:
            interval_samples = [row for row in curve_samples if row["md"] >= interval["top_md"] and row["md"] <= fallback]
            stats, histogram = _sample_stats(interval_samples)

    external_dev_rows = _dev_file_rows(selected) if selected else []
    trajectory = _trajectory(selected, interval, external_dev_rows) if selected else {"exists": False, "station_count": 0}
    perforations = []
    production_summary = None
    production_months = []
    production_events = []
    production_sources = []
    ofm_well = None
    ofm_deviation = {"count": 0, "min_md": None, "max_md": None}
    ofm_trajectory = {"exists": False, "station_count": 0, "external_comparisons": []}
    ofm_markers: list[dict[str, Any]] = []
    if selected:
        lookup_keys = {selected["project_key"], normalize_well_name(selected["canonical_name"])}
        placeholders = ",".join("?" for _ in lookup_keys)
        perforations = [dict(row) for row in conn.execute(
            f"SELECT * FROM production_intervals WHERE well_key IN ({placeholders}) ORDER BY top_md", tuple(lookup_keys)
        )]
        production_months = [dict(row) for row in conn.execute(
            f"SELECT * FROM production_monthly WHERE well_key IN ({placeholders}) ORDER BY production_month,id", tuple(lookup_keys)
        )]
        production_events = [dict(row) for row in conn.execute(
            f"SELECT * FROM production_events WHERE well_key IN ({placeholders}) ORDER BY event_date,id", tuple(lookup_keys)
        )]
        production_summaries = summarize_production(production_months, production_events)
        production_summary = production_summaries[0] if production_summaries else None
        ofm_well_row = conn.execute(
            f"SELECT * FROM ofm_wells WHERE well_key IN ({placeholders}) ORDER BY id DESC LIMIT 1", tuple(lookup_keys)
        ).fetchone()
        ofm_well = dict(ofm_well_row) if ofm_well_row else None
        deviation_row = conn.execute(
            f"SELECT COUNT(*) AS count,MIN(md) AS min_md,MAX(md) AS max_md FROM ofm_deviation_stations WHERE well_key IN ({placeholders})",
            tuple(lookup_keys),
        ).fetchone()
        if deviation_row:
            ofm_deviation = dict(deviation_row)
        ofm_lookup_keys = set(lookup_keys)
        if ofm_well and ofm_well.get("well_key"):
            ofm_lookup_keys.add(str(ofm_well["well_key"]))
        ofm_trajectory, ofm_stations = _ofm_trajectory(conn, ofm_lookup_keys)
        ofm_trajectory["external_comparisons"] = _compare_external_dev_to_ofm(external_dev_rows, ofm_stations)
        ofm_markers = [dict(row) for row in conn.execute(
            f"SELECT marker_name,depth_md,marker_date,picker FROM ofm_markers WHERE well_key IN ({placeholders}) ORDER BY depth_md LIMIT 40",
            tuple(lookup_keys),
        )]
        source_ids = {row.get("source_id") for row in [*perforations, *production_months, *production_events] if row.get("source_id") is not None}
        if ofm_well and ofm_well.get("source_id") is not None:
            source_ids.add(ofm_well["source_id"])
        source_ids = sorted(source_ids)
        if source_ids:
            source_placeholders = ",".join("?" for _ in source_ids)
            production_sources = [dict(row) for row in conn.execute(
                f"SELECT id,filename,file_path,batch,version,data_type FROM sources WHERE id IN ({source_placeholders}) ORDER BY filename",
                tuple(source_ids),
            )]
            for source in production_sources:
                source["is_ofm_mdb"] = Path(str(source.get("file_path") or source["filename"])).suffix.lower() in {".mdb", ".accdb"}

    available_tops = []
    seen_top = set()
    for row in well_tops:
        signature = (row["surface"], row.get("md"), row["source_file"])
        if signature not in seen_top:
            seen_top.add(signature)
            available_tops.append(row)

    layer_options_map: dict[str, dict[str, Any]] = {}
    for row in well_tops:
        option = layer_options_map.setdefault(row["unified_key"], {
            "value": row["unified_name"], "name": row["unified_name"], "well_top_records": 0,
            "md_min": None, "md_max": None, "structural_objects": [],
        })
        option["well_top_records"] += 1
        if row.get("md") is not None:
            option["md_min"] = row["md"] if option["md_min"] is None else min(option["md_min"], row["md"])
            option["md_max"] = row["md"] if option["md_max"] is None else max(option["md_max"], row["md"])
    for row in structural_objects:
        key = row["unified_key"]
        option = layer_options_map.setdefault(key, {
            "value": row["unified_name"], "name": row["unified_name"], "well_top_records": 0,
            "md_min": None, "md_max": None, "structural_objects": [],
        })
        option["structural_objects"].append({key: row.get(key) for key in ("id", "name", "file", "kind", "computable")})
    layer_options = sorted(
        layer_options_map.values(),
        key=lambda row: (row["well_top_records"] == 0, row["md_min"] is None, row["md_min"] or 0, row["name"]),
    )

    layer_suggestions = []
    if requested_layer and not top_matches and not structural_matches:
        names = sorted({row["unified_name"] for row in well_tops} | {row["unified_name"] for row in structural_objects})
        for name in names:
            score = SequenceMatcher(None, layer_key, _layer_key(name)).ratio()
            if score >= .5:
                layer_suggestions.append({"name": name, "score": round(score, 3)})
        layer_suggestions.sort(key=lambda row: row["score"], reverse=True)

    findings = []
    if selected:
        findings.append(f"井名匹配到 {selected['canonical_name']}（{first_candidates[0]['score']:.0%}）")
    else:
        findings.append("未确定唯一井对象；以下按资料对象名称检索")
    if requested_layer:
        if not selected and global_top_matches:
            findings.append(f"全工区有 {len(global_top_matches)} 条 {requested_layer} Well Top 记录，涉及 {len({row['well_key'] for row in global_top_matches})} 口井")
        elif not selected:
            findings.append(f"全工区没有 {requested_layer} Well Top 记录")
        elif top_matches:
            findings.append(f"井上存在 {requested_layer} Well Top，共 {len(top_matches)} 个版本记录")
        else:
            findings.append(f"井上没有 {requested_layer} Well Top")
        if structural_matches:
            findings.append(f"同时找到 {len(structural_matches)} 个同名构造面对象")
        else:
            findings.append(f"未找到同名 {requested_layer} 构造面")
    elif selected:
        findings.append(
            f"层位证据：该井匹配 {selected_top_layer_count} 个 Well Top 层位、{len(well_tops)} 条分层记录"
            if well_tops else "层位证据：该井没有读取到 Well Top 分层记录"
        )
        if selected_structural_rows:
            findings.append(
                f"未指定构造面：该井已有 {len(selected_structural_rows)} 个构造面版本完成交会计算，钻遇 {selected_structural_hit_count} 个"
            )
        else:
            findings.append(f"未指定构造面：工区有 {len(structural_objects)} 个层位对象，尚未对该井计算空间交会")
    if requested_curve:
        if not selected:
            findings.append(f"已识别 {requested_curve} 为 {type_rows.get(requested_type, {}).get('name', requested_type or '未分类')}；指定井名后可计算深度段与分布")
        else:
            findings.append(f"找到 {len(matching_curves)} 条 {requested_curve} / {type_rows.get(requested_type, {}).get('name', requested_type or '未分类')} 候选曲线" if matching_curves else f"没有找到有效 {requested_curve} 曲线；已列出该井其他有效曲线")
    elif selected and analysis_curve:
        findings.append(f"未指定曲线：默认预览 {analysis_curve['mnemonic']}；可从 {len(curve_rows)} 条原始曲线记录中继续选择")
    elif selected:
        findings.append("未指定曲线，且该井没有读取到有效 LAS 曲线")
    if selected:
        findings.append(f"射孔层段 {len(perforations)} 段" if perforations else "当前没有传入该井射孔层段")
        if production_summary:
            findings.append(f"生产动态 {production_summary['production_months']} 个月，投产 {production_summary['onstream_date'] or '时间待补'}")
        if ofm_trajectory.get("exists"):
            findings.append(
                f"MDB 井头：{ofm_trajectory.get('source_file')} · XY {ofm_trajectory.get('wellhead_x')}, {ofm_trajectory.get('wellhead_y')}；"
                f"MDB 轨迹 {ofm_trajectory.get('usable_station_count')} 点，MD {ofm_trajectory.get('min_md')}–{ofm_trajectory.get('max_md')}"
            )
            comparisons = ofm_trajectory.get("external_comparisons") or []
            if comparisons:
                findings.append(f"外部 DEV 与 MDB 已比较 {len(comparisons)} 份，首份结果：{comparisons[0].get('label')}")
        elif ofm_well:
            findings.append("MDB 已匹配井头信息，但未读取到可用 MDB 井轨迹")
    else:
        findings.append("指定井名后可继续检查 DEV 与射孔层段")

    catalog_matches = _catalog_matches(conn, project_root, tokens)
    return {
        "query": query, "tokens": tokens,
        "interpretation": {"well": tokens[0] if selected else None, "layer": requested_layer, "curve": requested_curve, "curve_type": requested_type},
        "well": {
            "status": "matched" if selected else "not_resolved", "selected": ({
                "id": selected["id"], "project_key": selected["project_key"], "name": selected["canonical_name"],
                "x": selected.get("x"), "y": selected.get("y"), "source_types": selected["source_types"],
                "total_depth": selected.get("total_depth"), "max_survey_md": selected.get("max_survey_md"),
                "log_start_md": selected.get("log_start_md"), "log_stop_md": selected.get("log_stop_md"),
                "match_score": first_candidates[0]["score"], "match_reasons": first_candidates[0]["reasons"],
                "evidence_files": [{key: source.get(key) for key in ("data_type", "filename", "file_path", "catalog_id", "batch", "version", "x", "y")}
                                   for source in selected.get("_sources", [])],
            } if selected else None), "candidates": first_candidates,
            "note": "模糊结果仅用于本次检索，不会自动合并井身份",
        },
        "layer": {
            "query": requested_layer, "well_top_matches": top_matches, "structural_matches": structural_hits,
            "selected_well_top_layers": selected_top_layer_count,
            "selected_well_top_records": len(well_tops),
            "selected_structural_computed": len(selected_structural_rows),
            "selected_structural_hits": selected_structural_hit_count,
            "structural_object_count": len(structural_objects),
            "global_well_top_count": len(global_top_matches),
            "global_well_top_wells": len({row["well_key"] for row in global_top_matches}),
            "global_well_top_samples": global_top_matches[:20],
            "options": layer_options,
            "interval": interval, "available_tops": available_tops[:80], "suggestions": layer_suggestions[:8],
            "explanation": "Well Top 是该井上的分层点；构造面是空间解释面。二者同名时分别列证据，不相互替代。",
        },
        "curve": {
            "query": requested_curve, "requested_type": requested_type,
            "status": "matched" if matching_curves else ("default_selected" if analysis_curve and not requested_curve else ("missing" if requested_curve and selected else "object_only")),
            "selection_mode": selection_mode,
            "default_reason": "优先选择 GR，其次选择已分类且有效样点最多的原始曲线" if selection_mode == "default_preview" else None,
            "selected": ({key: analysis_curve.get(key) for key in ("mnemonic", "unit", "description", "sample_count", "type_key", "type_name", "file", "log_start", "log_stop")} if analysis_curve else None),
            "matching_curves": [{key: row.get(key) for key in ("mnemonic", "unit", "sample_count", "type_key", "type_name", "file", "log_start", "log_stop", "interval_sample_count")} for row in matching_curves],
            "available_curves": [{key: row.get(key) for key in ("mnemonic", "unit", "sample_count", "type_key", "type_name", "file", "log_start", "log_stop")} for row in curve_rows[:160]],
            "stats": stats, "samples": curve_samples, "interval_samples": interval_samples,
            "histogram": histogram, "files_checked": las_files_checked, "errors": las_errors,
            "sampling_note": "统计图按需从该井 LAS 抽样读取，原始文件不复制、不改写。",
        },
        "trajectory": trajectory,
        "ofm_trajectory": ofm_trajectory,
        "perforations": {"count": len(perforations), "rows": perforations},
        "production": {
            "summary": production_summary, "months": production_months, "events": production_events,
            "sources": production_sources, "from_ofm_mdb": any(row["is_ofm_mdb"] for row in production_sources),
            "record_counts": {"perforations": len(perforations), "months": len(production_months), "events": len(production_events)},
            "formation": (ofm_well or {}).get("zone_name") or "/".join(dict.fromkeys(str(row.get("interval_name")) for row in perforations if row.get("interval_name"))) or None,
            "ofm_well": ({key: (ofm_well or {}).get(key) for key in ("well_name", "alias", "zone_name", "field_name", "completion_date", "well_type", "status", "interest", "x", "y")} if ofm_well else None),
            "ofm_deviation": ofm_deviation, "markers": ofm_markers,
            "chart_fields": PRODUCTION_SERIES_FIELDS,
        },
        "catalog_matches": catalog_matches,
        "findings": findings,
        "scope_note": "关联检索绕过当前井点筛选，以完整工区索引为范围；只读访问原文件。",
    }


def _distance(left: dict[str, Any], right: dict[str, Any], prefix: str = "") -> float | None:
    x_key, y_key = f"{prefix}x", f"{prefix}y"
    if None in (left.get(x_key), left.get(y_key), right.get(x_key), right.get(y_key)):
        return None
    return math.hypot(float(left[x_key]) - float(right[x_key]), float(left[y_key]) - float(right[y_key]))


def _well_bottom(well: dict[str, Any]) -> dict[str, float | None]:
    for path in well.get("_dev_paths") or []:
        try:
            stations = parse_dev_stations(path)
        except (OSError, ValueError):
            continue
        if stations:
            row = stations[-1]
            return {"bottom_x": row.get("x"), "bottom_y": row.get("y"), "bottom_z": row.get("z"), "bottom_md": row.get("md")}
    return {"bottom_x": None, "bottom_y": None, "bottom_z": None, "bottom_md": None}


def _multi_well_search(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    wells: list[dict[str, Any]],
    query: str,
) -> dict[str, Any]:
    tokens = _tokens(query)
    if len(tokens) < 2:
        raise ValueError("多井模式至少输入两口井，并用逗号分隔")
    if len(tokens) > 4:
        raise ValueError("一次最多对比 4 口井，避免读取过多单井 LAS / DEV")
    source_by_key = {row["project_key"]: row for row in wells}
    rows, unresolved, duplicate_queries = [], [], []
    selected_keys: set[str] = set()
    for token in tokens:
        result = _single_relationship_search(conn, snapshot, wells, token, forced_well=True)
        selected = (result.get("well") or {}).get("selected")
        if not selected:
            unresolved.append({"query": token, "candidates": (result.get("well") or {}).get("candidates", [])[:4]})
            continue
        if selected["project_key"] in selected_keys:
            duplicate_queries.append({"query": token, "matched": selected["name"]})
            continue
        selected_keys.add(selected["project_key"])
        original = source_by_key[selected["project_key"]]
        curves: dict[str, dict[str, Any]] = {}
        for curve in (result.get("curve") or {}).get("available_curves", []):
            key = normalize_header(curve.get("mnemonic") or "")
            if not key:
                continue
            if key not in curves or (curve.get("sample_count") or 0) > (curves[key].get("sample_count") or 0):
                curves[key] = curve
        layers = {
            _layer_key(layer["name"]): layer for layer in (result.get("layer") or {}).get("options", [])
            if layer.get("well_top_records")
        }
        trajectory = result.get("trajectory") or {}
        row = {
            "query": token, **selected, **_well_bottom(original),
            "trajectory": trajectory,
            "curves": curves, "layers": layers,
            "curve_count": len(curves), "curve_type_count": len({item.get("type_key") for item in curves.values() if item.get("type_key")}),
            "layer_count": len(layers), "structural_hit_count": (result.get("layer") or {}).get("selected_structural_hits", 0),
            "perforation_count": (result.get("perforations") or {}).get("count", 0),
            "production_months": ((result.get("production") or {}).get("summary") or {}).get("production_months", 0),
        }
        rows.append(row)
    if not rows:
        raise ValueError("没有匹配到可对比的井")

    well_keys = [row["project_key"] for row in rows]
    curve_keys = sorted({key for row in rows for key in row["curves"]})
    layer_keys = sorted({key for row in rows for key in row["layers"]})
    curve_matrix = []
    for key in curve_keys:
        cells = {row["project_key"]: row["curves"].get(key) for row in rows}
        present = sum(value is not None for value in cells.values())
        sample = next(value for value in cells.values() if value is not None)
        curve_matrix.append({
            "key": key, "name": sample.get("mnemonic") or key, "type_key": sample.get("type_key"),
            "type_name": sample.get("type_name") or "未分类", "present_count": present,
            "common": present == len(rows), "cells": cells,
        })
    curve_matrix.sort(key=lambda row: (-row["present_count"], row["type_name"], row["name"]))
    layer_matrix = []
    for key in layer_keys:
        cells = {row["project_key"]: row["layers"].get(key) for row in rows}
        present = sum(value is not None for value in cells.values())
        sample = next(value for value in cells.values() if value is not None)
        layer_matrix.append({
            "key": key, "name": sample.get("name") or key, "present_count": present,
            "common": present == len(rows), "cells": cells,
        })
    layer_matrix.sort(key=lambda row: (-row["present_count"], row["name"]))

    pairs = []
    for left_index, left in enumerate(rows):
        for right in rows[left_index + 1:]:
            head_distance = _distance(left, right)
            bottom_distance = _distance(left, right, "bottom_")
            pairs.append({
                "left_key": left["project_key"], "left_name": left["name"],
                "right_key": right["project_key"], "right_name": right["name"],
                "head_distance": head_distance, "bottom_distance": bottom_distance,
                "bottom_separation_change": bottom_distance - head_distance if head_distance is not None and bottom_distance is not None else None,
            })
    common_curves = [row["name"] for row in curve_matrix if row["common"]]
    common_layers = [row["name"] for row in layer_matrix if row["common"]]
    findings = [
        f"输入 {len(tokens)} 个井名，匹配到 {len(rows)} 口唯一井" + (f"，{len(unresolved)} 个未解析" if unresolved else ""),
        f"曲线并集 {len(curve_matrix)} 条、全部井共有 {len(common_curves)} 条；层位并集 {len(layer_matrix)} 个、全部井共有 {len(common_layers)} 个",
        f"可计算 {sum(row['head_distance'] is not None for row in pairs)} 组井口距离，单位沿用当前工区坐标单位",
    ]
    if duplicate_queries:
        findings.append(f"有 {len(duplicate_queries)} 个输入名称实际匹配到已选中的同一口井，已避免重复统计")
    return {
        "mode": "multi_well", "query": query, "tokens": tokens, "findings": findings,
        "scope_note": "多井对比绕过当前井点筛选；井距为当前工区平面坐标直线距离，未确认 CRS 时不自动写成米。",
        "multi_well": {
            "wells": rows, "well_keys": well_keys, "unresolved": unresolved, "duplicate_queries": duplicate_queries,
            "pairs": pairs, "curve_matrix": curve_matrix, "layer_matrix": layer_matrix,
            "curve_union_count": len(curve_matrix), "common_curves": common_curves,
            "layer_union_count": len(layer_matrix), "common_layers": common_layers,
        },
        "catalog_matches": _catalog_matches(conn, str(Path(snapshot["project"]["root"]).resolve()), tokens),
    }


def _multi_layer_search(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    wells: list[dict[str, Any]],
    query: str,
) -> dict[str, Any]:
    tokens = _tokens(query)
    if len(tokens) < 3:
        raise ValueError("单井多层位模式需要输入一口井和至少两个层位")
    if len(tokens) > 13:
        raise ValueError("一次最多对比 12 个层位")
    well_token, layer_tokens = tokens[0], tokens[1:]
    base = _single_relationship_search(conn, snapshot, wells, well_token, forced_well=True)
    selected = (base.get("well") or {}).get("selected")
    if not selected:
        raise ValueError("没有确定唯一井，请先使用更完整的井名")
    source_well = next(row for row in wells if row["project_key"] == selected["project_key"])
    layer_rows = []
    for index, layer_name in enumerate(layer_tokens):
        result = _single_relationship_search(conn, snapshot, wells, f"{well_token}, {layer_name}", forced_layer=layer_name, forced_well=True)
        layer = result.get("layer") or {}
        top_matches = layer.get("well_top_matches") or []
        structural = layer.get("structural_matches") or []
        md = z = None
        status, evidence, evidence_file = "missing", "没有井上分层或同名构造面", None
        version_count, md_spread = 0, None
        if top_matches:
            mds = [float(row["md"]) for row in top_matches if row.get("md") is not None]
            chosen = next((row for row in top_matches if row.get("md") is not None), top_matches[0])
            md, z = chosen.get("md"), chosen.get("z")
            status, evidence, evidence_file = "resolved", "Well Top", chosen.get("source_file")
            version_count = len({row.get("source_file") for row in top_matches})
            md_spread = max(mds) - min(mds) if len(mds) > 1 else 0.0 if mds else None
        else:
            hit = next((row for row in structural if row.get("hit") and row.get("intersection_md") is not None), None)
            if hit:
                md, z = hit.get("intersection_md"), hit.get("surface_z")
                status, evidence, evidence_file = "resolved", "构造面—DEV 交会", hit.get("file")
                version_count = len(structural)
            elif any(row.get("computed") for row in structural):
                status, evidence = "not_intersected", "同名构造面已计算，但井轨迹未交会"
                version_count = len(structural)
            elif structural:
                status, evidence = "not_computed", "找到同名构造面，尚未计算 DEV 交会"
                version_count = len(structural)
        point = None
        if md is not None:
            point = (_trajectory(source_well, {"top_md": md, "base_md": md}).get("interval_top"))
        layer_rows.append({
            "input_index": index, "query": layer_name, "key": _layer_key(layer_name),
            "status": status, "evidence": evidence, "file": evidence_file,
            "md": md, "z": z, "point": point, "version_count": version_count, "md_spread": md_spread,
            "suggestions": layer.get("suggestions") or [],
        })
    resolved = sorted([row for row in layer_rows if row.get("md") is not None], key=lambda row: row["md"])
    available_curves: dict[str, dict[str, Any]] = {}
    for curve in (base.get("curve") or {}).get("available_curves", []):
        key = normalize_header(curve.get("mnemonic") or "")
        if key and (key not in available_curves or (curve.get("sample_count") or 0) > (available_curves[key].get("sample_count") or 0)):
            available_curves[key] = curve
    perforations = (base.get("perforations") or {}).get("rows") or []
    segments = []
    for top, bottom in zip(resolved, resolved[1:]):
        top_md, base_md = float(top["md"]), float(bottom["md"])
        full_curves, partial_curves = [], []
        for curve in available_curves.values():
            start, stop = curve.get("log_start"), curve.get("log_stop")
            if start is None or stop is None:
                continue
            low, high = sorted((float(start), float(stop)))
            if low <= top_md and high >= base_md:
                full_curves.append(curve["mnemonic"])
            elif high >= top_md and low <= base_md:
                partial_curves.append(curve["mnemonic"])
        perforation_length = 0.0
        perforation_segments = 0
        for row in perforations:
            if row.get("top_md") is None or row.get("base_md") is None:
                continue
            overlap = max(0.0, min(base_md, float(row["base_md"])) - max(top_md, float(row["top_md"])))
            if overlap:
                perforation_segments += 1
                perforation_length += overlap
        top_point, base_point = top.get("point"), bottom.get("point")
        segments.append({
            "top": top["query"], "base": bottom["query"], "top_md": top_md, "base_md": base_md,
            "md_thickness": base_md - top_md,
            "tvd_thickness": (float(base_point["tvd"]) - float(top_point["tvd"])) if top_point and base_point else None,
            "horizontal_displacement": math.hypot(float(base_point["x"]) - float(top_point["x"]), float(base_point["y"]) - float(top_point["y"])) if top_point and base_point else None,
            "full_curves": sorted(set(full_curves)), "partial_curves": sorted(set(partial_curves)),
            "perforation_segments": perforation_segments, "perforation_length": perforation_length,
        })
    input_resolved_order = [row["key"] for row in layer_rows if row.get("md") is not None]
    depth_order = [row["key"] for row in resolved]
    order_changed = input_resolved_order != depth_order
    deepest_md = resolved[-1]["md"] if resolved else None
    max_md = selected.get("max_survey_md") or selected.get("total_depth") or selected.get("log_stop_md")
    findings = [
        f"井名匹配到 {selected['name']}（{selected['match_score']:.0%}）",
        f"请求 {len(layer_tokens)} 个层位，解析 {len(resolved)} 个；缺失或待计算 {len(layer_rows) - len(resolved)} 个",
    ]
    if resolved:
        findings.append(f"实际钻遇顺序：{' → '.join(row['query'] for row in resolved)}")
    if order_changed:
        findings.append("输入顺序与按 MD 排列的实际钻遇顺序不同，结果已按深度重排")
    if segments:
        findings.append(f"形成 {len(segments)} 个层间段，最浅至最深 MD 跨度 {resolved[-1]['md'] - resolved[0]['md']:.2f}")
    return {
        "mode": "multi_layer", "query": query, "tokens": tokens, "findings": findings,
        "scope_note": "多层位结果优先采用该井 Well Top；没有井上分层时才使用已计算的同名构造面—DEV 交会。所有层间厚度均明确区分 MD 与 TVD。",
        "multi_layer": {
            "well": selected, "layers": layer_rows, "resolved_layers": resolved, "segments": segments,
            "order_changed": order_changed, "requested_count": len(layer_tokens), "resolved_count": len(resolved),
            "deepest_md": deepest_md, "max_md": max_md,
            "drilled_below_deepest": max(0.0, float(max_md) - float(deepest_md)) if max_md is not None and deepest_md is not None else None,
            "available_curve_count": len(available_curves), "perforation_count": len(perforations),
        },
        "catalog_matches": _catalog_matches(conn, str(Path(snapshot["project"]["root"]).resolve()), tokens),
    }


def relationship_search(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    wells: list[dict[str, Any]],
    query: str,
    mode: str = "single",
) -> dict[str, Any]:
    normalized_mode = str(mode or "single").strip().lower()
    if normalized_mode == "multi_well":
        return _multi_well_search(conn, snapshot, wells, query)
    if normalized_mode == "multi_layer":
        return _multi_layer_search(conn, snapshot, wells, query)
    if normalized_mode not in {"single", "single_object"}:
        raise ValueError("未知检索模式")
    result = _single_relationship_search(conn, snapshot, wells, query)
    result["mode"] = normalized_mode
    if normalized_mode == "single_object":
        result["scope_note"] = "单要素模式会自动判断井、层位、构造面、曲线或目录文件；名称相似只作为本次候选，不修改工区归类。"
    return result
