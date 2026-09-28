from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


SURFACE_SUFFIXES = {".zmap", ".zmap+", ".grd", ".grid", ".asc", ".dat", ".txt", ".ptd"}
MAX_GRID_CELLS = 2_000_000
MAX_SURFACE_BYTES = 128 * 1024 * 1024
PARAMETERS = {
    "netpay": {"label": "NetPay", "default": 10.0, "unit": "m"},
    "geofactor": {"label": "GeoFactor / NTG", "default": 1.0, "unit": "fraction"},
    "phie": {"label": "PHIE", "default": 0.15, "unit": "fraction"},
    "so": {"label": "So", "default": 0.70, "unit": "fraction"},
    "bo": {"label": "Bo", "default": 1.20, "unit": "rm3/sm3"},
    "rs": {"label": "Rs", "default": 0.0, "unit": "m3/m3"},
    "rec": {"label": "REC", "default": 0.20, "unit": "fraction"},
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    try:
        number = float(str(value).replace("D", "E").replace("d", "e"))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _numbers(line: str) -> list[float]:
    return [number for token in re.split(r"[\s,]+", line.strip()) if (number := _number(token)) is not None]


@dataclass
class Grid:
    name: str
    path: str
    values: np.ndarray
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    y_ascending: bool
    null_value: float | None
    format: str
    approximated: bool = False

    @property
    def rows(self) -> int:
        return int(self.values.shape[0])

    @property
    def columns(self) -> int:
        return int(self.values.shape[1])

    @property
    def x(self) -> np.ndarray:
        return np.linspace(self.x_min, self.x_max, self.columns)

    @property
    def y(self) -> np.ndarray:
        start, stop = (self.y_min, self.y_max) if self.y_ascending else (self.y_max, self.y_min)
        return np.linspace(start, stop, self.rows)

    def public_metadata(self) -> dict[str, Any]:
        finite = self.values[np.isfinite(self.values)]
        return {
            "name": self.name,
            "format": self.format,
            "rows": self.rows,
            "columns": self.columns,
            "bounds": {"x_min": self.x_min, "x_max": self.x_max, "y_min": self.y_min, "y_max": self.y_max},
            "value_min": float(np.min(finite)) if finite.size else None,
            "value_max": float(np.max(finite)) if finite.size else None,
            "valid_cells": int(finite.size),
            "approximated": self.approximated,
        }


def _apply_null(values: np.ndarray, null_value: float | None) -> np.ndarray:
    result = values.astype(float, copy=True)
    if null_value is not None:
        tolerance = max(1e-9, abs(null_value) * 1e-7)
        result[np.isclose(result, null_value, rtol=1e-7, atol=tolerance)] = np.nan
    result[~np.isfinite(result)] = np.nan
    return result


def _read_fsasci(path: Path, lines: list[str]) -> Grid:
    rows = columns = None
    bounds = None
    null_value = 1e30
    start = None
    inline_values: list[float] = []
    for index, raw in enumerate(lines):
        line = raw.strip()
        upper = line.upper()
        if upper.startswith("FSASCI"):
            values = _numbers(line)
            if values:
                null_value = values[-1]
        elif upper.startswith("FSLIMI"):
            values = _numbers(line)
            if len(values) >= 6:
                bounds = values[:6]
        elif upper.startswith("FSNROW"):
            values = _numbers(line)
            if len(values) >= 2:
                rows, columns = int(values[0]), int(values[1])
        elif line.startswith("->"):
            start = index + 1
            inline_values.extend(_numbers(line[2:]))
            break
    if not rows or not columns or not bounds or start is None:
        raise ValueError(f"{path.name} 缺少 FSASCI 网格头信息")
    if rows * columns > MAX_GRID_CELLS:
        raise ValueError(f"{path.name} 包含 {rows * columns:,} 个节点，超过单次安全计算上限 {MAX_GRID_CELLS:,}；请先重采样为较疏网格")
    data = inline_values
    for line in lines[start:]:
        data.extend(_numbers(line))
    required = rows * columns
    if len(data) < required:
        raise ValueError(f"{path.name} 网格值不足：需要 {required:,}，实际 {len(data):,}")
    array = _apply_null(np.asarray(data[:required], dtype=float).reshape(rows, columns), null_value)
    return Grid(path.stem, str(path), array, bounds[0], bounds[1], bounds[2], bounds[3], False, null_value, "Petrel FSASCI")


def _zmap_header(lines: list[str]) -> tuple[int, int, float, float, float, float, float | None, int]:
    markers = [index for index, line in enumerate(lines) if line.lstrip().startswith("@")]
    if len(markers) < 2:
        raise ValueError("缺少 ZMAP 数据起始标记")
    header_rows = [_numbers(line) for line in lines[markers[0] + 1:markers[1]]]
    header_rows = [row for row in header_rows if row]
    grid_row = next((row for row in header_rows if len(row) >= 6 and row[0] >= 1 and row[1] >= 1 and row[3] > row[2] and row[5] > row[4]), None)
    if grid_row is None:
        raise ValueError("无法识别 ZMAP 行列数与 XY 范围")
    before = header_rows[header_rows.index(grid_row) - 1] if header_rows.index(grid_row) else []
    null_value = before[1] if len(before) >= 2 else None
    return int(grid_row[0]), int(grid_row[1]), grid_row[2], grid_row[3], grid_row[4], grid_row[5], null_value, markers[1] + 1


def _read_zmap(path: Path, lines: list[str]) -> Grid:
    rows, columns, x_min, x_max, y_min, y_max, null_value, start = _zmap_header(lines)
    if rows * columns > MAX_GRID_CELLS:
        raise ValueError(f"{path.name} 包含 {rows * columns:,} 个节点，超过单次安全计算上限 {MAX_GRID_CELLS:,}；请先重采样为较疏网格")
    data: list[float] = []
    for line in lines[start:]:
        if line.lstrip().startswith(("!", "#")):
            continue
        data.extend(_numbers(line))
    required = rows * columns
    if len(data) < required:
        raise ValueError(f"{path.name} 网格值不足：需要 {required:,}，实际 {len(data):,}")
    # ZMAP nodes are serialized column by column; each column advances along Y.
    array = np.asarray(data[:required], dtype=float).reshape(columns, rows).T
    array = _apply_null(array, null_value)
    return Grid(path.stem, str(path), array, x_min, x_max, y_min, y_max, True, null_value, "ZMAP+ GRID")


def _read_xyz(path: Path, lines: list[str]) -> Grid:
    points = []
    for line in lines:
        if line.lstrip().startswith(("!", "#", "@", "->", "FS")):
            continue
        values = _numbers(line)
        if len(values) >= 3:
            points.append(values[:3])
    if len(points) < 4:
        raise ValueError(f"{path.name} 不是可识别的规则 ZMAP/XYZ 平面")
    data = np.asarray(points, dtype=float)
    xs, ys = np.unique(data[:, 0]), np.unique(data[:, 1])
    if len(xs) * len(ys) != len(data):
        raise ValueError("当前仅支持规则 XYZ 网格；散点属性面请先在 Petrel 中导出为 ZMAP GRID")
    x_index = {value: index for index, value in enumerate(xs)}
    y_index = {value: index for index, value in enumerate(ys)}
    array = np.full((len(ys), len(xs)), np.nan)
    for x, y, value in data:
        array[y_index[y], x_index[x]] = value
    return Grid(path.stem, str(path), array, float(xs[0]), float(xs[-1]), float(ys[0]), float(ys[-1]), True, None, "XYZ regular grid")


def read_grid(path: str | Path) -> Grid:
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"属性面文件不存在：{source}")
    if source.stat().st_size > MAX_SURFACE_BYTES:
        raise ValueError(f"{source.name} 超过 128 MB 的单面安全读取上限；请先在 Petrel 中重采样或分区导出")
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    upper = "\n".join(lines[:80]).upper()
    if "FSASCI" in upper and "FSNROW" in upper:
        return _read_fsasci(source, lines)
    if any(line.lstrip().upper().startswith("@") for line in lines[:80]):
        return _read_zmap(source, lines)
    return _read_xyz(source, lines)


