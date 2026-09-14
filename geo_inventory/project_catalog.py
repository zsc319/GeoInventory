from __future__ import annotations

import math
import os
import re
import shlex
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

from .curve_analysis import is_time_depth_mnemonic
from .db import Database
from .importers import identity_similarity, normalize_well_name
from .project_scan import SEISMIC_EXTENSIONS
from .well_identity import classify_well_identity


CATEGORY_LABELS = {
    "well_heads": "井头",
    "well_paths": "井轨迹",
    "well_logs": "测井 LAS",
    "checkshots": "Checkshot",
    "well_tops": "井顶",
    "core": "岩心标定",
    "interpretations": "解释结论",
    "seismic_3d": "3D 地震",
    "seismic_2d": "2D 地震",
    "horizons": "层位",
    "faults": "断层",
    "polygons": "Polygon",
    "production": "生产数据",
    "other": "其他资料",
}

SOURCE_TYPES = {
    "well_heads": "well_head",
    "well_paths": "deviation",
    "well_logs": "las",
    "checkshots": "checkshot",
    "well_tops": "well_top",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_file(root: Path, path: Path) -> str:
    relative = str(path.relative_to(root)).replace("/", "\\").lower()
    suffix = path.suffix.lower()
    if "checkshot" in relative and suffix == ".las":
        return "checkshots"
    if suffix == ".las":
        return "well_logs"
    if suffix == ".dev":
        return "well_paths"
    if "wellhead" in relative or "井坐标" in relative:
        return "well_heads"
    if "welltop" in relative or "井顶" in relative:
        return "well_tops"
    if suffix in SEISMIC_EXTENSIONS:
        return "seismic_2d" if "2d_sinopec" in relative or "sísmica 2d" in relative else "seismic_3d"
    if "fault" in relative or "断层" in relative:
        return "faults"
    if "horizon" in relative or "层位" in relative or suffix == ".ptd":
        return "horizons"
    if "polygon" in relative or suffix == ".shp":
        return "polygons"
    if suffix in {".mdb", ".accdb"} or "production" in relative or "生产" in relative or "ofm" in relative:
        return "production"
    if any(token in relative for token in ("core", "rock", "plug", "thin section", "岩心", "岩芯", "取心", "岩样", "薄片")):
        return "core"
    if any(token in relative for token in ("interpret", "reservoir", "解释", "孔渗", "饱和")):
        return "interpretations"
    return "other"


def sync_project_catalog(database: Database, snapshot: dict[str, Any]) -> dict[str, int]:
    project = snapshot.get("project") or {}
    root = Path(project.get("root") or "").resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{root}")
    representative_paths = {str(Path(row["path"]).resolve()).lower() for row in snapshot.get("representatives", [])}
    rows: list[tuple[Any, ...]] = []
    for directory, _, filenames in os.walk(root):
        parent = Path(directory)
        for filename in filenames:
            path = parent / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(root)
            source_folder = str(relative.parent) if str(relative.parent) != "." else "项目根目录"
            rows.append((
                str(root), str(path), str(relative), path.name, path.suffix.lower() or "[none]",
                classify_file(root, path), source_folder, stat.st_size,
                datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                1 if str(path.resolve()).lower() in representative_paths else 0,
            ))
    with database.connect() as conn:
        conn.executemany(
            """INSERT INTO project_catalog_items(
                project_root,file_path,relative_path,filename,extension,category_key,source_folder,
                bytes,modified_at,representative
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(file_path) DO UPDATE SET
                project_root=excluded.project_root,relative_path=excluded.relative_path,
                filename=excluded.filename,extension=excluded.extension,category_key=excluded.category_key,
                source_folder=excluded.source_folder,bytes=excluded.bytes,modified_at=excluded.modified_at,
                representative=excluded.representative""",
            rows,
        )
        conn.commit()
    return {"items": len(rows), "representatives": len(representative_paths)}


def ensure_project_catalog(database: Database, snapshot: dict[str, Any] | None) -> None:
    if not snapshot or snapshot.get("empty"):
        return
    root = str(Path(snapshot["project"]["root"]).resolve())
    expected = int(snapshot["project"].get("total_files") or 0)
    with database.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) count FROM project_catalog_items WHERE project_root=?", (root,)
        ).fetchone()
    if not row or int(row["count"]) < expected:
        sync_project_catalog(database, snapshot)


