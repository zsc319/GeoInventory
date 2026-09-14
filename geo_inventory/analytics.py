from __future__ import annotations

import json
import math
import re
import sqlite3
from itertools import combinations
from typing import Any

from .geometry import point_in_polygon
from .importers import identity_similarity
from .well_identity import classify_well_identity


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = conn.execute(
        """SELECT
          (SELECT COUNT(*) FROM wells) AS wells,
          (SELECT COUNT(*) FROM sources WHERE status='ready') AS sources,
          (SELECT COUNT(DISTINCT mnemonic) FROM las_curves) AS curve_types,
          (SELECT COUNT(DISTINCT attribute_name) FROM interpretation_values) AS interpretation_types,
          (SELECT COUNT(*) FROM seismic_surveys) AS seismic_surveys,
          (SELECT COUNT(*) FROM polygons) AS polygons,
          (SELECT COUNT(*) FROM wells WHERE x IS NOT NULL AND y IS NOT NULL) AS located_wells"""
    ).fetchone()
    source_breakdown = [dict(row) for row in conn.execute(
        """SELECT s.data_type,COUNT(*) files,SUM(s.record_count) records,
           (SELECT COUNT(DISTINCT ws.well_id) FROM well_sources ws
             JOIN sources sx ON sx.id=ws.source_id
             WHERE sx.status='ready' AND sx.data_type=s.data_type) well_count
           FROM sources s WHERE s.status='ready' GROUP BY s.data_type ORDER BY files DESC"""
    )]
    confidence_counts: dict[str, int] = {}
    for row in wells(conn):
        label = row["identity_status"]
        confidence_counts[label] = confidence_counts.get(label, 0) + 1
    confidence = [
        {"level": level, "count": confidence_counts.get(level, 0)}
        for level in ("多源互证", "单源硬证据", "待核实身份")
    ]
    result = dict(counts)
    # Time/depth channels are calibration relationships rather than physical logs.
    from .curve_analysis import is_time_depth_mnemonic
    result["curve_types"] = sum(not is_time_depth_mnemonic(row["mnemonic"]) for row in conn.execute("SELECT DISTINCT mnemonic FROM las_curves"))
    return {
        **result,
        "source_breakdown": source_breakdown,
        "confidence": confidence,
        "identity_standard": {
            "existence": "Well Head、DEV、LAS、生产动态任一出现，即确认井实体存在",
            "verified": "两类及以上硬数据指向同一标准井名，记为多源互证",
            "single": "只有一类硬数据仍是已存在井，只是缺少跨来源互证",
            "pending": "仅有 Well Top/Checkshot 等辅助记录，或不同来源井口坐标偏差超过 5 个坐标单位",
        },
    }


def wells(conn: sqlite3.Connection, polygon_id: int | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in conn.execute(
        """SELECT w.*,ps.filename preferred_filename,ps.data_type preferred_type,
          COUNT(DISTINCT ws.source_id) source_count,
          GROUP_CONCAT(DISTINCT s.data_type) source_types,
          COUNT(DISTINCT lc.mnemonic) curve_count,
          MIN(lf.start_md) log_start_md,MAX(lf.stop_md) log_stop_md,
          (SELECT MAX(md) FROM deviation_stations d WHERE d.well_id=w.id) max_survey_md,
          MIN(ws.x) source_x_min,MAX(ws.x) source_x_max,
          MIN(ws.y) source_y_min,MAX(ws.y) source_y_max
        FROM wells w
        LEFT JOIN sources ps ON ps.id=w.preferred_source_id
        LEFT JOIN well_sources ws ON ws.well_id=w.id
        LEFT JOIN sources s ON s.id=ws.source_id
        LEFT JOIN las_curves lc ON lc.well_id=w.id
          AND UPPER(REPLACE(REPLACE(lc.mnemonic,'-',''),'_','')) NOT LIKE 'ONEWAYTIME%'
          AND UPPER(REPLACE(REPLACE(lc.mnemonic,'-',''),'_','')) NOT LIKE 'OWT%'
          AND UPPER(REPLACE(REPLACE(lc.mnemonic,'-',''),'_','')) NOT LIKE 'TWT%'
        LEFT JOIN las_files lf ON lf.well_id=w.id
        GROUP BY w.id ORDER BY w.canonical_name"""
    )]
    if polygon_id is not None:
        polygon = conn.execute("SELECT geometry_json,crs FROM polygons WHERE id=?", (polygon_id,)).fetchone()
        if not polygon:
            raise ValueError("Polygon 不存在")
        geometry = json.loads(polygon["geometry_json"])
        rows = [row for row in rows if row["x"] is not None and row["y"] is not None and point_in_polygon(row["x"], row["y"], geometry)]
    for row in rows:
        row["source_types"] = sorted((row["source_types"] or "").split(",")) if row["source_types"] else []
        coordinate_spread = None
        if all(row.get(field) is not None for field in ("source_x_min", "source_x_max", "source_y_min", "source_y_max")):
            coordinate_spread = math.hypot(row["source_x_max"] - row["source_x_min"], row["source_y_max"] - row["source_y_min"])
        row["coordinate_spread"] = coordinate_spread
        row.update(classify_well_identity(row["source_types"], coordinate_spread))
    return rows


