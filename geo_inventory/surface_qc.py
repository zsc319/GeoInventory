"""Read-only quality checks for the regular surfaces in a project."""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

from .reserves import Grid, resolve_surface


MAX_COMPARE_SURFACES = 8
MAX_COMPARE_POINTS = 50_000
MAX_WELL_ROWS = 500


def _sample(grid: Grid, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample nearest nodes; never extrapolate beyond the grid's XY extent."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    values = np.full(x.shape, np.nan)
    distances = np.full(x.shape, np.nan)
    inside = (
        np.isfinite(x) & np.isfinite(y)
        & (x >= grid.x_min) & (x <= grid.x_max)
        & (y >= grid.y_min) & (y <= grid.y_max)
    )
    if not np.any(inside):
        return values, distances, inside
    valid_positions = np.flatnonzero(inside)
    col = np.rint((x[inside] - grid.x_min) / max(grid.x_max - grid.x_min, 1e-30) * (grid.columns - 1)).astype(int)
    row_fraction = (y[inside] - grid.y_min) / max(grid.y_max - grid.y_min, 1e-30)
    if not grid.y_ascending:
        row_fraction = 1 - row_fraction
    row = np.rint(row_fraction * (grid.rows - 1)).astype(int)
    col = np.clip(col, 0, grid.columns - 1)
    row = np.clip(row, 0, grid.rows - 1)
    values[valid_positions] = grid.values[row, col]
    node_x = grid.x_min + col * (grid.x_max - grid.x_min) / max(1, grid.columns - 1)
    node_y = grid.y_min + (row if grid.y_ascending else grid.rows - 1 - row) * (grid.y_max - grid.y_min) / max(1, grid.rows - 1)
    distances[valid_positions] = np.hypot(x[inside] - node_x, y[inside] - node_y)
    return values, distances, inside


def _missing_map(values: np.ndarray, y_ascending: bool, bins: int = 16) -> list[list[float]]:
    rows = np.array_split(np.arange(values.shape[0]), min(bins, values.shape[0]))
    columns = np.array_split(np.arange(values.shape[1]), min(bins, values.shape[1]))
    # The first row shown in the UI is north, regardless of file orientation.
    return [
        [round(float(np.mean(~np.isfinite(values[np.ix_(row, col)]))), 4) for col in columns]
        for row in (rows[::-1] if y_ascending else rows)
    ]


def surface_quality(grid: Grid) -> dict[str, Any]:
    valid = grid.values[np.isfinite(grid.values)]
    total = int(grid.values.size)
    if not valid.size:
        raise ValueError("所选属性面没有有效数值，无法进行体检")
    q = np.percentile(valid, [5, 25, 50, 75, 95])
    iqr = q[3] - q[1]
    outliers = int(np.count_nonzero((valid < q[1] - 3 * iqr) | (valid > q[3] + 3 * iqr)))
    return {
        "name": grid.name, "format": grid.format, "rows": grid.rows, "columns": grid.columns,
        "bounds": {"x_min": grid.x_min, "x_max": grid.x_max, "y_min": grid.y_min, "y_max": grid.y_max},
        "total_cells": total, "valid_cells": int(valid.size), "missing_cells": total - int(valid.size),
        "missing_percent": round((total - valid.size) * 100 / total, 2),
        "minimum": float(valid.min()), "maximum": float(valid.max()),
        "mean": float(valid.mean()), "standard_deviation": float(valid.std()),
        "p05": float(q[0]), "p25": float(q[1]), "median": float(q[2]),
        "p75": float(q[3]), "p95": float(q[4]), "iqr_outliers": outliers,
        "missing_map": _missing_map(grid.values, grid.y_ascending),
        "method": "空值按平面格式的空值标记识别；异常值为 Q1−3×IQR 或 Q3+3×IQR 之外的节点；空值图按网格分块统计。",
    }


def well_surface_samples(grid: Grid, wells: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(wells, key=lambda row: str(row.get("canonical_name") or row.get("project_key") or ""))
    coordinates = []
    for row in rows:
        try:
            x, y = float(row.get("x")), float(row.get("y"))
        except (TypeError, ValueError):
            x = y = math.nan
        coordinates.append((x, y))
    points = np.asarray(coordinates, dtype=float).reshape(-1, 2)
    values, distances, inside = _sample(grid, points[:, 0], points[:, 1])
    result_rows = []
    counts = {"ok": 0, "no_coordinates": 0, "outside": 0, "missing": 0}
    for index, row in enumerate(rows):
        if not np.isfinite(points[index]).all():
            status = "no_coordinates"
        elif not inside[index]:
            status = "outside"
        elif not np.isfinite(values[index]):
            status = "missing"
        else:
            status = "ok"
        counts[status] += 1
        if len(result_rows) < MAX_WELL_ROWS:
            result_rows.append({
                "well_key": row.get("project_key"),
                "well_name": row.get("canonical_name") or row.get("project_key") or "未知井",
                "x": float(points[index, 0]) if np.isfinite(points[index, 0]) else None,
                "y": float(points[index, 1]) if np.isfinite(points[index, 1]) else None,
                "value": float(values[index]) if status == "ok" else None,
                "distance": float(distances[index]) if inside[index] else None,
                "status": status,
            })
    return {
        "total_wells": len(rows), "counts": counts, "rows": result_rows,
        "display_limit": MAX_WELL_ROWS, "truncated": len(rows) > MAX_WELL_ROWS,
        "method": "使用井的 XY 坐标取属性面最近网格节点，不做坐标系转换；距离单位与平面 XY 坐标一致。",
    }


def compare_surfaces(conn: Any, snapshot: dict[str, Any], surface_ids: list[str]) -> dict[str, Any]:
    if not isinstance(surface_ids, list) or not 2 <= len(surface_ids) <= MAX_COMPARE_SURFACES:
        raise ValueError(f"请选择 2 至 {MAX_COMPARE_SURFACES} 个属性面")
    if any(not isinstance(item, str) for item in surface_ids) or len(set(surface_ids)) != len(surface_ids):
        raise ValueError("属性面选择有重复或格式不正确")
    reference = resolve_surface(conn, snapshot, surface_ids[0])
    count = min(MAX_COMPARE_POINTS, reference.values.size)
    rng = np.random.default_rng(42)
    indexes = np.sort(rng.choice(reference.values.size, count, replace=False))
    row, col = np.unravel_index(indexes, reference.values.shape)
    x, y = reference.x[col], reference.y[row]
    columns = [reference.values[row, col]]
    names = [reference.name]
    for surface_id in surface_ids[1:]:
        grid = resolve_surface(conn, snapshot, surface_id)
        sampled, _, _ = _sample(grid, x, y)
        columns.append(sampled)
        names.append(grid.name)
    matrix = np.column_stack(columns)
    complete = matrix[np.isfinite(matrix).all(axis=1)]
    if len(complete) < 10:
        raise ValueError("共同有效网格点少于 10 个；请检查平面范围、坐标系和空值")
    means = complete.mean(axis=0)
    standard_deviations = complete.std(axis=0)
    nonconstant = standard_deviations > 1e-12
    standardized = np.zeros_like(complete)
    standardized[:, nonconstant] = (complete[:, nonconstant] - means[nonconstant]) / standard_deviations[nonconstant]
    correlations = np.corrcoef(standardized[:, nonconstant], rowvar=False) if np.count_nonzero(nonconstant) >= 2 else None
    correlation_matrix: list[list[float | None]] = [[None] * len(names) for _ in names]
    active = np.flatnonzero(nonconstant)
    if correlations is not None:
        for i, source in enumerate(active):
            for j, target in enumerate(active):
                correlation_matrix[int(source)][int(target)] = round(float(correlations[i, j]), 4)
    elif len(active) == 1:
        correlation_matrix[int(active[0])][int(active[0])] = 1.0
    strong_pairs = [
        {"first": names[i], "second": names[j], "correlation": correlation_matrix[i][j]}
        for i in range(len(names)) for j in range(i + 1, len(names))
        if correlation_matrix[i][j] is not None and abs(correlation_matrix[i][j]) >= 0.9
    ]
    pca: dict[str, Any] | None = None
    if len(active) >= 2:
        active_values = standardized[:, active]
        covariance = active_values.T @ active_values / len(active_values)
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        order = np.argsort(eigenvalues)[::-1]
        axes = eigenvectors[:, order[:2]]
        for component in range(axes.shape[1]):
            pivot = np.argmax(np.abs(axes[:, component]))
            if axes[pivot, component] < 0:
                axes[:, component] *= -1
        scores = active_values @ axes
        plot_indices = np.linspace(0, len(scores) - 1, min(400, len(scores)), dtype=int)
        total_variance = float(np.maximum(eigenvalues, 0).sum())
        pca = {
            "explained_variance_percent": [round(float(max(eigenvalues[index], 0) / total_variance * 100), 2) for index in order[:2]],
            "points": [[round(float(a), 4), round(float(b), 4)] for a, b in scores[plot_indices]],
            "features": [names[int(index)] for index in active],
        }
    return {
        "surfaces": [{"id": item, "name": name, "constant": not bool(nonconstant[index])} for index, (item, name) in enumerate(zip(surface_ids, names))],
        "reference_cells": int(reference.values.size), "sampled_cells": count,
        "common_valid_cells": len(complete), "common_valid_percent": round(len(complete) * 100 / count, 2),
        "correlation": correlation_matrix, "strong_pairs": strong_pairs, "pca": pca,
        "method": "以首个平面为参考，固定随机种子抽取至多 50,000 个节点；其他平面按 XY 最近节点取值，仅使用共同有效点。相关性为 Pearson 系数，PCA 前逐属性标准化；常数属性不参与计算。",
    }
