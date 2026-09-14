from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .analytics import interpretation_statistics
from .curve_analysis import curve_statistics, is_time_depth_mnemonic
from .db import utcnow
from .importers import normalize_well_name
from .project_catalog import parse_dev_stations


DEFAULT_INTERPRETATION_TYPES = [
    {"key": "unclassified", "name": "待分类", "role": "PENDING", "color": "#718079", "aliases": []},
    {"key": "porosity", "name": "孔隙度", "role": "PHI", "color": "#4eb3d3", "aliases": ["PHI", "POR", "PORO", "POROSITY", "孔隙度"]},
    {"key": "permeability", "name": "渗透率", "role": "PERM", "color": "#e6a75d", "aliases": ["PERM", "PERMEABILITY", "K", "渗透率"]},
    {"key": "saturation", "name": "饱和度", "role": "SAT", "color": "#9b8afb", "aliases": ["SW", "SO", "SG", "SAT", "SATURATION", "含油饱和度", "含水饱和度", "饱和度"]},
    {"key": "facies", "name": "岩相 / 岩性", "role": "FACIES", "color": "#53c7b7", "aliases": ["FACIES", "LITH", "LITHOLOGY", "ROCKTYPE", "岩相", "岩性"]},
    {"key": "net_gross", "name": "净毛比", "role": "NTG", "color": "#b9e94b", "aliases": ["NTG", "NETGROSS", "净毛比"]},
    {"key": "pay", "name": "有效储层 / 油层", "role": "PAY", "color": "#ef7b74", "aliases": ["PAY", "NETPAY", "RESERVOIR", "有效厚度", "油层"]},
]


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
CORE_TOKENS = ("core", "rock", "plug", "box", "岩心", "岩芯", "取心", "岩样", "薄片")
GENERIC_FOLDERS = {"core", "cores", "photo", "photos", "image", "images", "岩心", "岩心照片", "岩芯照片", "取心"}


def _project_root(snapshot: dict[str, Any]) -> str:
    return str(Path(snapshot["project"]["root"]).resolve())