def well_detail(conn: sqlite3.Connection, well_id: int) -> dict[str, Any] | None:
    well = conn.execute("SELECT * FROM wells WHERE id=?", (well_id,)).fetchone()
    if not well:
        return None
    occurrences = [dict(row) for row in conn.execute(
        """SELECT ws.*,s.filename,s.data_type,s.batch,s.version,s.imported_at
           FROM well_sources ws JOIN sources s ON s.id=ws.source_id
           WHERE ws.well_id=? ORDER BY s.imported_at DESC""", (well_id,)
    )]
    curves = [dict(row) for row in conn.execute(
        """SELECT lc.mnemonic,lc.unit,lc.description,lc.sample_count,lc.value_min,lc.value_max,
           lf.start_md,lf.stop_md,s.filename,s.batch,s.version
           FROM las_curves lc JOIN las_files lf ON lf.id=lc.las_file_id JOIN sources s ON s.id=lc.source_id
           WHERE lc.well_id=?
             AND UPPER(REPLACE(REPLACE(lc.mnemonic,'-',''),'_','')) NOT LIKE 'ONEWAYTIME%'
             AND UPPER(REPLACE(REPLACE(lc.mnemonic,'-',''),'_','')) NOT LIKE 'OWT%'
             AND UPPER(REPLACE(REPLACE(lc.mnemonic,'-',''),'_','')) NOT LIKE 'TWT%'
           ORDER BY lc.mnemonic,s.imported_at DESC""", (well_id,)
    )]
    interpretations = [dict(row) for row in conn.execute(
        """SELECT iv.attribute_name,COUNT(*) values_count,COUNT(DISTINCT iv.attribute_value) distinct_values,
           MIN(iv.top_md) min_top,MAX(iv.base_md) max_base,GROUP_CONCAT(DISTINCT COALESCE(iv.version,s.version)) versions
           FROM interpretation_values iv JOIN sources s ON s.id=iv.source_id
           WHERE iv.well_id=? GROUP BY iv.attribute_name ORDER BY iv.attribute_name""", (well_id,)
    )]
    coord_spread = coordinate_spread(occurrences)
    well_data = dict(well)
    well_data.update(classify_well_identity({row["data_type"] for row in occurrences}, coord_spread))
    return {"well": well_data, "sources": occurrences, "curves": curves, "interpretations": interpretations, "coordinate_spread": coord_spread}


def coordinate_spread(occurrences: list[dict[str, Any]]) -> float | None:
    points = [(row["x"], row["y"]) for row in occurrences if row.get("x") is not None and row.get("y") is not None]
    if len(points) < 2:
        return None
    return max(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in combinations(points, 2))


def curve_statistics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        """SELECT UPPER(mnemonic) mnemonic,
          COUNT(DISTINCT well_id) well_count,COUNT(DISTINCT las_file_id) file_count,
          GROUP_CONCAT(DISTINCT unit) units,SUM(sample_count) sample_count,
          MIN(value_min) value_min,MAX(value_max) value_max
        FROM las_curves GROUP BY UPPER(mnemonic) ORDER BY well_count DESC,mnemonic"""
    )]