def probe_grid(path: str | Path) -> dict[str, Any] | None:
    source = Path(path)
    if not source.is_file() or source.suffix.lower() not in SURFACE_SUFFIXES:
        return None
    head: list[str] = []
    try:
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(80), handle):
                head.append(line)
    except OSError:
        return None
    try:
        text = "".join(head).upper()
        if "FSASCI" in text:
            rows = columns = None
            bounds = None
            for line in head:
                if line.upper().startswith("FSNROW"):
                    values = _numbers(line)
                    rows, columns = int(values[0]), int(values[1])
                elif line.upper().startswith("FSLIMI"):
                    bounds = _numbers(line)[:4]
            return {"format": "Petrel FSASCI", "rows": rows, "columns": columns, "bounds": bounds}
        if "@" in text and "GRID" in text:
            rows, columns, x_min, x_max, y_min, y_max, _, _ = _zmap_header(head)
            return {"format": "ZMAP+ GRID", "rows": rows, "columns": columns, "bounds": [x_min, x_max, y_min, y_max]}
    except (ValueError, IndexError):
        return None
    return None


def surface_inventory(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    root = str(Path(snapshot["project"]["root"]).resolve())
    rows: list[dict[str, Any]] = []
    candidates = conn.execute(
        """SELECT id,filename,file_path,relative_path,category_key,extension FROM project_catalog_items
           WHERE project_root=? AND extension IN ('.zmap','.zmap+','.grd','.grid','.asc','.dat','.txt','.ptd')
           ORDER BY filename COLLATE NOCASE""",
        (root,),
    ).fetchall()
    for row in candidates:
        metadata = probe_grid(row["file_path"])
        if metadata:
            rows.append({"id": f"catalog:{row['id']}", "name": row["filename"], "path": row["file_path"], "relative_path": row["relative_path"], "source": "工区资料树", **metadata})
    for row in conn.execute("SELECT * FROM reserve_surfaces ORDER BY created_at DESC"):
        metadata = json.loads(row["metadata_json"] or "{}")
        rows.append({"id": f"reserve:{row['surface_id']}", "name": row["surface_name"], "path": row["file_path"], "relative_path": row["surface_name"], "source": "储量计算导入", **metadata})
    return rows


def resolve_surface(conn: sqlite3.Connection, snapshot: dict[str, Any], surface_id: str) -> Grid:
    kind, _, raw_id = str(surface_id or "").partition(":")
    if not raw_id.isdigit():
        raise ValueError("请选择有效的属性平面")
    if kind == "catalog":
        root = str(Path(snapshot["project"]["root"]).resolve())
        row = conn.execute("SELECT filename,file_path FROM project_catalog_items WHERE id=? AND project_root=?", (int(raw_id), root)).fetchone()
    elif kind == "reserve":
        row = conn.execute("SELECT surface_name filename,file_path FROM reserve_surfaces WHERE surface_id=?", (int(raw_id),)).fetchone()
    else:
        row = None
    if not row:
        raise ValueError("属性平面不存在或不属于当前工区")
    return read_grid(row["file_path"])


def import_surface(conn: sqlite3.Connection, path: str | Path, original_name: str) -> dict[str, Any]:
    grid = read_grid(path)
    metadata = grid.public_metadata()
    now = utcnow()
    cursor = conn.execute(
        """INSERT INTO reserve_surfaces(surface_name,file_path,metadata_json,created_at,updated_at)
           VALUES(?,?,?,?,?) ON CONFLICT(file_path) DO UPDATE SET surface_name=excluded.surface_name,
           metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
        (original_name, str(Path(path).resolve()), json.dumps(metadata, ensure_ascii=False), now, now),
    )
    surface_id = cursor.lastrowid or conn.execute("SELECT surface_id FROM reserve_surfaces WHERE file_path=?", (str(Path(path).resolve()),)).fetchone()[0]
    return {"id": f"reserve:{surface_id}", "name": original_name, "source": "储量计算导入", **metadata}


def list_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    for row in conn.execute("SELECT * FROM reserve_parameter_groups ORDER BY updated_at DESC"):
        result.append({
            "id": row["group_id"], "name": row["group_name"],
            "configuration": json.loads(row["configuration_json"] or "{}"),
            "result_summary": json.loads(row["result_summary_json"] or "{}"),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })
    return result


def save_group(conn: sqlite3.Connection, payload: dict[str, Any], group_id: str | None = None) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("请填写储量参数组名称")
    group_id = group_id or f"reserve-{uuid.uuid4().hex[:12]}"
    configuration = payload.get("configuration") or {}
    summary = payload.get("result_summary") or {}
    now = utcnow()
    existing = conn.execute("SELECT created_at FROM reserve_parameter_groups WHERE group_id=?", (group_id,)).fetchone()
    created = existing["created_at"] if existing else now
    conn.execute(
        """INSERT INTO reserve_parameter_groups(group_id,group_name,configuration_json,result_summary_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET group_name=excluded.group_name,
           configuration_json=excluded.configuration_json,result_summary_json=excluded.result_summary_json,updated_at=excluded.updated_at""",
        (group_id, name, json.dumps(configuration, ensure_ascii=False), json.dumps(summary, ensure_ascii=False), created, now),
    )
    return {"id": group_id, "name": name, "configuration": configuration, "result_summary": summary, "created_at": created, "updated_at": now}


def delete_group(conn: sqlite3.Connection, group_id: str) -> None:
    conn.execute("DELETE FROM reserve_parameter_groups WHERE group_id=?", (group_id,))


def _resample(source: Grid, target: Grid) -> np.ndarray:
    if source.values.shape == target.values.shape and np.allclose([source.x_min, source.x_max, source.y_min, source.y_max], [target.x_min, target.x_max, target.y_min, target.y_max]) and source.y_ascending == target.y_ascending:
        return source.values.copy()
    tx, ty = target.x, target.y
    col = np.rint((tx - source.x_min) / max(1e-30, source.x_max - source.x_min) * (source.columns - 1)).astype(int)
    row_fraction = (ty - source.y_min) / max(1e-30, source.y_max - source.y_min)
    if not source.y_ascending:
        row_fraction = 1 - row_fraction
    row = np.rint(row_fraction * (source.rows - 1)).astype(int)
    outside_x = (tx < source.x_min) | (tx > source.x_max)
    outside_y = (ty < source.y_min) | (ty > source.y_max)
    sampled = source.values[np.ix_(np.clip(row, 0, source.rows - 1), np.clip(col, 0, source.columns - 1))].copy()
    sampled[:, outside_x] = np.nan
    sampled[outside_y, :] = np.nan
    return sampled


def _polygon_mask(grid: Grid, polygon: list[list[float]] | None) -> np.ndarray:
    if not polygon or len(polygon) < 3:
        return np.ones(grid.values.shape, dtype=bool)
    points = [(float(point[0]), float(point[1])) for point in polygon if len(point) >= 2]
    if len(points) < 3:
        return np.ones(grid.values.shape, dtype=bool)
    xs, ys = grid.x, grid.y
    mask = np.zeros(grid.values.shape, dtype=bool)
    for start in range(0, grid.rows, 256):
        stop = min(grid.rows, start + 256)
        x_mesh, y_mesh = np.meshgrid(xs, ys[start:stop])
        inside = np.zeros(x_mesh.shape, dtype=bool)
        previous = points[-1]
        for current in points:
            x1, y1 = previous
            x2, y2 = current
            crosses = ((y1 > y_mesh) != (y2 > y_mesh)) & (x_mesh < (x2 - x1) * (y_mesh - y1) / ((y2 - y1) or 1e-30) + x1)
            inside ^= crosses
            previous = current
        mask[start:stop] = inside
    return mask


def _layer_preview(array: np.ndarray, grid: Grid, label: str, unit: str, max_size: int = 180) -> dict[str, Any]:
    row_step = max(1, math.ceil(array.shape[0] / max_size))
    col_step = max(1, math.ceil(array.shape[1] / max_size))
    preview = array[::row_step, ::col_step]
    finite = preview[np.isfinite(preview)]
    values = [None if not math.isfinite(float(value)) else round(float(value), 7) for value in preview.ravel()]
    return {
        "label": label, "unit": unit, "rows": int(preview.shape[0]), "columns": int(preview.shape[1]),
        "values": values, "value_min": float(np.min(finite)) if finite.size else None,
        "value_max": float(np.max(finite)) if finite.size else None,
        "bounds": {"x_min": grid.x_min, "x_max": grid.x_max, "y_min": grid.y_min, "y_max": grid.y_max},
        "y_ascending": grid.y_ascending,
    }


def calculate(conn: sqlite3.Connection, snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    configuration = payload.get("configuration") or payload
    structure_id = str(configuration.get("structure_surface_id") or "")
    parameter_config = configuration.get("parameters") or {}
    surface_ids = [str(item.get("surface_id") or "") for item in parameter_config.values() if isinstance(item, dict) and item.get("mode") == "surface"]
    reference_id = structure_id or next((value for value in surface_ids if value), "")
    if not reference_id:
        raise ValueError("至少需要选择一个构造面或属性平面，用于确定 XY 网格和单元面积")
    reference = resolve_surface(conn, snapshot, reference_id)
    if reference.rows < 2 or reference.columns < 2:
        raise ValueError("参考平面至少需要 2×2 个网格节点")
    arrays: dict[str, np.ndarray] = {}
    sources: dict[str, str] = {}
    for key, definition in PARAMETERS.items():
        item = parameter_config.get(key) if isinstance(parameter_config.get(key), dict) else {}
        mode = item.get("mode") or "constant"
        if mode == "surface":
            surface = resolve_surface(conn, snapshot, str(item.get("surface_id") or ""))
            values = _resample(surface, reference)
            sources[key] = surface.name
        else:
            value = _number(item.get("value"))
            value = definition["default"] if value is None else value
            values = np.full(reference.values.shape, value, dtype=float)
            sources[key] = f"常数 {value:g}"
        if key in {"geofactor", "phie", "so", "rec"}:
            finite = values[np.isfinite(values)]
            if item.get("unit_mode") == "percent":
                if finite.size and float(np.max(finite)) > 100.000001:
                    raise ValueError(f"{definition['label']} 的百分数超过 100%，请核对属性面或单位")
                values = values / 100.0
            elif finite.size and float(np.max(finite)) > 1.000001:
                raise ValueError(f"{definition['label']} 当前按 0–1 小数读取，但检测到大于 1 的值；若源面为百分数，请切换为“百分数 %”")
        arrays[key] = values
    mask = np.isfinite(reference.values)
    for key, values in arrays.items():
        mask &= np.isfinite(values)
        mask &= values >= 0
        if key == "bo":
            mask &= values > 0
    polygon = payload.get("polygon") or configuration.get("polygon")
    mask &= _polygon_mask(reference, polygon)
    if not np.any(mask):
        raise ValueError("当前范围内没有同时具备全部参数的有效网格")
    dx = abs((reference.x_max - reference.x_min) / (reference.columns - 1))
    dy = abs((reference.y_max - reference.y_min) / (reference.rows - 1))
    node_area = dx * dy
    weights = np.ones(reference.values.shape, dtype=float)
    weights[[0, -1], :] *= 0.5
    weights[:, [0, -1]] *= 0.5
    effective_area = node_area * weights
    bulk = effective_area * arrays["netpay"] * arrays["geofactor"]
    hcpv = bulk * arrays["phie"] * arrays["so"]
    stoiip = np.divide(hcpv, arrays["bo"], out=np.full_like(hcpv, np.nan), where=arrays["bo"] > 0)
    recoverable = stoiip * arrays["rec"]
    solution_gas = stoiip * arrays["rs"]
    for values in (bulk, hcpv, stoiip, recoverable, solution_gas):
        values[~mask] = np.nan
    area_m2 = float(np.sum(effective_area[mask]))
    stoiip_total = float(np.nansum(stoiip))
    recoverable_total = float(np.nansum(recoverable))
    gas_total = float(np.nansum(solution_gas))
    density_factor = 1e6 / 1e4 / node_area
    layers = {
        "stoiip_density": _layer_preview(stoiip * density_factor, reference, "原始地质储量丰度", "10⁴ m³/km²"),
        "recoverable_density": _layer_preview(recoverable * density_factor, reference, "可采储量丰度", "10⁴ m³/km²"),
        "netpay": _layer_preview(np.where(mask, arrays["netpay"], np.nan), reference, "NetPay", "m"),
        "geofactor": _layer_preview(np.where(mask, arrays["geofactor"], np.nan), reference, "GeoFactor / NTG", "fraction"),
        "phie": _layer_preview(np.where(mask, arrays["phie"], np.nan), reference, "PHIE", "fraction"),
        "so": _layer_preview(np.where(mask, arrays["so"], np.nan), reference, "So", "fraction"),
        "bo": _layer_preview(np.where(mask, arrays["bo"], np.nan), reference, "Bo", "rm³/sm³"),
        "rs": _layer_preview(np.where(mask, arrays["rs"], np.nan), reference, "Rs", "m³/m³"),
        "rec": _layer_preview(np.where(mask, arrays["rec"], np.nan), reference, "REC", "fraction"),
    }
    return {
        "formula": "STOIIP = Σ(Acell × NetPay × GeoFactor × PHIE × So ÷ Bo)",
        "reference": {**reference.public_metadata(), "id": reference_id, "cell_dx": dx, "cell_dy": dy},
        "sources": sources,
        "scope": "圈定范围" if polygon else "全有效网格",
        "polygon": polygon or [],
        "summary": {
            "valid_cells": int(np.count_nonzero(mask)), "area_km2": area_m2 / 1e6,
            "bulk_volume_1e6_m3": float(np.nansum(bulk)) / 1e6,
            "hcpv_1e6_m3": float(np.nansum(hcpv)) / 1e6,
            "stoiip_1e4_m3": stoiip_total / 1e4,
            "stoiip_mmbbl": stoiip_total * 6.28981077 / 1e6,
            "recoverable_1e4_m3": recoverable_total / 1e4,
            "recoverable_mmbbl": recoverable_total * 6.28981077 / 1e6,
            "solution_gas_1e8_m3": gas_total / 1e8,
        },
        "layers": layers,
        "warnings": [
            "体积计算假定 XY 与厚度均采用米制；若源数据单位不同，请先完成单位换算。",
            "不同网格采用最近节点重采样；结果需结合坐标系、边界和原始 Petrel 设置复核。",
        ],
    }