def catalog_payload(
    conn: sqlite3.Connection,
    project_root: str,
    category: str | None = None,
    query: str = "",
    group_id: int | None = None,
    page: int = 1,
    per_page: int = 100,
) -> dict[str, Any]:
    where = ["i.project_root=?"]
    params: list[Any] = [project_root]
    if category:
        where.append("i.category_key=?")
        params.append(category)
    join = "LEFT JOIN project_catalog_group_items gi ON gi.item_id=i.id"
    if group_id is not None:
        where.append("gi.group_id=?")
        params.append(group_id)
    clause = " AND ".join(where)
    page = max(1, page)
    per_page = min(500, max(20, per_page))
    select_sql = f"""SELECT i.*,dt.display_name,g.id group_id,g.name group_name
        FROM project_catalog_items i
        LEFT JOIN directory_display_translations dt
          ON dt.project_root=i.project_root AND dt.file_path=i.file_path
        LEFT JOIN project_catalog_group_items gi ON gi.item_id=i.id
        LEFT JOIN project_catalog_groups g ON g.id=gi.group_id
        WHERE {clause}"""
    if query.strip():
        query_text = query.strip()
        query_lower = query_text.lower()
        query_compact = re.sub(r"[^a-z0-9]", "", query_lower)
        query_digits = re.findall(r"\d+", query_lower)
        query_stem = Path(query_text).stem
        matched: list[dict[str, Any]] = []
        for raw in conn.execute(f"{select_sql} ORDER BY i.representative DESC,i.filename COLLATE NOCASE", params):
            item = dict(raw)
            filename_lower = item["filename"].lower()
            display_lower = str(item.get("display_name") or item["filename"]).lower()
            relative_lower = item["relative_path"].lower()
            filename_compact = re.sub(r"[^a-z0-9]", "", filename_lower)
            display_compact = re.sub(r"[^a-z0-9]", "", display_lower)
            exact = query_lower in filename_lower or query_lower in display_lower or query_lower in relative_lower
            compact = bool(query_compact and (query_compact in filename_compact or query_compact in display_compact))
            identity_score = max(
                identity_similarity(query_stem, Path(item["filename"]).stem),
                identity_similarity(query_stem, Path(str(item.get("display_name") or item["filename"])).stem),
            )
            text_score = max(
                SequenceMatcher(None, query_compact, filename_compact).ratio() if query_compact else 0.0,
                SequenceMatcher(None, query_compact, display_compact).ratio() if query_compact else 0.0,
            )
            score = 1.0 if exact else 0.94 if compact else max(identity_score, text_score)
            if not exact and not compact and query_digits and re.findall(r"\d+", filename_lower) != query_digits:
                score = 0.0
            if exact or compact or score >= 0.72:
                item["match_score"] = round(score, 3)
                item["match_kind"] = "精确包含" if exact else "忽略符号" if compact else "井名/对象模糊匹配"
                matched.append(item)
        matched.sort(key=lambda item: (-item["match_score"], not bool(item["representative"]), item["filename"].upper()))
        total = len(matched)
        offset = (page - 1) * per_page
        items = matched[offset:offset + per_page]
    else:
        total = conn.execute(f"SELECT COUNT(DISTINCT i.id) count FROM project_catalog_items i {join} WHERE {clause}", params).fetchone()["count"]
        items = [dict(row) for row in conn.execute(
            f"{select_sql} ORDER BY i.representative DESC,i.filename COLLATE NOCASE LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page),
        )]
    counts = [dict(row) for row in conn.execute(
        """SELECT category_key,COUNT(*) files,SUM(bytes) bytes
           FROM project_catalog_items WHERE project_root=? GROUP BY category_key ORDER BY files DESC""",
        (project_root,),
    )]
    folders = [dict(row) for row in conn.execute(
        f"""SELECT i.source_folder name,COUNT(*) files,SUM(i.bytes) bytes
            FROM project_catalog_items i WHERE {' AND '.join(where[:2] if category else where[:1])}
            GROUP BY i.source_folder ORDER BY files DESC LIMIT 80""",
        params[:2] if category else params[:1],
    )]
    groups = [dict(row) for row in conn.execute(
        """SELECT g.*,COUNT(gi.item_id) item_count,COALESCE(SUM(i.bytes),0) bytes
           FROM project_catalog_groups g
           LEFT JOIN project_catalog_group_items gi ON gi.group_id=g.id
           LEFT JOIN project_catalog_items i ON i.id=gi.item_id
           WHERE g.project_root=? GROUP BY g.id ORDER BY g.category_key,g.name""",
        (project_root,),
    )]
    return {
        "items": items, "total": total, "page": page, "per_page": per_page,
        "pages": max(1, math.ceil(total / per_page)), "counts": counts,
        "folders": folders, "groups": groups,
    }