def interpretation_statistics(conn: sqlite3.Connection, data_type: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE s.data_type=?" if data_type else ""
    parameters = (data_type,) if data_type else ()
    return [dict(row) for row in conn.execute(
        f"""SELECT iv.attribute_name,s.data_type,
          COUNT(DISTINCT iv.well_id) well_count,COUNT(*) value_count,
          COUNT(DISTINCT iv.attribute_value) distinct_value_count,
          COUNT(DISTINCT COALESCE(iv.version,s.version,s.batch,'未标注')) version_count,
          GROUP_CONCAT(DISTINCT COALESCE(iv.version,s.version,s.batch,'未标注')) versions,
          MIN(iv.numeric_value) numeric_min,MAX(iv.numeric_value) numeric_max
        FROM interpretation_values iv JOIN sources s ON s.id=iv.source_id
        {where}
        GROUP BY iv.attribute_name,s.data_type ORDER BY well_count DESC,iv.attribute_name""",
        parameters,
    )]


def seismic_statistics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        """SELECT ss.*,s.filename,s.batch,s.version,s.imported_at,s.warning
           FROM seismic_surveys ss JOIN sources s ON s.id=ss.source_id ORDER BY s.imported_at DESC"""
    )]


def polygons(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(row) for row in conn.execute(
        """SELECT p.*,s.filename FROM polygons p JOIN sources s ON s.id=p.source_id ORDER BY p.name"""
    )]
    for row in rows:
        row["geometry"] = json.loads(row.pop("geometry_json"))
    return rows


def sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM sources ORDER BY imported_at DESC,id DESC"
    )]


def identity_suggestions(conn: sqlite3.Connection, threshold: float = 0.72, limit: int = 100) -> list[dict[str, Any]]:
    items = [dict(row) for row in conn.execute(
        "SELECT id,canonical_name,normalized_key,uwi,x,y,confidence_score FROM wells ORDER BY id"
    )]
    # Candidate buckets avoid an O(n²) full comparison for large well catalogs.
    buckets: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        digits = "-".join(re.findall(r"\d+", item["normalized_key"]))
        bucket = f"N:{digits}" if digits else f"P:{item['normalized_key'][:4]}"
        buckets.setdefault(bucket, []).append(index)
    candidate_pairs: set[tuple[int, int]] = set()
    for indexes in buckets.values():
        if len(indexes) <= 200:
            candidate_pairs.update((min(a, b), max(a, b)) for a, b in combinations(indexes, 2))
        else:
            ordered = sorted(indexes, key=lambda i: items[i]["normalized_key"])
            for position, left_index in enumerate(ordered):
                for right_index in ordered[position + 1:position + 7]:
                    candidate_pairs.add((min(left_index, right_index), max(left_index, right_index)))
    ordered_all = sorted(range(len(items)), key=lambda i: items[i]["normalized_key"])
    for position, left_index in enumerate(ordered_all):
        for right_index in ordered_all[position + 1:position + 4]:
            candidate_pairs.add((min(left_index, right_index), max(left_index, right_index)))

    suggestions: list[dict[str, Any]] = []
    for left_index, right_index in candidate_pairs:
        left, right = items[left_index], items[right_index]
        score = identity_similarity(left["canonical_name"], right["canonical_name"])
        reasons: list[str] = []
        if left["uwi"] and right["uwi"] and left["uwi"] == right["uwi"]:
            score = 1.0
            reasons.append("UWI 相同")
        if left["x"] is not None and right["x"] is not None and left["y"] is not None and right["y"] is not None:
            distance = math.hypot(left["x"] - right["x"], left["y"] - right["y"])
            if distance <= 5:
                score = min(1.0, score + 0.08)
                reasons.append(f"井位相距 {distance:.1f}")
        if score >= threshold:
            suggestions.append({"left": left, "right": right, "score": round(score, 3), "reasons": reasons or ["井名相似"]})
    return sorted(suggestions, key=lambda row: row["score"], reverse=True)[:limit]


def trajectory(conn: sqlite3.Connection, well_id: int) -> list[dict[str, Any]]:
    # Return the newest deviation survey when multiple batches exist.
    source = conn.execute(
        """SELECT d.source_id FROM deviation_stations d JOIN sources s ON s.id=d.source_id
           WHERE d.well_id=? GROUP BY d.source_id ORDER BY s.imported_at DESC,d.source_id DESC LIMIT 1""", (well_id,)
    ).fetchone()
    if not source:
        return []
    return [dict(row) for row in conn.execute(
        """SELECT md,inclination,azimuth,tvd,northing,easting,tvdss,z_msl
           FROM deviation_stations WHERE well_id=? AND source_id=? ORDER BY md""", (well_id, source["source_id"])
    )]
