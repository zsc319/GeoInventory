from __future__ import annotations

import json
import math
import os
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .geometry import convex_hull, ring_area
from .importers import normalize_well_name, parse_las, scan_segy
from .time_depth import is_time_depth_file


SIDECAR_EXTENSIONS = {".xml", ".prj", ".shx", ".dbf", ".sbn", ".sbx"}
SEGY_EXTENSIONS = {".sgy", ".segy", ".seg-y"}
ZGY_EXTENSIONS = {".zgy"}
SEISMIC_EXTENSIONS = SEGY_EXTENSIONS | ZGY_EXTENSIONS


def build_project_snapshot(
    root: str | Path,
    seismic_3d_path: str | Path | None = None,
    seismic_2d_path: str | Path | None = None,
    surface_path: str | Path | None = None,
    horizon_2d_path: str | Path | None = None,
    fault_path: str | Path | None = None,
    scan_surface_values: bool = True,
) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{root}")
    files = _catalog_files(root)
    if not files:
        raise ValueError("项目目录中没有文件")
    representatives = choose_representatives(files)
    categories = _category_summary(root, files)
    top_folders = _top_folder_summary(root, files)

    wellhead_candidates = [item for item in files if "wellhead" in _relative_lower(root, item) and item.suffix.lower() not in SIDECAR_EXTENSIONS]
    wellhead_file = next((item for item in wellhead_candidates if item.suffix == ""), None) or (_median_file(wellhead_candidates) if wellhead_candidates else None)
    wellheads = parse_petrel_well_head(wellhead_file) if wellhead_file else {"count": 0, "wells": [], "points": [], "crs": None}

    checkshot_files = [path for path in files if is_time_depth_file(root, path)]
    las_files = [path for path in files if path.suffix.lower() == ".las" and path not in checkshot_files]
    dev_files = [path for path in files if path.suffix.lower() == ".dev"]
    coverage = _well_file_coverage(wellheads.get("wells", []), las_files, dev_files, checkshot_files)
    representative_analysis = analyze_representatives(root, representatives)

    ptd_files = [path for path in files if path.suffix.lower() == ".ptd"]
    surface_file = _resolve_optional_path(surface_path) or (_median_file(ptd_files) if ptd_files else None)
    surface = parse_petrel_surface(surface_file, scan_surface_values) if surface_file else None

    horizon_files = [path for path in files if "horizons" in _relative_lower(root, path) and path.suffix == "" and path.is_file()]
    horizon_2d_file = _resolve_optional_path(horizon_2d_path) or _prefer_named(horizon_files, "CUC 1995") or (_median_file(horizon_files) if horizon_files else None)
    horizon_2d = parse_petrel_profile(horizon_2d_file) if horizon_2d_file else None

    individual_fault_files = [
        path for path in files
        if path.suffix == "" and path.parent.name.lower() == "faults" and path.name.lower() != "faults"
    ]
    fault_file = _resolve_optional_path(fault_path) or (_median_file(individual_fault_files) if individual_fault_files else None)
    representative_fault = parse_fault_sticks(fault_file) if fault_file else None
    fault_model_file = _first_business_file(files, lambda item: "fault model" in _relative_lower(root, item) and item.suffix == "")
    fault_model = parse_fault_model(fault_model_file) if fault_model_file else None
    fault_2d_file = _first_business_file(files, lambda item: "faults\\2d" in _relative_lower(root, item) and item.suffix == "")
    fault_2d = parse_fault_2d(fault_2d_file) if fault_2d_file else None

    segy_files = [path for path in files if path.suffix.lower() in SEGY_EXTENSIONS]
    seismic_files = [path for path in files if path.suffix.lower() in SEISMIC_EXTENSIONS]
    seismic_3d_file = _resolve_optional_path(seismic_3d_path) or _choose_3d_seismic(segy_files)
    seismic_2d_file = _resolve_optional_path(seismic_2d_path) or _choose_matching_2d_seismic(segy_files, horizon_2d)
    seismic_3d = scan_segy(seismic_3d_file) if seismic_3d_file else None
    seismic_2d = scan_segy(seismic_2d_file) if seismic_2d_file else None
    if seismic_3d:
        seismic_3d["path"] = str(seismic_3d_file)
        seismic_3d["filename"] = seismic_3d_file.name
    if seismic_2d:
        seismic_2d["path"] = str(seismic_2d_file)
        seismic_2d["filename"] = seismic_2d_file.name

    matches = {
        "surface_to_3d": match_surface_to_seismic(surface, seismic_3d) if surface and seismic_3d else None,
        "horizon_2d_to_seismic": match_2d_points_to_seismic(horizon_2d, seismic_2d) if horizon_2d and seismic_2d else None,
        "fault_to_3d": match_fault_to_seismic(representative_fault, seismic_3d) if representative_fault and seismic_3d else None,
    }
    quality = build_quality_assessment(categories, coverage, surface, seismic_3d, matches, len(individual_fault_files), fault_model)
    visualization = build_visualization_payload(wellheads, surface, horizon_2d, representative_fault, seismic_3d, seismic_2d)
    return {
        "project": {
            "name": root.name,
            "root": str(root),
            "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scan_mode": "one_representative_per_file_directory",
            "total_files": len(files),
            "total_bytes": sum(path.stat().st_size for path in files),
            "total_gb": round(sum(path.stat().st_size for path in files) / 1024 ** 3, 3),
            "representative_count": len(representatives),
        },
        "categories": categories,
        "top_folders": top_folders,
        "representatives": [
            {"folder": str(parent.relative_to(root)), "path": str(path), "filename": path.name, "extension": path.suffix.lower() or "[none]", "bytes": path.stat().st_size}
            for parent, path in representatives.items()
        ],
        "representative_analysis": representative_analysis,
        "wellheads": wellheads,
        "well_coverage": coverage,
        "surface": surface,
        "horizon_2d": horizon_2d,
        "faults": {
            "individual_file_count": len(individual_fault_files),
            "representative": representative_fault,
            "model": fault_model,
            "fault_2d": fault_2d,
        },
        "seismic": {"three_d": seismic_3d, "two_d": seismic_2d, "file_count": len(seismic_files), "zgy_file_count": sum(path.suffix.lower() in ZGY_EXTENSIONS for path in seismic_files)},
        "matches": matches,
        "quality": quality,
        "visualization": visualization,
    }