def create_group(conn: sqlite3.Connection, project_root: str, category: str, name: str, parent_id: int | None = None) -> dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("文件夹名称不能为空")
    cursor = conn.execute(
        "INSERT INTO project_catalog_groups(project_root,category_key,parent_id,name,created_at) VALUES(?,?,?,?,?)",
        (project_root, category, parent_id, name, utcnow()),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM project_catalog_groups WHERE id=?", (cursor.lastrowid,)).fetchone())


def update_catalog_group(conn: sqlite3.Connection, project_root: str, group_id: int, name: str) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("资料夹名称不能为空")
    cursor = conn.execute("UPDATE project_catalog_groups SET name=? WHERE id=? AND project_root=?", (clean_name, int(group_id), project_root))
    if not cursor.rowcount:
        raise ValueError("资料夹不存在")
    conn.commit()
    return dict(conn.execute("SELECT * FROM project_catalog_groups WHERE id=?", (int(group_id),)).fetchone())


def delete_catalog_group(conn: sqlite3.Connection, project_root: str, group_id: int) -> None:
    cursor = conn.execute("DELETE FROM project_catalog_groups WHERE id=? AND project_root=?", (int(group_id), project_root))
    if not cursor.rowcount:
        raise ValueError("资料夹不存在")
    conn.commit()


def unassign_items_from_group(conn: sqlite3.Connection, project_root: str, item_ids: list[int]) -> int:
    if not item_ids:
        return 0
    valid = [row["id"] for row in conn.execute(
        f"SELECT id FROM project_catalog_items WHERE project_root=? AND id IN ({','.join('?' for _ in item_ids)})",
        (project_root, *item_ids),
    )]
    if not valid:
        return 0
    cursor = conn.execute(
        f"DELETE FROM project_catalog_group_items WHERE item_id IN ({','.join('?' for _ in valid)})", valid,
    )
    conn.commit()
    return int(cursor.rowcount)


def assign_items_to_group(conn: sqlite3.Connection, project_root: str, group_id: int, item_ids: list[int]) -> int:
    group = conn.execute(
        "SELECT id FROM project_catalog_groups WHERE id=? AND project_root=?", (group_id, project_root)
    ).fetchone()
    if not group:
        raise ValueError("目标文件夹不存在")
    valid = [row["id"] for row in conn.execute(
        f"SELECT id FROM project_catalog_items WHERE project_root=? AND id IN ({','.join('?' for _ in item_ids)})",
        (project_root, *item_ids),
    )] if item_ids else []
    conn.executemany(
        """INSERT INTO project_catalog_group_items(group_id,item_id) VALUES(?,?)
           ON CONFLICT(item_id) DO UPDATE SET group_id=excluded.group_id""",
        [(group_id, item_id) for item_id in valid],
    )
    conn.commit()
    return len(valid)


def list_well_groups(conn: sqlite3.Connection, project_root: str) -> list[dict[str, Any]]:
    """Return delivery groups with their explicit well-key memberships."""
    rows = [dict(row) for row in conn.execute(
        """SELECT g.*,COUNT(m.well_key) member_count
           FROM project_well_groups g
           LEFT JOIN project_well_group_members m ON m.group_id=g.id
           WHERE g.project_root=? GROUP BY g.id ORDER BY g.name COLLATE NOCASE""",
        (project_root,),
    )]
    if not rows:
        return []
    members: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute(
        """SELECT m.group_id,m.well_key FROM project_well_group_members m
           JOIN project_well_groups g ON g.id=m.group_id
           WHERE g.project_root=? ORDER BY m.well_key""",
        (project_root,),
    ):
        members[int(row["group_id"])].append(row["well_key"])
    for row in rows:
        row["well_keys"] = members.get(int(row["id"]), [])
    return rows


def create_well_group(conn: sqlite3.Connection, project_root: str, name: str, description: str | None = None) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("井组名称不能为空")
    now = utcnow()
    cursor = conn.execute(
        """INSERT INTO project_well_groups(project_root,name,description,created_at,updated_at)
           VALUES(?,?,?,?,?)""",
        (project_root, clean_name, str(description or "").strip() or None, now, now),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM project_well_groups WHERE id=?", (cursor.lastrowid,)).fetchone())


def update_well_group(conn: sqlite3.Connection, project_root: str, group_id: int, name: str, description: str | None = None) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("井组名称不能为空")
    cursor = conn.execute(
        """UPDATE project_well_groups SET name=?,description=?,updated_at=?
           WHERE id=? AND project_root=?""",
        (clean_name, str(description or "").strip() or None, utcnow(), int(group_id), project_root),
    )
    if not cursor.rowcount:
        raise ValueError("井组不存在")
    conn.commit()
    return dict(conn.execute("SELECT * FROM project_well_groups WHERE id=?", (int(group_id),)).fetchone())


def delete_well_group(conn: sqlite3.Connection, project_root: str, group_id: int) -> None:
    cursor = conn.execute("DELETE FROM project_well_groups WHERE id=? AND project_root=?", (int(group_id), project_root))
    if not cursor.rowcount:
        raise ValueError("井组不存在")
    conn.commit()


def replace_well_group_members(conn: sqlite3.Connection, project_root: str, group_id: int, well_keys: list[str], valid_well_keys: set[str]) -> int:
    exists = conn.execute("SELECT id FROM project_well_groups WHERE id=? AND project_root=?", (int(group_id), project_root)).fetchone()
    if not exists:
        raise ValueError("井组不存在")
    keys = sorted({normalize_well_name(value) for value in well_keys if normalize_well_name(value) in valid_well_keys})
    conn.execute("DELETE FROM project_well_group_members WHERE group_id=?", (int(group_id),))
    conn.executemany("INSERT INTO project_well_group_members(group_id,well_key) VALUES(?,?)", [(int(group_id), key) for key in keys])
    conn.execute("UPDATE project_well_groups SET updated_at=? WHERE id=?", (utcnow(), int(group_id)))
    conn.commit()
    return len(keys)


def _filename_well_name(filename: str) -> str:
    name = Path(filename).stem
    name = re.sub(r"(?i)(?:_?LOGS?|_?TZ(?:_?3D)?|[-_]?WELLHEAD)$", "", name)
    return name.rstrip("_ ")


@lru_cache(maxsize=32)
def _petrel_well_top_names_cached(path_text: str, modified_ns: int) -> tuple[str, ...]:
    """Read well identifiers from one representative Petrel well-top table."""
    del modified_ns
    header: list[str] = []
    in_header = False
    well_index: int | None = None
    names: set[str] = set()
    with Path(path_text).open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.upper() == "BEGIN HEADER":
                in_header = True
                continue
            if line.upper() == "END HEADER":
                in_header = False
                well_index = next((index for index, name in enumerate(header) if name.strip().lower() == "well"), None)
                continue
            if in_header:
                header.append(line)
                continue
            if well_index is None:
                continue
            try:
                values = shlex.split(line, posix=True)
            except ValueError:
                continue
            if len(values) <= well_index:
                continue
            name = values[well_index].strip()
            if normalize_well_name(name):
                names.add(name)
    return tuple(sorted(names, key=str.upper))


def petrel_well_top_names(path: str | Path) -> list[str]:
    source = Path(path)
    try:
        return list(_petrel_well_top_names_cached(str(source), source.stat().st_mtime_ns))
    except OSError:
        return []


def _alias_map(conn: sqlite3.Connection, project_root: str) -> dict[str, tuple[str, str]]:
    return {
        row["alias_key"]: (row["canonical_key"], row["canonical_name"])
        for row in conn.execute("SELECT * FROM project_well_aliases WHERE project_root=?", (project_root,))
    }


def project_wells(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    aliases = _alias_map(conn, project_root)
    inventory: dict[str, dict[str, Any]] = {}

    def resolve(raw_name: str) -> tuple[str, str]:
        key = normalize_well_name(raw_name)
        seen: set[str] = set()
        display = raw_name
        while key in aliases and key not in seen:
            seen.add(key)
            key, display = aliases[key]
        return key, display

    def get(raw_name: str) -> dict[str, Any] | None:
        if not raw_name:
            return None
        key, display = resolve(raw_name)
        if not key:
            return None
        return inventory.setdefault(key, {
            "id": f"project:{key}", "project_key": key, "canonical_name": display or raw_name,
            "uwi": None, "x": None, "y": None, "crs": snapshot.get("wellheads", {}).get("crs"),
            "kb_elevation": None, "total_depth": None, "source_types": set(), "source_count": 0,
            "curve_names": set(), "curve_count": 0, "log_start_md": None, "log_stop_md": None,
            "max_survey_md": None, "preferred_type": None, "preferred_filename": None,
            "_sources": [], "_coordinates": [], "_dev_paths": [], "_las_paths": [],
        })

    head_path = snapshot.get("wellheads", {}).get("path")
    for head in snapshot.get("wellheads", {}).get("wells", []):
        row = get(head.get("name") or head.get("uwi"))
        if not row:
            continue
        row["canonical_name"] = aliases.get(normalize_well_name(head.get("name")), (None, head.get("name")))[1] or head.get("name")
        row["uwi"] = row["uwi"] or head.get("uwi")
        for field, target in (("x", "x"), ("y", "y"), ("kb", "kb_elevation"), ("td_md", "total_depth")):
            if row[target] is None and head.get(field) is not None:
                row[target] = head[field]
        if head.get("x") is not None and head.get("y") is not None:
            row["_coordinates"].append((head["x"], head["y"]))
        row["source_types"].add("well_head")
        row["_sources"].append({"raw_name": head.get("name"), "data_type": "well_head", "filename": Path(head_path).name if head_path else "Well Head", "file_path": head_path, "x": head.get("x"), "y": head.get("y"), "batch": "目录扫描", "version": None})

    catalog_rows = [dict(item) for item in conn.execute(
        """SELECT * FROM project_catalog_items WHERE project_root=?
           AND category_key IN ('well_logs','well_paths','checkshots','well_tops')""", (project_root,)
    )]
    for item in catalog_rows:
        if item["category_key"] == "well_tops":
            if not item["representative"] or item["extension"] in {".xml", ".prj", ".shx", ".dbf", ".sbn", ".sbx"}:
                continue
            for top_well_name in petrel_well_top_names(item["file_path"]):
                row = get(top_well_name)
                if not row:
                    continue
                row["source_types"].add("well_top")
                row["_sources"].append({"raw_name": top_well_name, "data_type": "well_top", "filename": item["filename"], "file_path": item["file_path"], "catalog_id": item["id"], "x": None, "y": None, "batch": "代表 Well Top", "version": None, "representative": True})
            continue
        raw_name = _filename_well_name(item["filename"])
        row = get(raw_name)
        if not row:
            continue
        data_type = SOURCE_TYPES[item["category_key"]]
        row["source_types"].add(data_type)
        path_key = "_dev_paths" if data_type == "deviation" else "_las_paths" if data_type == "las" else None
        if path_key:
            row[path_key].append(item["file_path"])
        row["_sources"].append({"raw_name": raw_name, "data_type": data_type, "filename": item["filename"], "file_path": item["file_path"], "catalog_id": item["id"], "x": None, "y": None, "batch": item["source_folder"], "version": None, "representative": bool(item["representative"])})

    for sample in snapshot.get("representative_analysis", {}).get("las_samples", []):
        row = get(sample.get("well") or _filename_well_name(sample.get("filename", "")))
        if not row:
            continue
        row["source_types"].add("las")
        row["curve_names"].update(name for name in (sample.get("curves") or []) if not is_time_depth_mnemonic(name))
        starts = [value for value in (row["log_start_md"], sample.get("start")) if value is not None]
        stops = [value for value in (row["log_stop_md"], sample.get("stop")) if value is not None]
        row["log_start_md"] = min(starts) if starts else None
        row["log_stop_md"] = max(stops) if stops else None

    for sample in snapshot.get("representative_analysis", {}).get("dev_samples", []):
        row = get(sample.get("well") or _filename_well_name(sample.get("filename", "")))
        if row:
            row["source_types"].add("deviation")
            if sample.get("max_md") is not None:
                row["max_survey_md"] = max(row["max_survey_md"] or 0, sample["max_md"])

    # Structured production exports are hard evidence that a well exists. Keep
    # unmatched OFM names as separate wells until an alias is confirmed; exact
    # and saved-alias keys join the scanned Well Head/LAS/DEV identity here.
    ofm_wells = [dict(item) for item in conn.execute(
        """SELECT ow.*,s.filename,s.file_path,s.batch,s.version
           FROM ofm_wells ow JOIN sources s ON s.id=ow.source_id
           WHERE s.status='ready' ORDER BY ow.well_name"""
    )]
    for production in ofm_wells:
        row = get(production["well_name"])
        if not row:
            continue
        row["source_types"].add("production")
        if row["x"] is None and production.get("x") is not None:
            row["x"] = production["x"]
        if row["y"] is None and production.get("y") is not None:
            row["y"] = production["y"]
        if production.get("x") is not None and production.get("y") is not None:
            row["_coordinates"].append((production["x"], production["y"]))
        if row["kb_elevation"] is None:
            row["kb_elevation"] = production.get("kb_elevation")
        if row["total_depth"] is None:
            row["total_depth"] = production.get("total_depth")
        row["_sources"].append({
            "raw_name": production["well_name"], "data_type": "production",
            "filename": production["filename"], "file_path": production["file_path"],
            "x": production.get("x"), "y": production.get("y"),
            "batch": production["batch"], "version": production["version"],
        })
    production_rows = [dict(item) for item in conn.execute(
        """SELECT p.well_key,p.well_name,s.filename,s.file_path,s.batch,s.version
           FROM (
             SELECT source_id,well_key,well_name FROM production_intervals
             UNION
             SELECT source_id,well_key,well_name FROM production_monthly
             UNION
             SELECT source_id,well_key,well_name FROM production_events
           ) p JOIN sources s ON s.id=p.source_id
           WHERE s.status='ready'
           GROUP BY p.well_key,p.well_name,s.id
           ORDER BY p.well_name"""
    )]
    for production in production_rows:
        row = get(production["well_name"])
        if not row:
            continue
        row["source_types"].add("production")
        row["_sources"].append({
            "raw_name": production["well_name"], "data_type": "production",
            "filename": production["filename"], "file_path": production["file_path"],
            "x": None, "y": None, "batch": production["batch"], "version": production["version"],
        })

    result: list[dict[str, Any]] = []
    for row in inventory.values():
        row["source_types"] = sorted(row["source_types"])
        row["source_count"] = len({source.get("file_path") or source["filename"] for source in row["_sources"]})
        row["curve_names"] = sorted(row["curve_names"])
        row["curve_count"] = len(row["curve_names"])
        source_set = set(row["source_types"])
        has_head = "well_head" in source_set
        has_logs, has_dev = "las" in source_set, "deviation" in source_set
        coordinate_spread = None
        if len(row["_coordinates"]) > 1:
            xs = [point[0] for point in row["_coordinates"]]
            ys = [point[1] for point in row["_coordinates"]]
            coordinate_spread = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        row["coordinate_spread"] = coordinate_spread
        row.update(classify_well_identity(row["source_types"], coordinate_spread))
        row["preferred_type"] = "well_head" if has_head else "las" if has_logs else "deviation" if has_dev else "production" if "production" in source_set else (row["source_types"][0] if row["source_types"] else None)
        preferred = next((source for source in row["_sources"] if source["data_type"] == row["preferred_type"]), None)
        row["preferred_filename"] = preferred["filename"] if preferred else None
        row["has_trajectory"] = bool(row["_dev_paths"])
        result.append(row)
    return sorted(result, key=lambda row: row["canonical_name"].upper())


def project_well_public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def project_well_detail(conn: sqlite3.Connection, snapshot: dict[str, Any], key: str) -> dict[str, Any] | None:
    row = next((item for item in project_wells(conn, snapshot) if item["project_key"] == key), None)
    if not row:
        return None
    coordinate_spread = None
    if len(row["_coordinates"]) > 1:
        coordinate_spread = max(math.hypot(a[0] - b[0], a[1] - b[1]) for index, a in enumerate(row["_coordinates"]) for b in row["_coordinates"][index + 1:])
    curves = [{"mnemonic": name, "unit": None, "description": "代表 LAS 样本", "sample_count": None, "value_min": None, "value_max": None, "start_md": row["log_start_md"], "stop_md": row["log_stop_md"], "filename": row["preferred_filename"], "batch": "代表性扫描", "version": None} for name in row["curve_names"]]
    well = project_well_public(row)
    well["created_at"] = snapshot["project"].get("scanned_at")
    return {"well": well, "sources": row["_sources"], "curves": curves, "interpretations": [], "coordinate_spread": coordinate_spread, "dev_paths": row["_dev_paths"], "las_paths": row["_las_paths"], "sampling_note": "曲线明细来自每个文件夹的一份代表 LAS；文件关联来自完整目录索引。"}


def save_well_alias(conn: sqlite3.Connection, snapshot: dict[str, Any], alias_key: str, canonical_key: str, canonical_name: str) -> None:
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    if alias_key == canonical_key:
        raise ValueError("两口井已经是同一标准键")
    conn.execute(
        """INSERT INTO project_well_aliases(project_root,alias_key,canonical_key,canonical_name,created_at)
           VALUES(?,?,?,?,?) ON CONFLICT(project_root,alias_key) DO UPDATE SET
           canonical_key=excluded.canonical_key,canonical_name=excluded.canonical_name,created_at=excluded.created_at""",
        (project_root, alias_key, canonical_key, canonical_name, utcnow()),
    )
    conn.commit()


def project_identity_suggestions(conn: sqlite3.Connection, snapshot: dict[str, Any], limit: int = 150) -> list[dict[str, Any]]:
    rows = project_wells(conn, snapshot)
    buckets: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        key = row["project_key"]
        digits = "-".join(re.findall(r"\d+", key))
        consonants = re.sub(r"[AEIOU]", "", key)
        if digits:
            buckets[f"D:{digits}"].append(index)
        if len(consonants) >= 3:
            buckets[f"V:{consonants}"].append(index)
        if row["x"] is not None and row["y"] is not None:
            buckets[f"C:{round(row['x'])}:{round(row['y'])}"].append(index)
    pairs: set[tuple[int, int]] = set()
    for indexes in buckets.values():
        if len(indexes) <= 100:
            for position, left in enumerate(indexes):
                for right in indexes[position + 1:]:
                    pairs.add((min(left, right), max(left, right)))
    suggestions: list[dict[str, Any]] = []
    for left_index, right_index in pairs:
        left, right = rows[left_index], rows[right_index]
        score = identity_similarity(left["canonical_name"], right["canonical_name"])
        reasons: list[str] = []
        left_vowels = re.sub(r"[AEIOU]", "", left["project_key"])
        right_vowels = re.sub(r"[AEIOU]", "", right["project_key"])
        if left_vowels == right_vowels and left_vowels:
            score = max(score, 0.86)
            reasons.append("省略元音后名称一致")
        left_digits = re.findall(r"\d+", left["project_key"])
        right_digits = re.findall(r"\d+", right["project_key"])
        if left_digits and left_digits == right_digits:
            score = max(score, 0.78)
            reasons.append("井号数字一致")
        distance = None
        if left["x"] is not None and right["x"] is not None and left["y"] is not None and right["y"] is not None:
            distance = math.hypot(left["x"] - right["x"], left["y"] - right["y"])
            if distance <= 1:
                score = max(score, 0.98)
                reasons.append(f"井头坐标相同（{distance:.2f} m）")
            elif distance <= 5:
                score = max(score, 0.92)
                reasons.append(f"井头相距 {distance:.2f} m")
        if score >= 0.76:
            suggestions.append({
                "left": project_well_public(left), "right": project_well_public(right),
                "score": round(score, 3), "reasons": reasons or ["井名字符相似"],
                "coordinate_distance": round(distance, 3) if distance is not None else None,
                "can_compare_trajectory": bool(left["_dev_paths"] and right["_dev_paths"]),
            })
    return sorted(suggestions, key=lambda row: (row["score"], row["can_compare_trajectory"]), reverse=True)[:limit]


def parse_dev_stations(path: str | Path) -> list[dict[str, float]]:
    stations: list[dict[str, float]] = []
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or not re.match(r"^[+-]?\d", line):
                continue
            values = line.split()
            if len(values) < 5:
                continue
            try:
                stations.append({"md": float(values[0]), "x": float(values[1]), "y": float(values[2]), "z": float(values[3]), "tvd": float(values[4])})
            except ValueError:
                continue
    return sorted(stations, key=lambda row: row["md"])


def _interpolate(stations: list[dict[str, float]], md: float) -> tuple[float, float, float] | None:
    if not stations or md < stations[0]["md"] or md > stations[-1]["md"]:
        return None
    right_index = next((index for index, row in enumerate(stations) if row["md"] >= md), len(stations) - 1)
    if right_index == 0:
        row = stations[0]
        return row["x"], row["y"], row["z"]
    left, right = stations[right_index - 1], stations[right_index]
    fraction = 0 if right["md"] == left["md"] else (md - left["md"]) / (right["md"] - left["md"])
    return tuple(left[key] + (right[key] - left[key]) * fraction for key in ("x", "y", "z"))


def compare_project_trajectories(conn: sqlite3.Connection, snapshot: dict[str, Any], left_key: str, right_key: str) -> dict[str, Any]:
    rows = {row["project_key"]: row for row in project_wells(conn, snapshot)}
    left, right = rows.get(left_key), rows.get(right_key)
    if not left or not right or not left["_dev_paths"] or not right["_dev_paths"]:
        raise ValueError("两口候选井都需要至少一份 DEV 轨迹")
    left_path, right_path = left["_dev_paths"][0], right["_dev_paths"][0]
    left_stations, right_stations = parse_dev_stations(left_path), parse_dev_stations(right_path)
    if len(left_stations) < 2 or len(right_stations) < 2:
        raise ValueError("DEV 轨迹有效测点不足")
    start = max(left_stations[0]["md"], right_stations[0]["md"])
    stop = min(left_stations[-1]["md"], right_stations[-1]["md"])
    if stop <= start:
        raise ValueError("两份 DEV 没有共同 MD 深度段")
    sample_mds = [start + (stop - start) * index / 100 for index in range(101)]
    distances = []
    for md in sample_mds:
        a, b = _interpolate(left_stations, md), _interpolate(right_stations, md)
        if a and b:
            distances.append(math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3))))
    mean_distance = sum(distances) / len(distances)
    max_distance = max(distances)
    # Do not call two trajectories "fully identical" merely because 101
    # interpolated samples happen to agree.  Strong matching means every
    # measured station has the same MD/X/Y/Z/TVD values (within numeric text
    # precision), while the sampled distances still explain near-matches.
    stationwise_identical = len(left_stations) == len(right_stations) and all(
        abs(float(left_station[field]) - float(right_station[field])) <= 1e-6
        for left_station, right_station in zip(left_stations, right_stations)
        for field in ("md", "x", "y", "z", "tvd")
    )
    conclusion = "轨迹完全一致" if stationwise_identical else "轨迹高度一致" if max_distance <= 2 else "轨迹近似，建议人工复核" if max_distance <= 10 else "轨迹差异明显"
    return {
        "left_name": left["canonical_name"], "right_name": right["canonical_name"],
        "left_path": left_path, "right_path": right_path,
        "left_station_count": len(left_stations), "right_station_count": len(right_stations),
        "common_md_start": round(start, 3), "common_md_stop": round(stop, 3),
        "mean_3d_distance": round(mean_distance, 4), "max_3d_distance": round(max_distance, 4),
        "stationwise_identical": stationwise_identical,
        "conclusion": conclusion,
    }
