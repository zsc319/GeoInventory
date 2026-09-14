from __future__ import annotations

import json
import math
import sqlite3
import struct
from collections import defaultdict
from pathlib import Path
from typing import Any

from .curve_analysis import curve_statistics, is_time_depth_mnemonic
from .db import utcnow
from .geometry import point_in_polygon
from .project_catalog import parse_dev_stations


SETTING_KEY = "global_well_filter"


def load_filter(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value_json FROM workspace_settings WHERE setting_key=?", (SETTING_KEY,)).fetchone()
    if not row:
        return {"active": False, "global_logic": "AND", "class_logic": {"curve": "AND", "polygon": "AND", "horizon": "AND"}, "conditions": []}
    try:
        return json.loads(row["value_json"])
    except ValueError:
        return {"active": False, "global_logic": "AND", "class_logic": {}, "conditions": []}


def save_filter(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    conditions = []
    for row in payload.get("conditions", []):
        kind = row.get("type")
        operator = row.get("operator")
        value = str(row.get("value") or "").strip()
        if kind in {"curve", "polygon", "horizon"} and operator in {"exists", "not_exists"} and value:
            conditions.append({"type": kind, "operator": operator, "value": value})
    result = {
        "active": bool(conditions) and payload.get("active", True),
        "global_logic": "OR" if payload.get("global_logic") == "OR" else "AND",
        "class_logic": {kind: "OR" if (payload.get("class_logic") or {}).get(kind) == "OR" else "AND" for kind in ("curve", "polygon", "horizon")},
        "conditions": conditions,
    }
    conn.execute(
        """INSERT INTO workspace_settings(setting_key,value_json,updated_at) VALUES(?,?,?)
           ON CONFLICT(setting_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
        (SETTING_KEY, json.dumps(result, ensure_ascii=False), utcnow()),
    )
    conn.commit()
    return result


def read_shapefile_polygons(path: str | Path) -> list[list[list[list[float]]]]:
    """Read polygon rings and Petrel-style PolyLineZ boundaries without GDAL.

    Petrel commonly exports a visually closed polygon as ESRI PolyLineZ (13)
    instead of PolygonZ (15).  The spatial filter closes those paths at query
    time, which matches the user's displayed polygon boundary.
    """
    geometries = []
    with Path(path).open("rb") as handle:
        header = handle.read(100)
        if len(header) < 100 or struct.unpack(">i", header[:4])[0] != 9994:
            raise ValueError("不是有效的 ESRI Shapefile")
        while True:
            record_header = handle.read(8)
            if not record_header:
                break
            if len(record_header) < 8:
                break
            content_length = struct.unpack(">i", record_header[4:])[0] * 2
            content = handle.read(content_length)
            if len(content) < 44:
                continue
            shape_type = struct.unpack("<i", content[:4])[0]
            if shape_type not in {3, 5, 13, 15, 23, 25}:
                continue
            num_parts, num_points = struct.unpack("<ii", content[36:44])
            parts_offset = 44
            points_offset = parts_offset + num_parts * 4
            if num_parts <= 0 or num_points <= 0 or len(content) < points_offset + num_points * 16:
                continue
            parts = list(struct.unpack(f"<{num_parts}i", content[parts_offset:points_offset])) + [num_points]
            points = [list(struct.unpack("<dd", content[points_offset + index * 16:points_offset + (index + 1) * 16])) for index in range(num_points)]
            rings = [points[parts[index]:parts[index + 1]] for index in range(num_parts) if parts[index + 1] - parts[index] >= 3]
            if shape_type in {3, 13, 23}:
                rings = [ring for ring in rings if math.isclose(ring[0][0], ring[-1][0], abs_tol=1e-7) and math.isclose(ring[0][1], ring[-1][1], abs_tol=1e-7)]
            if rings:
                geometries.append(rings)
    return geometries


def project_polygons(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    root = str(Path(snapshot["project"]["root"]).resolve())
    rows = [dict(row) for row in conn.execute(
        """SELECT id,file_path,filename,source_folder FROM project_catalog_items
           WHERE project_root=? AND category_key='polygons' AND extension='.shp' ORDER BY filename""", (root,)
    )]
    result = []
    for item in rows:
        try:
            geometries = read_shapefile_polygons(item["file_path"])
            if not geometries:
                continue
            display_geometry = [ring for geometry in geometries for ring in geometry]
            points = [point for ring in display_geometry for point in ring]
            result.append({
                "id": f"project:{item['id']}", "name": item["filename"],
                "crs": snapshot.get("wellheads", {}).get("crs"), "geometry": display_geometry,
                "geometries": geometries, "feature_count": len(geometries), "filename": item["filename"],
                "x_min": min(point[0] for point in points), "x_max": max(point[0] for point in points),
                "y_min": min(point[1] for point in points), "y_max": max(point[1] for point in points),
            })
        except (OSError, ValueError, struct.error):
            continue
    return result


def point_in_project_polygon(x: float, y: float, polygon: dict[str, Any]) -> bool:
    """Test a multi-feature SHP while preserving holes inside each feature."""
    return any(point_in_polygon(x, y, geometry) for geometry in polygon.get("geometries") or [polygon.get("geometry", [])])


def horizon_options(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    surface = snapshot.get("surface")
    if surface and surface.get("path"):
        result.append({"id": f"surface:{Path(surface['path']).name}", "name": Path(surface["path"]).stem, "kind": "regular_surface", "path": surface["path"], "ready": False})
    horizon = snapshot.get("horizon_2d")
    if horizon:
        for name in horizon.get("line_names", []):
            result.append({"id": f"2d:{name}", "name": name, "kind": "2d_profile", "path": horizon.get("path"), "ready": False, "note": "2D 层位仅在线上有定义，需先建立井轨迹与地震线的容差关系"})
    return result


def evaluate_filter(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    well_rows: list[dict[str, Any]],
    curve_profile: dict[str, Any],
    filter_payload: dict[str, Any] | None = None,
) -> tuple[set[str], dict[str, Any]]:
    rules = filter_payload or load_filter(conn)
    all_keys = {row["project_key"] for row in well_rows}
    if not rules.get("active") or not rules.get("conditions"):
        return all_keys, {"active": False, "matched": len(all_keys), "total": len(all_keys), "conditions": []}
    curve_sets = {row["mnemonic"]: set(row["wells"]) for row in curve_statistics(curve_profile) if not is_time_depth_mnemonic(row["mnemonic"])}
    polygons = {row["id"]: row for row in project_polygons(conn, snapshot)}
    horizon_hits = {(row["horizon_key"], row["well_key"]): bool(row["hit"]) for row in conn.execute("SELECT horizon_key,well_key,hit FROM horizon_well_hits WHERE project_root=?", (str(Path(snapshot["project"]["root"]).resolve()),))}
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for condition in rules["conditions"]:
        by_type[condition["type"]].append(condition)

    matched = set()
    pending_horizons = set()
    for well in well_rows:
        class_results = []
        for kind, conditions in by_type.items():
            condition_results = []
            for condition in conditions:
                exists = False
                if kind == "curve":
                    exists = well["project_key"] in curve_sets.get(condition["value"].upper(), set())
                elif kind == "polygon":
                    polygon = polygons.get(condition["value"])
                    exists = bool(polygon and well.get("x") is not None and point_in_project_polygon(well["x"], well["y"], polygon))
                elif kind == "horizon":
                    key = (condition["value"], well["project_key"])
                    if key not in horizon_hits:
                        pending_horizons.add(condition["value"])
                    exists = horizon_hits.get(key, False)
                condition_results.append(exists if condition["operator"] == "exists" else not exists)
            logic = (rules.get("class_logic") or {}).get(kind, "AND")
            class_results.append(any(condition_results) if logic == "OR" else all(condition_results))
        accepted = any(class_results) if rules.get("global_logic") == "OR" else all(class_results)
        if accepted:
            matched.add(well["project_key"])
    return matched, {"active": True, "matched": len(matched), "total": len(all_keys), "conditions": rules["conditions"], "pending_horizons": sorted(pending_horizons), "global_logic": rules.get("global_logic", "AND"), "class_logic": rules.get("class_logic", {})}


def compute_surface_horizon_hits(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    well_rows: list[dict[str, Any]],
    horizon_key: str,
    progress_callback=None,
    surface_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    surface = surface_override or snapshot.get("surface") or {}
    expected_key = f"surface:{Path(surface.get('path', '')).name}" if surface.get("path") else None
    if surface_override is None and horizon_key != expected_key:
        raise ValueError("当前只能计算规则 PTD Surface；2D 层位需要先定义地震线邻近容差")
    path = Path(surface["path"])
    bounds = surface.get("bounds") or {}
    rows_count, columns = int(surface.get("rows") or 0), int(surface.get("columns") or 0)
    xinc, yinc = float(surface.get("x_increment") or 0), float(surface.get("y_increment") or 0)
    if not path.is_file() or not rows_count or not columns or not xinc or not yinc:
        raise ValueError("层面网格元数据不完整")
    xmin, ymin, ymax = bounds["x_min"], bounds["y_min"], bounds["y_max"]
    trajectories: dict[str, list[dict[str, float]]] = {}
    target_indexes: set[int] = set()
    point_indexes: dict[tuple[str, int], tuple[int, int]] = {}
    for index, well in enumerate(well_rows, 1):
        stations = []
        if well.get("_dev_paths"):
            try:
                stations = parse_dev_stations(well["_dev_paths"][0])
            except OSError:
                stations = []
        if not stations and well.get("x") is not None and well.get("y") is not None:
            bottom = (well.get("total_depth") or 0) - (well.get("kb_elevation") or 0)
            stations = [{"md": 0.0, "x": well["x"], "y": well["y"], "z": well.get("kb_elevation") or 0.0, "tvd": 0.0}, {"md": well.get("total_depth") or 0.0, "x": well["x"], "y": well["y"], "z": -bottom, "tvd": well.get("total_depth") or 0.0}]
        trajectories[well["project_key"]] = stations
        for station_index, station in enumerate(stations):
            col = round((station["x"] - xmin) / xinc)
            row_from_min = round((station["y"] - ymin) / yinc)
            row_from_max = round((ymax - station["y"]) / yinc)
            if 0 <= col < columns and 0 <= row_from_min < rows_count:
                index_min, index_max = row_from_min * columns + col, row_from_max * columns + col
                point_indexes[(well["project_key"], station_index)] = (index_min, index_max)
                target_indexes.update((index_min, index_max))
        if progress_callback and index % 100 == 0:
            progress_callback(0.18 * index / max(1, len(well_rows)), f"读取 DEV 轨迹 {index}/{len(well_rows)}")

    targets = sorted(target_indexes)
    values: dict[int, float] = {}
    target_position = 0
    value_index = 0
    values_started = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle, 1):
            line = raw_line.strip()
            if not values_started:
                if line.startswith("->"):
                    values_started = True
                continue
            tokens = line.split()
            line_end = value_index + len(tokens)
            while target_position < len(targets) and targets[target_position] < line_end:
                target = targets[target_position]
                if target >= value_index:
                    try:
                        values[target] = float(tokens[target - value_index])
                    except (ValueError, IndexError):
                        pass
                target_position += 1
            value_index = line_end
            if progress_callback and line_no % 250000 == 0:
                progress_callback(0.18 + 0.65 * value_index / max(1, rows_count * columns), f"按轨迹位置抽取层面网格：{value_index:,} / {rows_count * columns:,} 单元")
            if target_position >= len(targets):
                break
    null_value = 1e29
    min_valid = sum(index_pair[0] in values and abs(values[index_pair[0]]) < null_value for index_pair in point_indexes.values())
    max_valid = sum(index_pair[1] in values and abs(values[index_pair[1]]) < null_value for index_pair in point_indexes.values())
    orientation = 0 if min_valid >= max_valid else 1
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    result_rows = []
    hit_count = 0
    for well_index, (well_key, stations) in enumerate(trajectories.items(), 1):
        sampled = []
        for station_index, station in enumerate(stations):
            pair = point_indexes.get((well_key, station_index))
            surface_z = values.get(pair[orientation]) if pair else None
            if surface_z is None or abs(surface_z) >= null_value:
                continue
            tvdss = -station["z"]
            sampled.append((station["md"], tvdss - surface_z, surface_z))
        hit, intersection_md, surface_z = False, None, None
        previous = None
        for current in sampled:
            if current[1] >= 0:
                hit, surface_z = True, current[2]
                if previous and current[1] != previous[1]:
                    fraction = -previous[1] / (current[1] - previous[1])
                    intersection_md = previous[0] + (current[0] - previous[0]) * fraction
                else:
                    intersection_md = current[0]
                break
            previous = current
        if hit:
            hit_count += 1
        result_rows.append((project_root, horizon_key, well_key, int(hit), intersection_md, surface_z, "DEV XYZ/TVDSS 与 PTD 最近网格点交会" if len(stations) > 2 else "井口垂向近似", utcnow()))
        if progress_callback and well_index % 200 == 0:
            progress_callback(0.85 + 0.14 * well_index / max(1, len(trajectories)), f"判断层面钻遇 {well_index}/{len(trajectories)}")
    conn.executemany(
        """INSERT INTO horizon_well_hits(project_root,horizon_key,well_key,hit,intersection_md,surface_z,method,computed_at)
           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_root,horizon_key,well_key) DO UPDATE SET
           hit=excluded.hit,intersection_md=excluded.intersection_md,surface_z=excluded.surface_z,method=excluded.method,computed_at=excluded.computed_at""",
        result_rows,
    )
    conn.commit()
    if progress_callback:
        progress_callback(1.0, "层面钻遇关系计算完成")
    return {"horizon_key": horizon_key, "wells": len(result_rows), "hit_wells": hit_count, "coverage_percentage": round(hit_count / max(1, len(result_rows)) * 100, 2), "grid_orientation": "y_min_to_y_max" if orientation == 0 else "y_max_to_y_min", "method": "DEV 轨迹 XYZ/TVDSS 与 PTD 规则网格最近单元逐站交会"}