def save_snapshot(snapshot: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_snapshot(path: str | Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for directory, _, filenames in os.walk(root):
        parent = Path(directory)
        for filename in filenames:
            path = parent / filename
            try:
                if path.is_file():
                    result.append(path)
            except OSError:
                continue
    return result


def choose_representatives(files: Iterable[Path]) -> dict[Path, Path]:
    grouped: dict[Path, list[Path]] = defaultdict(list)
    for path in files:
        grouped[path.parent].append(path)
    result: dict[Path, Path] = {}
    for parent, paths in grouped.items():
        business = [path for path in paths if path.suffix.lower() not in SIDECAR_EXTENSIONS]
        candidates = business or paths
        extension_groups: dict[str, list[Path]] = defaultdict(list)
        for path in candidates:
            extension_groups[path.suffix.lower() or "[none]"].append(path)
        chosen_extension = max(extension_groups, key=lambda key: (len(extension_groups[key]), _extension_priority(key)))
        result[parent] = _median_file(extension_groups[chosen_extension])
    return result


def analyze_representatives(root: Path, representatives: dict[Path, Path]) -> dict[str, Any]:
    las_results: list[dict[str, Any]] = []
    dev_results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    curve_samples: Counter[str] = Counter()
    for path in representatives.values():
        try:
            if path.suffix.lower() == ".las":
                parsed = parse_las(path)
                mnemonics = [curve["mnemonic"] for curve in parsed["curves"][1:]]
                curve_samples.update(set(mnemonics))
                las_results.append({
                    "folder": str(path.parent.relative_to(root)), "filename": path.name,
                    "well": parsed["well"].get("WELL", {}).get("value") or path.stem,
                    "curve_count": len(mnemonics), "curves": mnemonics,
                    "start": parsed["start"], "stop": parsed["stop"], "step": parsed["step"],
                    "depth_unit": parsed.get("depth_unit"), "sample_rows": parsed["sample_rows"],
                    "curve_details": [{key: curve.get(key) for key in (
                        "mnemonic", "unit", "description", "sample_count", "value_min", "value_max",
                        "value_mean", "value_std", "p05", "p50", "p95",
                    )} for curve in parsed["curves"][1:]],
                })
            elif path.suffix.lower() == ".dev":
                dev_results.append(parse_petrel_dev(path))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {
        "las_sample_count": len(las_results),
        "dev_sample_count": len(dev_results),
        "curve_sample_coverage": [{"mnemonic": name, "sample_files": count} for name, count in curve_samples.most_common()],
        "las_samples": las_results,
        "dev_samples": dev_results,
        "errors": errors,
    }


def parse_petrel_well_head(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    headers: list[str] = []
    wells: list[dict[str, Any]] = []
    in_header = False
    crs = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line.startswith("# Coordinate reference system X, Y:"):
                crs = line.split(":", 1)[1].strip()
            if line == "BEGIN HEADER":
                in_header = True
                continue
            if line == "END HEADER":
                in_header = False
                continue
            if in_header:
                headers.append(line.split(",", 1)[-1].strip())
                continue
            if not line or line.startswith("#") or line.startswith("VERSION"):
                continue
            values = _petrel_tokens(line)
            if len(values) < len(headers):
                continue
            row = dict(zip(headers, values))
            x, y = _float(row.get("Surface X")), _float(row.get("Surface Y"))
            wells.append({
                "name": row.get("Name"), "uwi": row.get("UWI"), "x": x, "y": y,
                "kb": _float(row.get("Well datum value")), "td_md": _float(row.get("TD (MD)")),
            })
    points = [[row["x"], row["y"], row["name"]] for row in wells if row["x"] is not None and row["y"] is not None]
    return {"path": str(path), "count": len(wells), "located_count": len(points), "crs": crs, "wells": wells, "points": _thin(points, 5000)}


def parse_petrel_dev(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    name = path.stem
    station_count = 0
    max_md = None
    x = y = kb = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line.startswith("# WELL NAME:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("# WELL HEAD X-COORDINATE:"):
                x = _first_number(line.split(":", 1)[1])
            elif line.startswith("# WELL HEAD Y-COORDINATE:"):
                y = _first_number(line.split(":", 1)[1])
            elif line.startswith("# WELL DATUM"):
                kb = _first_number(line.split(":", 1)[1])
            elif line and not line.startswith("#") and re.match(r"^[+-]?\d", line):
                md = _float(line.split()[0])
                if md is not None:
                    station_count += 1
                    max_md = md if max_md is None else max(max_md, md)
    return {"path": str(path), "filename": path.name, "well": name, "station_count": station_count, "max_md": max_md, "x": x, "y": y, "kb": kb}


def parse_petrel_surface(path: str | Path, scan_values: bool = True) -> dict[str, Any]:
    path = Path(path)
    bounds = None
    rows = columns = None
    x_increment = y_increment = None
    null_value = 1e30
    valid_count = null_count = 0
    value_min = value_max = None
    values_started = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line.startswith("FSASCI"):
                maybe_null = _float(line.split()[-1])
                if maybe_null is not None:
                    null_value = maybe_null
            elif line.startswith("FSLIMI"):
                numbers = [_float(value) for value in line.split()[1:]]
                if len(numbers) >= 6 and all(value is not None for value in numbers[:6]):
                    bounds = {"x_min": numbers[0], "x_max": numbers[1], "y_min": numbers[2], "y_max": numbers[3], "z_min": numbers[4], "z_max": numbers[5]}
            elif line.startswith("FSNROW"):
                values = line.split()
                rows, columns = int(values[1]), int(values[2])
            elif line.startswith("FSXINC"):
                values = line.split()
                x_increment, y_increment = float(values[1]), float(values[2])
            elif line.startswith("->"):
                values_started = True
                if not scan_values:
                    break
            elif values_started and scan_values:
                for token in line.split():
                    value = _float(token)
                    if value is None:
                        continue
                    if abs(value) >= abs(null_value) * 0.99:
                        null_count += 1
                    else:
                        valid_count += 1
                        value_min = value if value_min is None else min(value_min, value)
                        value_max = value if value_max is None else max(value_max, value)
    total_cells = (rows or 0) * (columns or 0)
    if not scan_values:
        valid_count = None
        null_count = None
    return {
        "path": str(path), "filename": path.name, "format": "Petrel FSASCI surface",
        "rows": rows, "columns": columns, "total_cells": total_cells,
        "x_increment": x_increment, "y_increment": y_increment, "bounds": bounds,
        "valid_cells": valid_count, "null_cells": null_count,
        "valid_percentage": round(valid_count / total_cells * 100, 3) if valid_count is not None and total_cells else None,
        "value_min": value_min, "value_max": value_max,
        "crs": parse_crs_sidecar(path),
    }


def parse_petrel_profile(path: str | Path, max_points: int = 5000) -> dict[str, Any]:
    path = Path(path)
    profile_type = None
    points: list[list[Any]] = []
    point_count = 0
    line_names: set[str] = set()
    value_min = value_max = None
    trace_min = trace_max = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("PROFILE"):
                match = re.search(r"\bTYPE\s+(\d+)", line)
                profile_type = int(match.group(1)) if match else None
                continue
            if line.startswith(("SNAPPING", "#", "FFASCI", "FFATTR", "->")):
                continue
            values = line.split()
            if len(values) < 3:
                continue
            x, y = _float(values[0]), _float(values[1])
            if x is None or y is None:
                continue
            z_index = 4 if profile_type == 1 and len(values) > 4 else 2
            z = _float(values[z_index])
            trace = int(float(values[7])) if profile_type == 1 and len(values) > 7 and _float(values[7]) is not None else None
            name = values[9] if profile_type == 1 and len(values) > 9 else path.stem
            point_count += 1
            line_names.add(name)
            if z is not None:
                value_min = z if value_min is None else min(value_min, z)
                value_max = z if value_max is None else max(value_max, z)
            if trace is not None:
                trace_min = trace if trace_min is None else min(trace_min, trace)
                trace_max = trace if trace_max is None else max(trace_max, trace)
            if len(points) < max_points:
                points.append([x, y, z, name, trace])
            elif point_count % max(2, point_count // max_points) == 0:
                points[point_count % max_points] = [x, y, z, name, trace]
    xy = [(row[0], row[1]) for row in points]
    return {
        "path": str(path), "filename": path.name, "profile_type": profile_type,
        "point_count": point_count, "line_count": len(line_names), "line_names": sorted(line_names)[:200],
        "value_min": value_min, "value_max": value_max, "trace_min": trace_min, "trace_max": trace_max,
        "bounds": _point_bounds(xy), "points": points, "crs": parse_crs_sidecar(path),
    }


def parse_fault_sticks(path: str | Path, max_points: int = 5000) -> dict[str, Any]:
    path = Path(path)
    point_count = 0
    sticks: set[str] = set()
    points: list[list[float]] = []
    z_min = z_max = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            values = line.split()
            if len(values) < 8 or not values[0].upper().startswith(("INLINE", "XLINE", "CROSSLINE")):
                continue
            x, y, z = _float(values[3]), _float(values[4]), _float(values[5])
            if x is None or y is None or z is None:
                continue
            point_count += 1
            sticks.add(values[-1])
            z_min = z if z_min is None else min(z_min, z)
            z_max = z if z_max is None else max(z_max, z)
            if len(points) < max_points:
                points.append([x, y, z])
    return {
        "path": str(path), "filename": path.name, "fault_name": path.stem,
        "point_count": point_count, "stick_count": len(sticks), "z_min": z_min, "z_max": z_max,
        "bounds": _point_bounds([(row[0], row[1]) for row in points]), "points": points,
        "crs": parse_crs_sidecar(path),
    }


def parse_fault_model(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    faults: set[str] = set()
    pillars = 0
    points: list[list[float]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("FAULT"):
                match = re.search(r'FAULT\s+"([^"]+)"', stripped)
                if match:
                    faults.add(match.group(1))
            elif stripped.startswith("PILLAR"):
                pillars += 1
                values = stripped.split()
                if len(values) >= 4 and len(points) < 5000:
                    x, y, z = _float(values[1]), _float(values[2]), _float(values[3])
                    if x is not None and y is not None and z is not None:
                        points.append([x, y, z])
    return {"path": str(path), "filename": path.name, "fault_count": len(faults), "fault_names": sorted(faults), "pillar_count": pillars, "points": points}


def parse_fault_2d(path: str | Path, max_points: int = 5000) -> dict[str, Any]:
    path = Path(path)
    point_count = 0
    line_names: set[str] = set()
    points: list[list[float]] = []
    z_min = z_max = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            values = line.split()
            if len(values) < 5:
                continue
            x, y, z = _float(values[2]), _float(values[3]), _float(values[4])
            if x is None or y is None or z is None:
                continue
            point_count += 1
            line_names.add(values[0])
            z_min = z if z_min is None else min(z_min, z)
            z_max = z if z_max is None else max(z_max, z)
            if len(points) < max_points:
                points.append([x, y, z])
    return {"path": str(path), "filename": path.name, "point_count": point_count, "seismic_line_count": len(line_names), "line_names": sorted(line_names)[:500], "z_min": z_min, "z_max": z_max, "points": points}


def parse_crs_sidecar(data_path: str | Path) -> dict[str, Any] | None:
    data_path = Path(data_path)
    sidecar = Path(str(data_path) + ".crsmeta.xml")
    if not sidecar.is_file():
        return None
    try:
        root = ET.parse(sidecar).getroot()
        authority_codes = [element.text for element in root.iter() if element.tag.endswith("AuthorityCode") and element.text]
        descriptions = [element.text for element in root.iter() if element.tag.endswith("Description") and element.text]
        epsg = next((value.replace(",", ":") for value in authority_codes if value.upper().startswith("EPSG,")), None)
        return {"epsg": epsg, "authority_codes": authority_codes[:10], "description": descriptions[0].strip('"') if descriptions else None, "path": str(sidecar)}
    except (ET.ParseError, OSError):
        return None


def match_surface_to_seismic(surface: dict[str, Any], seismic: dict[str, Any]) -> dict[str, Any]:
    surface_bounds, grid = surface.get("bounds"), seismic.get("grid_transform")
    if not surface_bounds or not grid:
        return {"matched": False, "reason": "缺少 Surface 边界或 3D 道网格变换"}
    seismic_bounds = {key: seismic.get(key) for key in ("x_min", "x_max", "y_min", "y_max")}
    intersection = _bbox_intersection(surface_bounds, seismic_bounds)
    seismic_area = max(0.0, (seismic_bounds["x_max"] - seismic_bounds["x_min"]) * (seismic_bounds["y_max"] - seismic_bounds["y_min"]))
    surface_area = max(0.0, (surface_bounds["x_max"] - surface_bounds["x_min"]) * (surface_bounds["y_max"] - surface_bounds["y_min"]))
    inline_spacing, crossline_spacing = grid["inline_spacing"], grid["crossline_spacing"]
    ratio_x = (surface.get("x_increment") or 0) / crossline_spacing if crossline_spacing else None
    ratio_y = (surface.get("y_increment") or 0) / inline_spacing if inline_spacing else None
    coverage_inline = max(0, (seismic.get("inline_max") or 0) - (seismic.get("inline_min") or 0) + 1)
    coverage_crossline = max(0, (seismic.get("crossline_max") or 0) - (seismic.get("crossline_min") or 0) + 1)
    resampled_finer = ratio_x is not None and ratio_y is not None and min(ratio_x, ratio_y) < 0.9
    return {
        "matched": True,
        "seismic_coverage_percentage": round(intersection / seismic_area * 100, 3) if seismic_area else 0,
        "surface_overlap_percentage": round(intersection / surface_area * 100, 3) if surface_area else 0,
        "inline_count": coverage_inline,
        "crossline_count": coverage_crossline,
        "trace_grid": f"{coverage_inline} × {coverage_crossline}",
        "surface_grid": f"{surface.get('rows')} × {surface.get('columns')}",
        "surface_to_bin_ratio": [round(ratio_x, 3), round(ratio_y, 3)] if ratio_x is not None and ratio_y is not None else None,
        "interpretation_precision": "表面网格细于地震道距，属于重采样，真实独立解释精度不能高于 15m 道距" if resampled_finer else "表面网格与地震道距相当或更粗",
        "resampled_finer_than_seismic": resampled_finer,
    }


def match_fault_to_seismic(fault: dict[str, Any], seismic: dict[str, Any]) -> dict[str, Any]:
    grid = seismic.get("grid_transform")
    if not grid or not fault.get("points"):
        return {"matched": False, "reason": "缺少断层点或 3D 道网格变换"}
    distances: list[float] = []
    inside = 0
    for x, y, _ in fault["points"]:
        inline, crossline = _xy_to_grid(x, y, grid)
        if inline is None:
            continue
        if seismic["inline_min"] <= inline <= seismic["inline_max"] and seismic["crossline_min"] <= crossline <= seismic["crossline_max"]:
            inside += 1
        nearest_x, nearest_y = _grid_to_xy(round(inline), round(crossline), grid)
        distances.append(math.hypot(x - nearest_x, y - nearest_y))
    distances.sort()
    return {
        "matched": bool(distances), "inside_percentage": round(inside / len(distances) * 100, 3) if distances else 0,
        "median_snap_distance": round(statistics.median(distances), 4) if distances else None,
        "p95_snap_distance": round(_percentile(distances, 0.95), 4) if distances else None,
        "matched_points": len(distances), "stick_count": fault.get("stick_count"),
    }


def match_2d_points_to_seismic(horizon: dict[str, Any], seismic: dict[str, Any]) -> dict[str, Any]:
    seismic_name = _line_identity(seismic.get("filename", ""))
    available_lines = horizon.get("line_names", [])
    matched_line = max(available_lines, key=lambda name: len(_line_identity(name)) if _line_identity(name) and _line_identity(name) in seismic_name else 0, default=None)
    matching_identity = _line_identity(matched_line or "")
    horizon_points = [
        [row[0], row[1]] for row in horizon.get("points", [])
        if not matching_identity or _line_identity(row[3]) == matching_identity
    ]
    trace_points = seismic.get("trace_sample_points") or []
    if not horizon_points or not trace_points:
        return {"matched": False, "reason": "缺少 2D 层位点或地震道坐标"}
    horizon_points = _thin(horizon_points, 1500)
    trace_points = _thin(trace_points, 5000)
    spacings = [math.hypot(trace_points[i][0] - trace_points[i - 1][0], trace_points[i][1] - trace_points[i - 1][1]) for i in range(1, len(trace_points))]
    positive_spacings = [value for value in spacings if value > 1e-6]
    trace_spacing = statistics.median(positive_spacings) if positive_spacings else None
    distances = [min(math.hypot(point[0] - trace[0], point[1] - trace[1]) for trace in trace_points) for point in horizon_points]
    distances.sort()
    threshold = (trace_spacing or 25) * 0.75
    return {
        "matched": True, "horizon_points": horizon.get("point_count"), "seismic_traces": seismic.get("trace_count"),
        "matched_line": matched_line, "matched_horizon_sample_points": len(horizon_points),
        "trace_spacing": round(trace_spacing, 3) if trace_spacing else None,
        "median_snap_distance": round(statistics.median(distances), 3),
        "p95_snap_distance": round(_percentile(distances, 0.95), 3),
        "on_trace_percentage": round(sum(value <= threshold for value in distances) / len(distances) * 100, 3),
        "trace_range": [horizon.get("trace_min"), horizon.get("trace_max")],
    }


def build_quality_assessment(
    categories: list[dict[str, Any]], coverage: dict[str, Any], surface: dict[str, Any] | None,
    seismic: dict[str, Any] | None, matches: dict[str, Any], fault_files: int,
    fault_model: dict[str, Any] | None,
) -> dict[str, Any]:
    category_map = {row["key"]: row for row in categories}
    checks = [
        ("井头", bool(category_map.get("well_heads", {}).get("files")), 8, 1.0 if category_map.get("well_heads", {}).get("files") else 0.0),
        ("井轨迹覆盖", bool(category_map.get("well_paths", {}).get("files")), 12, coverage.get("head_with_dev_percentage", 0) / 100),
        ("测井覆盖", bool(category_map.get("well_logs", {}).get("files")), 12, coverage.get("head_with_las_percentage", 0) / 100),
        ("井顶", bool(category_map.get("well_tops", {}).get("files")), 8),
        ("3D 地震", seismic is not None, 16),
        ("层位", surface is not None or bool(category_map.get("horizons", {}).get("files")), 14),
        ("断层", fault_files > 0, 10),
        ("Polygon", bool(category_map.get("polygons", {}).get("files")), 8),
        ("生产", bool(category_map.get("production", {}).get("files")), 8),
    ]
    normalized_checks = [row if len(row) == 4 else (*row, 1.0 if row[1] else 0.0) for row in checks]
    score = round(sum(weight * max(0.0, min(1.0, factor)) for _, _, weight, factor in normalized_checks))
    alerts: list[dict[str, str]] = []
    surface_match = matches.get("surface_to_3d") or {}
    if surface_match.get("resampled_finer_than_seismic"):
        alerts.append({"level": "warning", "title": "层位网格存在超采样", "detail": surface_match["interpretation_precision"]})
    if surface_match.get("seismic_coverage_percentage", 0) < 95:
        alerts.append({"level": "warning", "title": "3D 层位未全面覆盖地震", "detail": f"地震范围覆盖率 {surface_match.get('seismic_coverage_percentage', 0):.1f}%"})
    if coverage.get("head_with_las_percentage", 0) < 80:
        alerts.append({"level": "critical", "title": "井头与 LAS 覆盖不足", "detail": f"井头井中有 LAS 的比例约 {coverage.get('head_with_las_percentage', 0):.1f}%（按文件名初判）"})
    if coverage.get("head_with_dev_percentage", 0) < 80:
        alerts.append({"level": "warning", "title": "井轨迹覆盖不足", "detail": f"井头井中有 DEV 的比例约 {coverage.get('head_with_dev_percentage', 0):.1f}%（按文件名初判）"})
    if not fault_model or not fault_model.get("fault_count"):
        alerts.append({"level": "warning", "title": "缺少可识别断层模型", "detail": "仅发现解释线，未识别结构模型"})
    return {
        "score": score, "grade": "完整" if score >= 85 else ("基本完整" if score >= 70 else "需补充"),
        "checks": [{"name": name, "passed": passed, "weight": weight, "factor": round(factor, 3)} for name, passed, weight, factor in normalized_checks],
        "alerts": alerts,
    }


def build_visualization_payload(
    wellheads: dict[str, Any], surface: dict[str, Any] | None, horizon_2d: dict[str, Any] | None,
    fault: dict[str, Any] | None, seismic_3d: dict[str, Any] | None, seismic_2d: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "wells": wellheads.get("points", []),
        "surface_bounds": surface.get("bounds") if surface else None,
        "surface_grid": {"rows": surface.get("rows"), "columns": surface.get("columns"), "z_min": surface.get("value_min"), "z_max": surface.get("value_max")} if surface else None,
        "horizon_2d_points": _thin(horizon_2d.get("points", []), 2000) if horizon_2d else [],
        "fault_points": _thin(fault.get("points", []), 2000) if fault else [],
        "seismic_3d_footprint": seismic_3d.get("footprint", []) if seismic_3d else [],
        "seismic_2d_points": seismic_2d.get("trace_sample_points", []) if seismic_2d else [],
    }


def _category_summary(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    definitions = [
        ("well_heads", "井头", lambda p: "wellhead" in _relative_lower(root, p)),
        ("well_paths", "井轨迹", lambda p: p.suffix.lower() == ".dev"),
        ("well_logs", "测井 LAS", lambda p: p.suffix.lower() == ".las" and not is_time_depth_file(root, p)),
        ("checkshots", "时深关系", lambda p: is_time_depth_file(root, p)),
        ("well_tops", "井顶", lambda p: "welltops" in _relative_lower(root, p)),
        ("core", "岩心标定", lambda p: any(token in _relative_lower(root, p) for token in ("core", "rock", "plug", "thin section", "岩心", "岩芯", "取心", "岩样", "薄片"))),
        ("interpretations", "解释结论", lambda p: any(token in _relative_lower(root, p) for token in ("interpret", "reservoir", "解释", "孔渗", "饱和"))),
        ("seismic_3d", "3D 地震", lambda p: p.suffix.lower() in SEISMIC_EXTENSIONS and "2d_sinopec" not in _relative_lower(root, p)),
        ("seismic_2d", "2D 地震", lambda p: p.suffix.lower() in SEGY_EXTENSIONS and "2d_sinopec" in _relative_lower(root, p)),
        ("horizons", "层位", lambda p: "horizon" in _relative_lower(root, p) or p.suffix.lower() == ".ptd"),
        ("faults", "断层", lambda p: "fault" in _relative_lower(root, p)),
        ("polygons", "Polygon", lambda p: "polygon" in _relative_lower(root, p) or p.suffix.lower() == ".shp"),
        ("production", "生产数据", lambda p: p.suffix.lower() in {".mdb", ".accdb"} or any(token in _relative_lower(root, p) for token in ("production", "ofm", "生产"))),
    ]
    rows = []
    for key, label, predicate in definitions:
        matched = [path for path in files if predicate(path)]
        rows.append({"key": key, "label": label, "files": len(matched), "bytes": sum(path.stat().st_size for path in matched), "gb": round(sum(path.stat().st_size for path in matched) / 1024 ** 3, 3)})
    return rows


def _top_folder_summary(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        relative = path.relative_to(root)
        grouped[relative.parts[0]].append(path)
    return [
        {"name": name, "files": len(paths), "gb": round(sum(path.stat().st_size for path in paths) / 1024 ** 3, 3), "extensions": dict(Counter(path.suffix.lower() or "[none]" for path in paths).most_common(8))}
        for name, paths in sorted(grouped.items())
    ]


def _well_file_coverage(wellhead_rows: list[dict[str, Any]], las_files: list[Path], dev_files: list[Path], checkshot_files: list[Path]) -> dict[str, Any]:
    head = {normalize_well_name(row.get("name")) for row in wellhead_rows if row.get("name")}
    las = {_well_key_from_filename(path) for path in las_files}
    dev = {_well_key_from_filename(path) for path in dev_files}
    checkshot = {_well_key_from_filename(path) for path in checkshot_files}
    universe = head | las | dev | checkshot
    return {
        "estimated_unique_wells": len(universe), "wellhead_wells": len(head), "las_wells": len(las), "dev_wells": len(dev), "checkshot_wells": len(checkshot),
        "head_las_wells": len(head & las), "head_dev_wells": len(head & dev), "all_core_wells": len(head & las & dev),
        "head_with_las_percentage": round(len(head & las) / len(head) * 100, 2) if head else 0,
        "head_with_dev_percentage": round(len(head & dev) / len(head) * 100, 2) if head else 0,
        "las_with_dev_percentage": round(len(las & dev) / len(las) * 100, 2) if las else 0,
        "method": "井头内容 + LAS/DEV/Checkshot 文件名规范化后的初判；缩写井名尚未人工别名校正",
    }


def _well_key_from_filename(path: Path) -> str:
    stem = re.sub(r"(?i)(?:[_\- ]?(?:LOGS?|TZ(?:[_\- ]?3D)?|TDR|OWT|TWT|CHECKSHOTS?|TIME[_\- ]?DEPTH))+$", "", path.stem)
    return normalize_well_name(stem)


def _line_identity(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    normalized = re.sub(r"(?:PSTM[PKS]?|CFCG|TWT|MIGRATION|MIGRATED|STACK|FINAL)", "", normalized)
    return normalized


def _choose_3d_seismic(paths: list[Path]) -> Path | None:
    candidates = [path for path in paths if "2d_sinopec" not in str(path).lower()]
    preferred = [path for path in candidates if "geobody_estimation" in path.name.lower()]
    return _median_file(preferred) if preferred else (min(candidates, key=lambda path: path.stat().st_size) if candidates else None)


def _choose_matching_2d_seismic(paths: list[Path], horizon: dict[str, Any] | None) -> Path | None:
    candidates = [path for path in paths if "2d_sinopec" in str(path).lower()]
    if horizon and horizon.get("line_names"):
        tokens = [re.sub(r"[^A-Z0-9]", "", name.upper()).replace("TWT", "") for name in horizon["line_names"]]
        scored = []
        for path in candidates:
            normalized = re.sub(r"[^A-Z0-9]", "", path.stem.upper())
            score = max((len(token) for token in tokens if token and token in normalized), default=0)
            scored.append((score, -path.stat().st_size, path))
        best = max(scored, key=lambda row: (row[0], row[1])) if scored else None
        if best and best[0] > 0:
            return best[2]
    return min(candidates, key=lambda path: path.stat().st_size) if candidates else None


def _grid_to_xy(inline: float, crossline: float, grid: dict[str, Any]) -> tuple[float, float]:
    a, b, c = grid["x_coefficients"]
    d, e, f = grid["y_coefficients"]
    return a * inline + b * crossline + c, d * inline + e * crossline + f


def _xy_to_grid(x: float, y: float, grid: dict[str, Any]) -> tuple[float | None, float | None]:
    a, b, c = grid["x_coefficients"]
    d, e, f = grid["y_coefficients"]
    determinant = a * e - b * d
    if abs(determinant) < 1e-12:
        return None, None
    offset_x, offset_y = x - c, y - f
    return (offset_x * e - b * offset_y) / determinant, (a * offset_y - offset_x * d) / determinant


def _bbox_intersection(left: dict[str, Any], right: dict[str, Any]) -> float:
    width = max(0.0, min(left["x_max"], right["x_max"]) - max(left["x_min"], right["x_min"]))
    height = max(0.0, min(left["y_max"], right["y_max"]) - max(left["y_min"], right["y_min"]))
    return width * height


def _point_bounds(points: list[tuple[float, float]]) -> dict[str, float] | None:
    if not points:
        return None
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    return {"x_min": min(xs), "x_max": max(xs), "y_min": min(ys), "y_max": max(ys)}


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return math.nan
    index = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * fraction)))
    return sorted_values[index]


def _thin(values: list[Any], limit: int) -> list[Any]:
    if len(values) <= limit:
        return values
    stride = math.ceil(len(values) / limit)
    return values[::stride]


def _float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _first_number(text: str) -> float | None:
    match = re.search(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?", text)
    return _float(match.group(0)) if match else None


def _petrel_tokens(line: str) -> list[str]:
    # Quotes denoting arc-seconds occur inside unquoted latitude/longitude
    # tokens, so shlex is not suitable. Only treat quotes at token boundaries
    # as Petrel string delimiters.
    tokens: list[str] = []
    for match in re.finditer(r'(?<!\S)"([^"]*)"(?=\s|$)|(\S+)', line):
        tokens.append(match.group(1) if match.group(1) is not None else match.group(2))
    return tokens


def _relative_lower(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("/", "\\").lower()


def _extension_priority(extension: str) -> int:
    order = {".las": 10, ".dev": 10, ".sgy": 10, ".segy": 10, ".seg-y": 10, ".mdb": 10, ".accdb": 10, ".ptd": 9, ".shp": 8, "[none]": 8, ".txt": 7, ".prn": 6, ".xlsx": 6, ".xls": 6, ".xml": 1}
    return order.get(extension, 4)


def _median_file(paths: list[Path]) -> Path:
    ordered = sorted(paths, key=lambda path: path.stat().st_size)
    return ordered[(len(ordered) - 1) // 2]


def _first_business_file(files: list[Path], predicate) -> Path | None:
    return next((path for path in files if predicate(path)), None)


def _prefer_named(files: list[Path], name: str) -> Path | None:
    return next((path for path in files if path.name.lower() == name.lower()), None)


def _resolve_optional_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"代表文件不存在：{resolved}")
    return resolved