def ensure_default_interpretation_types(conn: sqlite3.Connection, project_root: str) -> None:
    for item in DEFAULT_INTERPRETATION_TYPES:
        conn.execute(
            """INSERT OR IGNORE INTO interpretation_types(
               project_root,type_key,name,canonical_role,color,aliases_json,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (project_root, item["key"], item["name"], item["role"], item["color"], json.dumps(item["aliases"], ensure_ascii=False), utcnow()),
        )
    conn.commit()


def suggest_interpretation_type(attribute_name: str) -> dict[str, Any]:
    normalized = re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", attribute_name.upper())
    for item in DEFAULT_INTERPRETATION_TYPES[1:]:
        for alias in item["aliases"]:
            candidate = re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", alias.upper())
            if normalized == candidate or (len(candidate) >= 3 and candidate in normalized):
                return {"type_key": item["key"], "confidence": 0.92 if normalized == candidate else 0.72, "reason": f"命中 {alias} 同义词"}
    return {"type_key": None, "confidence": 0.0, "reason": "未命中解释结论类型规则"}


def interpretation_workbench(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    curve_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _project_root(snapshot)
    ensure_default_interpretation_types(conn, root)
    rows = interpretation_statistics(conn, "interpretation")
    for row in rows:
        row["origin"] = "interpretation_file"
    structured_names = {str(row["attribute_name"]).upper() for row in rows}
    curve_owned = {
        str(row["mnemonic"]).upper()
        for row in conn.execute(
            """SELECT mnemonic FROM curve_type_assignments
               WHERE project_root=? AND type_key!='unclassified'""",
            (root,),
        )
    }
    if curve_profile:
        for curve in curve_statistics(curve_profile):
            mnemonic = str(curve["mnemonic"]).upper()
            if (
                is_time_depth_mnemonic(mnemonic)
                or mnemonic in structured_names
                or mnemonic in curve_owned
            ):
                continue
            rows.append({
                "attribute_name": mnemonic,
                "well_count": curve.get("well_count", 0),
                "version_count": curve.get("file_count", 0),
                "versions": "LAS 原始字段",
                "distinct_value_count": None,
                "numeric_min": curve.get("value_min"),
                "numeric_max": curve.get("value_max"),
                "origin": "las_curve",
            })
    assignments = {row["attribute_name"]: dict(row) for row in conn.execute(
        "SELECT * FROM interpretation_type_assignments WHERE project_root=?", (root,)
    )}
    for row in rows:
        suggestion = suggest_interpretation_type(row["attribute_name"])
        row["suggestion"] = suggestion
        if row["attribute_name"] not in assignments and suggestion["type_key"]:
            conn.execute(
                """INSERT OR IGNORE INTO interpretation_type_assignments(
                   project_root,attribute_name,type_key,status,updated_at) VALUES(?,?,?,?,?)""",
                (root, row["attribute_name"], suggestion["type_key"], "automatic", utcnow()),
            )
    conn.commit()
    assignments = {row["attribute_name"]: dict(row) for row in conn.execute(
        "SELECT * FROM interpretation_type_assignments WHERE project_root=?", (root,)
    )}
    types = []
    for value in conn.execute("SELECT * FROM interpretation_types WHERE project_root=? ORDER BY rowid", (root,)):
        item = dict(value)
        item["aliases"] = json.loads(item.pop("aliases_json") or "[]")
        types.append(item)
    for row in rows:
        row["assignment"] = assignments.get(row["attribute_name"])
        row.setdefault("suggestion", suggest_interpretation_type(row["attribute_name"]))
    return {"types": types, "attributes": rows}


def create_interpretation_type(
    conn: sqlite3.Connection,
    project_root: str,
    name: str,
    role: str | None = None,
    color: str = "#37c8c2",
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("解释结论类型名称不能为空")
    base = re.sub(r"[^a-z0-9]+", "_", (role or clean_name).lower()).strip("_") or "custom_interpretation"
    key, index = base, 2
    while conn.execute("SELECT 1 FROM interpretation_types WHERE project_root=? AND type_key=?", (project_root, key)).fetchone():
        key = f"{base}_{index}"
        index += 1
    conn.execute(
        """INSERT INTO interpretation_types(
           project_root,type_key,name,canonical_role,color,aliases_json,created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (project_root, key, clean_name, role, color, "[]", utcnow()),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM interpretation_types WHERE project_root=? AND type_key=?", (project_root, key)).fetchone())


def assign_interpretation_type(
    conn: sqlite3.Connection,
    project_root: str,
    attribute_name: str,
    type_key: str | None,
) -> None:
    type_key = type_key or "unclassified"
    if not conn.execute("SELECT 1 FROM interpretation_types WHERE project_root=? AND type_key=?", (project_root, type_key)).fetchone():
        raise ValueError("目标解释结论类型不存在")
    conn.execute(
        """INSERT INTO interpretation_type_assignments(
           project_root,attribute_name,type_key,status,updated_at) VALUES(?,?,?,?,?)
           ON CONFLICT(project_root,attribute_name) DO UPDATE SET
           type_key=excluded.type_key,status='confirmed',updated_at=excluded.updated_at""",
        (project_root, attribute_name, type_key, "confirmed", utcnow()),
    )
    if type_key != "unclassified":
        conn.execute(
            "DELETE FROM curve_type_assignments WHERE project_root=? AND UPPER(mnemonic)=UPPER(?)",
            (project_root, attribute_name),
        )
    conn.commit()


def parse_core_depth_range(filename: str) -> dict[str, Any] | None:
    stem = Path(filename).stem.upper().replace(",", "")
    match = re.search(
        r"(?P<top>\d+(?:\.\d+)?)\s*(?P<unit1>FT|FEET|M|METERS?)?\s*(?:-|–|—|~|TO|至|到|_)\s*"
        r"(?P<base>\d+(?:\.\d+)?)\s*(?P<unit2>FT|FEET|M|METERS?)?",
        stem,
    )
    if not match:
        return None
    top, base = float(match.group("top")), float(match.group("base"))
    if base < top:
        top, base = base, top
    raw_unit = match.group("unit1") or match.group("unit2") or "UNKNOWN"
    unit = "ft" if raw_unit.startswith("F") else "m" if raw_unit.startswith("M") else None
    factor = 0.3048 if unit == "ft" else 1.0
    return {
        "top": top, "base": base, "unit": unit or "未标注",
        "top_md_m": round(top * factor, 4) if unit else None,
        "base_md_m": round(base * factor, 4) if unit else None,
    }


def _infer_well_key(relative_path: str, known_wells: dict[str, dict[str, Any]]) -> str | None:
    parts = Path(relative_path).parts[:-1]
    for part in reversed(parts):
        if part.lower() in GENERIC_FOLDERS:
            continue
        key = normalize_well_name(part)
        if key in known_wells:
            return key
    filename_key = normalize_well_name(Path(relative_path).stem)
    candidates = [key for key in known_wells if len(key) >= 3 and filename_key.startswith(key)]
    return max(candidates, key=len) if candidates else None


def core_calibration_payload(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    well_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    root = _project_root(snapshot)
    known_wells = {row["project_key"]: row for row in well_rows}
    catalog_rows = [dict(row) for row in conn.execute(
        """SELECT id,file_path,relative_path,filename,extension,bytes,source_folder
           FROM project_catalog_items WHERE project_root=? ORDER BY relative_path""",
        (root,),
    )]
    images = []
    for item in catalog_rows:
        path_text = item["relative_path"].lower()
        if item["extension"] not in IMAGE_EXTENSIONS or not any(token in path_text for token in CORE_TOKENS):
            continue
        depth = parse_core_depth_range(item["filename"])
        well_key = _infer_well_key(item["relative_path"], known_wells)
        images.append({
            **item,
            "well_key": well_key,
            "well_name": known_wells.get(well_key, {}).get("canonical_name") if well_key else None,
            "depth": depth,
            "url": f"/api/core-media/{item['id']}",
        })
    grouped = []
    for key in sorted({item["well_key"] for item in images if item["well_key"]}):
        well = known_wells[key]
        media = [item for item in images if item["well_key"] == key]
        stations = []
        if well.get("_dev_paths"):
            try:
                stations = parse_dev_stations(well["_dev_paths"][0])
            except (OSError, ValueError):
                stations = []
        stride = max(1, len(stations) // 100)
        grouped.append({
            "well_key": key,
            "well_name": well["canonical_name"],
            "image_count": len(media),
            "parsed_intervals": sum(item["depth"] is not None for item in media),
            "images": media,
            "trajectory": [{field: row[field] for field in ("md", "x", "y", "z")} for row in stations[::stride]],
            "max_md": max((row["md"] for row in stations), default=well.get("total_depth") or well.get("max_survey_md")),
        })
    return {
        "image_count": len(images),
        "matched_images": sum(item["well_key"] is not None for item in images),
        "parsed_depth_images": sum(item["depth"] is not None for item in images),
        "wells": grouped,
        "unmatched_images": [item for item in images if item["well_key"] is None],
        "core_attributes": interpretation_statistics(conn, "core"),
        "design_ready": True,
    }
