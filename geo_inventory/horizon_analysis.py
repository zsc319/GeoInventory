from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .db import utcnow
from .global_filter import compute_surface_horizon_hits
from .importers import normalize_well_name
from .project_catalog import parse_dev_stations
from .project_scan import parse_petrel_surface
from .relationship_search import parse_well_tops


AUXILIARY_EXTENSIONS = {".xml", ".prj", ".shx", ".dbf", ".sbn", ".sbx"}
HARD_WELL_SOURCES = {"well_head", "deviation", "las", "production"}


def layer_key(value: str) -> str:
    stem = Path(str(value or "")).stem.upper()
    stem = re.sub(r"(?:[_\- ]+(?:V|VER|REV)\d+)$", "", stem)
    stem = re.sub(r"(?:[_\- ]+20\d{2})$", "", stem)
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", stem)


def _horizon_name_registry(conn: sqlite3.Connection, project_root: str) -> dict[str, Any]:
    """Load the per-project canonical-layer vocabulary without touching source files."""
    groups: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """SELECT canonical_key,canonical_name,created_at,updated_at
           FROM horizon_name_groups WHERE project_root=? ORDER BY canonical_name COLLATE NOCASE""",
        (project_root,),
    ):
        item = dict(row)
        item["aliases"] = []
        groups[item["canonical_key"]] = item
    aliases: dict[str, str] = {}
    for row in conn.execute(
        """SELECT alias_key,alias_name,canonical_key,created_at
           FROM horizon_name_aliases WHERE project_root=? ORDER BY alias_name COLLATE NOCASE""",
        (project_root,),
    ):
        item = dict(row)
        aliases[item["alias_key"]] = item["canonical_key"]
        if item["canonical_key"] in groups:
            groups[item["canonical_key"]]["aliases"].append(item)
    return {"groups": groups, "aliases": aliases}


def _canonical_layer(key: str, registry: dict[str, Any]) -> tuple[str, str | None, bool]:
    canonical_key = registry["aliases"].get(key, key)
    group = registry["groups"].get(canonical_key)
    return canonical_key, (group or {}).get("canonical_name"), bool(group)


def create_horizon_name_group(conn: sqlite3.Connection, project_root: str, name: str) -> dict[str, Any]:
    canonical_name = str(name or "").strip()
    canonical_key = layer_key(canonical_name)
    if not canonical_name or not canonical_key:
        raise ValueError("请输入可识别的统一层位名称")
    now = utcnow()
    conn.execute(
        """INSERT INTO horizon_name_groups(project_root,canonical_key,canonical_name,created_at,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(project_root,canonical_key) DO UPDATE SET
             canonical_name=excluded.canonical_name,updated_at=excluded.updated_at""",
        (project_root, canonical_key, canonical_name, now, now),
    )
    conn.commit()
    return {"canonical_key": canonical_key, "canonical_name": canonical_name}


def assign_horizon_name_aliases(
    conn: sqlite3.Connection, project_root: str, canonical_key: str, aliases: Iterable[dict[str, Any] | str],
) -> dict[str, Any]:
    canonical_key = layer_key(canonical_key)
    exists = conn.execute(
        "SELECT 1 FROM horizon_name_groups WHERE project_root=? AND canonical_key=?", (project_root, canonical_key)
    ).fetchone()
    if not exists:
        raise ValueError("请先新建统一层位名称")
    saved: list[str] = []
    now = utcnow()
    for entry in aliases:
        source = entry if isinstance(entry, dict) else {"name": entry}
        alias_name = str(source.get("name") or source.get("label") or source.get("key") or "").strip()
        alias_key = layer_key(str(source.get("key") or alias_name))
        if not alias_key:
            continue
        # A raw name identical to the canonical key resolves naturally; it does
        # not need a redundant alias row and should remain removable by renaming.
        if alias_key == canonical_key:
            conn.execute(
                "DELETE FROM horizon_name_aliases WHERE project_root=? AND alias_key=?", (project_root, alias_key)
            )
            continue
        conn.execute(
            """INSERT INTO horizon_name_aliases(project_root,alias_key,alias_name,canonical_key,created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(project_root,alias_key) DO UPDATE SET
                 alias_name=excluded.alias_name,canonical_key=excluded.canonical_key,created_at=excluded.created_at""",
            (project_root, alias_key, alias_name or alias_key, canonical_key, now),
        )
        saved.append(alias_key)
    conn.commit()
    return {"canonical_key": canonical_key, "assigned": saved}


def remove_horizon_name_alias(conn: sqlite3.Connection, project_root: str, alias_key: str) -> None:
    conn.execute(
        "DELETE FROM horizon_name_aliases WHERE project_root=? AND alias_key=?", (project_root, layer_key(alias_key))
    )
    conn.commit()


def remove_horizon_name_group(conn: sqlite3.Connection, project_root: str, canonical_key: str) -> None:
    conn.execute(
        "DELETE FROM horizon_name_groups WHERE project_root=? AND canonical_key=?",
        (project_root, layer_key(canonical_key)),
    )
    conn.commit()


def _base_label(value: str) -> str:
    stem = Path(value).stem
    stem = re.sub(r"(?i)(?:[_\- ]+(?:V|VER|REV)\d+)$", "", stem)
    stem = re.sub(r"(?:[_\- ]+20\d{2})$", "", stem)
    return stem.strip(" _-") or Path(value).stem


def _number(value: str) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _sample_fingerprint(path: Path, size: int) -> tuple[str | None, str]:
    digest = hashlib.sha1()
    try:
        with path.open("rb") as handle:
            if size <= 2 * 1024 * 1024:
                for block in iter(lambda: handle.read(131072), b""):
                    digest.update(block)
                return digest.hexdigest(), "full"
            digest.update(handle.read(131072))
            handle.seek(max(0, size - 131072))
            digest.update(handle.read(131072))
            digest.update(str(size).encode("ascii"))
            return digest.hexdigest(), "head_tail"
    except OSError:
        return None, "unavailable"


def _xyz_preview(text: str, limit: int = 4000) -> dict[str, Any]:
    points: list[tuple[float, float, float]] = []
    for raw_line in text.splitlines():
        point = _xyz_triplet(raw_line)
        if point is None:
            continue
        points.append(point)
        if len(points) >= limit:
            break
    if not points:
        return {"point_sample_count": 0, "bounds": None, "value_min": None, "value_max": None}
    xs, ys, zs = [row[0] for row in points], [row[1] for row in points], [row[2] for row in points]
    return {
        "point_sample_count": len(points),
        "bounds": {"x_min": min(xs), "x_max": max(xs), "y_min": min(ys), "y_max": max(ys)},
        "value_min": min(zs), "value_max": max(zs),
    }


def _xyz_triplet(raw_line: str) -> tuple[float, float, float] | None:
    line = raw_line.strip()
    if not line or line.startswith(("#", "!", "@", "->", "FS", "PROFILE", "SNAPPING", "FF")):
        return None
    values = line.replace(",", " ").split()
    numbers = [_number(value) for value in values[:8]]
    numeric = [value for value in numbers if value is not None]
    if len(numeric) < 3:
        return None
    x, y, z = numeric[0], numeric[1], numeric[2]
    if abs(x) < 1000 and len(numeric) >= 5 and abs(numeric[2]) > 10000 and abs(numeric[3]) > 10000:
        x, y, z = numeric[2], numeric[3], numeric[4]
    return x, y, z


@lru_cache(maxsize=256)
def _surface_metadata_cached(path_text: str, modified_ns: int, size: int) -> dict[str, Any]:
    del modified_ns
    path = Path(path_text)
    fingerprint, fingerprint_mode = _sample_fingerprint(path, size)
    try:
        with path.open("rb") as handle:
            prefix = handle.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return {
            "format": "不可访问", "kind": "unknown", "computable": False,
            "fingerprint": fingerprint, "fingerprint_mode": fingerprint_mode,
        }
    upper = prefix.upper()
    if "FSASCI" in upper and "FSNROW" in upper:
        surface = parse_petrel_surface(path, scan_values=False)
        return {
            "format": surface.get("format") or "Petrel FSASCI surface", "kind": "regular_surface",
            "rows": surface.get("rows"), "columns": surface.get("columns"),
            "x_increment": surface.get("x_increment"), "y_increment": surface.get("y_increment"),
            "bounds": surface.get("bounds"), "value_min": (surface.get("bounds") or {}).get("z_min"),
            "value_max": (surface.get("bounds") or {}).get("z_max"), "point_sample_count": 0,
            "metadata_scope": "完整头信息；未读取全部网格值", "computable": True,
            "fingerprint": fingerprint, "fingerprint_mode": fingerprint_mode,
        }
    xyz = _xyz_preview(prefix)
    is_zmap = "ZMAP" in upper or path.suffix.lower() in {".zmap", ".zmap+"}
    if is_zmap:
        kind = "xyz_surface" if xyz["point_sample_count"] else "zmap_grid"
        label = "ZMAP+ XYZ surface" if kind == "xyz_surface" else "ZMAP+ grid"
    elif "PROFILE" in upper:
        kind, label = "2d_profile", "Petrel 2D profile"
    elif xyz["point_sample_count"]:
        kind, label = "xyz_surface", "XYZ point surface"
    else:
        kind, label = "catalog_horizon", "层位目录对象"
    return {
        "format": label, "kind": kind, "computable": kind == "xyz_surface",
        **xyz, "metadata_scope": "文件前 2 MB 坐标抽样；范围仅供初筛",
        "fingerprint": fingerprint, "fingerprint_mode": fingerprint_mode,
    }


def surface_metadata(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        stat = source.stat()
        return dict(_surface_metadata_cached(str(source), stat.st_mtime_ns, stat.st_size))
    except OSError:
        return {"format": "不可访问", "kind": "unknown", "computable": False}


def _aliases(conn: sqlite3.Connection, project_root: str) -> dict[str, str]:
    return {
        row["alias_key"]: row["canonical_key"]
        for row in conn.execute(
            "SELECT alias_key,canonical_key FROM project_well_aliases WHERE project_root=?", (project_root,)
        )
    }


def _resolve_key(value: str, aliases: dict[str, str]) -> str:
    key = normalize_well_name(value)
    seen: set[str] = set()
    while key in aliases and key not in seen:
        seen.add(key)
        key = aliases[key]
    return key


def _hard_wells(wells: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in wells if set(row.get("source_types") or []) & HARD_WELL_SOURCES]


def _top_items(conn: sqlite3.Connection, project_root: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            """SELECT id,filename,file_path,relative_path,source_folder,extension,bytes,modified_at,representative
               FROM project_catalog_items WHERE project_root=? AND category_key='well_tops'
               ORDER BY modified_at DESC,filename COLLATE NOCASE""",
            (project_root,),
        ) if Path(row["filename"]).suffix.lower() not in AUXILIARY_EXTENSIONS
    ]


def _top_rows(conn: sqlite3.Connection, project_root: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    aliases = _aliases(conn, project_root)
    items = _top_items(conn, project_root)
    rows: list[dict[str, Any]] = []
    for item in items:
        version_label = Path(item["filename"]).stem
        for row in parse_well_tops(item["file_path"]):
            row.update({
                "well_key": _resolve_key(row["well"], aliases), "item_id": item["id"],
                "source_file": item["filename"], "relative_path": item["relative_path"],
                "source_modified": item["modified_at"], "version_label": version_label,
            })
            rows.append(row)
    return items, rows


def _horizon_key(snapshot: dict[str, Any], item: dict[str, Any]) -> str:
    snapshot_path = (snapshot.get("surface") or {}).get("path")
    try:
        if snapshot_path and Path(snapshot_path).resolve() == Path(item["file_path"]).resolve():
            return f"surface:{Path(item['file_path']).name}"
    except OSError:
        pass
    return f"surface-catalog:{item['id']}"


def _comparison(base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    left, right = base["metadata"], other["metadata"]
    same_fingerprint = bool(left.get("fingerprint") and left.get("fingerprint") == right.get("fingerprint"))
    bounds_delta = None
    if left.get("bounds") and right.get("bounds"):
        keys = ("x_min", "x_max", "y_min", "y_max")
        values = [abs(float(left["bounds"].get(key, 0)) - float(right["bounds"].get(key, 0))) for key in keys]
        bounds_delta = max(values)
    z_delta = None
    if None not in (left.get("value_min"), left.get("value_max"), right.get("value_min"), right.get("value_max")):
        z_delta = max(abs(left["value_min"] - right["value_min"]), abs(left["value_max"] - right["value_max"]))
    grid_changed = (left.get("rows"), left.get("columns"), left.get("x_increment"), left.get("y_increment")) != (
        right.get("rows"), right.get("columns"), right.get("x_increment"), right.get("y_increment")
    )
    if same_fingerprint:
        status = "完整内容一致" if left.get("fingerprint_mode") == right.get("fingerprint_mode") == "full" else "首尾抽样指纹一致"
    elif grid_changed or (bounds_delta is not None and bounds_delta > 1e-6) or (z_delta is not None and z_delta > 1e-6):
        status = "范围、网格或深度域不同"
    else:
        status = "命名相同，需进一步逐点复核"
    return {
        "base_id": base["id"], "other_id": other["id"], "status": status,
        "same_fingerprint": same_fingerprint, "bounds_max_delta": bounds_delta,
        "depth_range_max_delta": z_delta, "grid_changed": grid_changed,
    }


def horizon_workbench(
    conn: sqlite3.Connection, snapshot: dict[str, Any], wells: list[dict[str, Any]],
) -> dict[str, Any]:
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    naming_registry = _horizon_name_registry(conn, project_root)
    hard_wells = _hard_wells(wells)
    hard_keys = {row["project_key"] for row in hard_wells}
    top_items, all_tops = _top_rows(conn, project_root)
    top_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_tops:
        top_groups[row["surface_key"]].append(row)
    merged_top_groups: dict[str, dict[str, Any]] = {}
    for raw_key, rows in top_groups.items():
        canonical_key, canonical_name, is_custom = _canonical_layer(raw_key, naming_registry)
        group = merged_top_groups.setdefault(canonical_key, {
            "rows": [], "raw_keys": set(), "raw_names": Counter(), "is_custom": is_custom,
            "canonical_name": canonical_name,
        })
        group["rows"].extend(rows)
        group["raw_keys"].add(raw_key)
        group["raw_names"].update(row["surface"] for row in rows)
        group["is_custom"] = group["is_custom"] or is_custom
        group["canonical_name"] = group["canonical_name"] or canonical_name
    top_layers = []
    for key, group in merged_top_groups.items():
        rows = group["rows"]
        names = group["raw_names"]
        layer_wells = {row["well_key"] for row in rows} & hard_keys
        versions = []
        for item_id, version_rows in _group_by(rows, "item_id"):
            sample = version_rows[0]
            version_wells = {row["well_key"] for row in version_rows} & hard_keys
            versions.append({
                "item_id": item_id, "file": sample["source_file"], "relative_path": sample["relative_path"],
                "records": len(version_rows), "well_count": len(version_wells), "modified_at": sample["source_modified"],
            })
        mds = [row["md"] for row in rows if row.get("md") is not None]
        raw_names = [name for name, _count in names.most_common()]
        top_layers.append({
            "key": key, "name": group["canonical_name"] or names.most_common(1)[0][0],
            "unified_name": group["canonical_name"] or names.most_common(1)[0][0],
            "is_unified": group["is_custom"], "raw_keys": sorted(group["raw_keys"]),
            "raw_names": raw_names, "record_count": len(rows),
            "well_count": len(layer_wells), "coverage_percentage": round(len(layer_wells) / max(1, len(hard_keys)) * 100, 2),
            "missing_count": max(0, len(hard_keys) - len(layer_wells)), "version_count": len(versions),
            "versions": sorted(versions, key=lambda row: str(row.get("modified_at") or ""), reverse=True),
            "md_min": min(mds) if mds else None, "md_max": max(mds) if mds else None,
            "unmatched_top_wells": len({row["well_key"] for row in rows} - hard_keys),
        })
    top_layers.sort(key=lambda row: (-row["well_count"], row["name"]))

    structural_items = [
        dict(row) for row in conn.execute(
            """SELECT id,filename,file_path,relative_path,extension,bytes,modified_at,representative
               FROM project_catalog_items WHERE project_root=? AND category_key='horizons'
               ORDER BY representative DESC,filename COLLATE NOCASE""",
            (project_root,),
        ) if Path(row["filename"]).suffix.lower() not in AUXILIARY_EXTENSIONS
    ]
    hit_counts = {
        row["horizon_key"]: dict(row) for row in conn.execute(
            """SELECT horizon_key,COUNT(*) computed_wells,SUM(hit) hit_wells,MAX(computed_at) computed_at
               FROM horizon_well_hits WHERE project_root=? GROUP BY horizon_key""", (project_root,)
        )
    }
    structural_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in structural_items:
        metadata = surface_metadata(item["file_path"])
        item.update({
            "key": layer_key(item["filename"]), "base_name": _base_label(item["filename"]),
            "horizon_key": _horizon_key(snapshot, item), "metadata": metadata,
        })
        item.update(hit_counts.get(item["horizon_key"], {"computed_wells": 0, "hit_wells": 0, "computed_at": None}))
        canonical_key, _canonical_name, _is_custom = _canonical_layer(item["key"], naming_registry)
        item["raw_key"] = item["key"]
        item["key"] = canonical_key
        structural_groups[canonical_key].append(item)
    structural = []
    for key, versions in structural_groups.items():
        versions.sort(key=lambda row: (not row["representative"], row["filename"], row["relative_path"]))
        base = versions[0]
        comparisons = [_comparison(base, row) for row in versions[1:]]
        kinds = Counter(row["metadata"].get("kind") or "unknown" for row in versions)
        _canonical_key, canonical_name, is_custom = _canonical_layer(key, naming_registry)
        hit_well_keys: set[str] = set()
        horizon_keys = [str(row["horizon_key"]) for row in versions]
        if horizon_keys:
            marks = ",".join("?" for _ in horizon_keys)
            hit_well_keys = {
                str(row["well_key"]) for row in conn.execute(
                    f"SELECT DISTINCT well_key FROM horizon_well_hits WHERE project_root=? AND hit=1 AND horizon_key IN ({marks})",
                    [project_root, *horizon_keys],
                )
            }
        raw_names = Counter(row["base_name"] for row in versions)
        structural.append({
            "key": key, "name": canonical_name or raw_names.most_common(1)[0][0],
            "unified_name": canonical_name or raw_names.most_common(1)[0][0],
            "is_unified": is_custom, "raw_keys": sorted({row["raw_key"] for row in versions}),
            "raw_names": [name for name, _count in raw_names.most_common()],
            "version_count": len(versions), "kind": kinds.most_common(1)[0][0],
            "versions": versions, "comparisons": comparisons,
            "computed_versions": sum(bool(row.get("computed_wells")) for row in versions),
            "hit_wells": len(hit_well_keys),
        })
    structural.sort(key=lambda row: (row["kind"] != "regular_surface", row["name"]))
    detected_names: dict[str, dict[str, Any]] = {}

    def remember_name(raw_key: str, raw_name: str, source: str, count: int) -> None:
        item = detected_names.setdefault(raw_key, {
            "key": raw_key, "names": Counter(), "well_top_records": 0, "structural_files": 0,
        })
        item["names"][raw_name] += max(1, count)
        if source == "well_top":
            item["well_top_records"] += count
        else:
            item["structural_files"] += count

    for raw_key, rows in top_groups.items():
        names = Counter(row["surface"] for row in rows)
        for raw_name, count in names.items():
            remember_name(raw_key, raw_name, "well_top", count)
    for group in structural:
        for version in group["versions"]:
            remember_name(version["raw_key"], version["base_name"], "surface", 1)
    naming_groups = []
    for group in naming_registry["groups"].values():
        aliases = sorted(group["aliases"], key=lambda row: str(row["alias_name"]).upper())
        naming_groups.append({
            "key": group["canonical_key"], "name": group["canonical_name"], "aliases": aliases,
            "alias_count": len(aliases), "created_at": group["created_at"],
        })
    naming_groups.sort(key=lambda row: row["name"].upper())
    detected = []
    for item in detected_names.values():
        raw_key = item["key"]
        canonical_key, canonical_name, is_unified = _canonical_layer(raw_key, naming_registry)
        detected.append({
            "key": raw_key, "name": item["names"].most_common(1)[0][0],
            "well_top_records": item["well_top_records"], "structural_files": item["structural_files"],
            "canonical_key": canonical_key if is_unified else None,
            "canonical_name": canonical_name, "is_unified": is_unified,
        })
    detected.sort(key=lambda row: row["name"].upper())
    return {
        "summary": {
            "unified_wells": len(hard_keys), "well_top_files": len(top_items),
            "well_top_records": len(all_tops), "well_top_layers": len(top_layers),
            "structural_files": len(structural_items), "structural_names": len(structural),
            "computed_surface_versions": sum(row["computed_versions"] for row in structural),
        },
        "well_top": {"layers": top_layers, "files": top_items},
        "structural": {"groups": structural},
        "naming": {"groups": naming_groups, "detected": detected},
        "method": {
            "well_top": "统一井分母只采用 Well Head、LAS、DEV、生产动态硬证据；同一层位出现在几个主文件中即记为几个分层方案版本。",
            "surface": "规则 PTD 读取完整头信息但不加载全部网格；ZMAP+/XYZ 和 2D 层位先读前 2 MB 坐标样本作版本初筛。主动计算 XYZ 点面时才建立完整空间桶索引。",
            "intersection": "构造面钻遇使用 DEV 的 XYZ/Z 轨迹与规则网格或 XYZ/ZMAP+ 点面相交并插值到 MD；仅有井口时会明确标为垂向近似。",
        },
    }


def _group_by(rows: list[dict[str, Any]], key: str) -> list[tuple[Any, list[dict[str, Any]]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return list(grouped.items())


def _production_intervals(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute("SELECT well_key,top_md,base_md,interval_name,status,event_date FROM production_intervals"):
        grouped[row["well_key"]].append(dict(row))
    return grouped


def horizon_detail(
    conn: sqlite3.Connection, snapshot: dict[str, Any], wells: list[dict[str, Any]], kind: str, key: str,
) -> dict[str, Any]:
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    naming_registry = _horizon_name_registry(conn, project_root)
    hard_wells = _hard_wells(wells)
    well_map = {row["project_key"]: row for row in hard_wells}
    _, all_tops = _top_rows(conn, project_root)
    tops_by_well: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_tops:
        tops_by_well[row["well_key"]].append(row)
    intersections: dict[str, dict[str, Any]] = {}
    versions: list[dict[str, Any]] = []
    name = key
    if kind == "well_top":
        target = [
            row for row in all_tops
            if _canonical_layer(row["surface_key"], naming_registry)[0] == key
        ]
        if not target:
            raise ValueError("没有找到该 Well Top 层位")
        _canonical_key, canonical_name, _is_unified = _canonical_layer(key, naming_registry)
        name = canonical_name or Counter(row["surface"] for row in target).most_common(1)[0][0]
        for item_id, rows in _group_by(target, "item_id"):
            sample = rows[0]
            versions.append({"id": item_id, "file": sample["source_file"], "relative_path": sample["relative_path"], "records": len(rows), "wells": len({row["well_key"] for row in rows})})
        for well_key, rows in _group_by(target, "well_key"):
            rows.sort(key=lambda row: str(row.get("source_modified") or ""), reverse=True)
            chosen = rows[0]
            intersections[well_key] = {"intersection_md": chosen.get("md"), "surface_z": chosen.get("z"), "method": "Well Top 井上分层点", "record_count": len(rows)}
    elif kind == "surface":
        rows = [dict(row) for row in conn.execute(
            "SELECT well_key,intersection_md,surface_z,method,computed_at FROM horizon_well_hits WHERE project_root=? AND horizon_key=? AND hit=1",
            (project_root, key),
        )]
        intersections = {row["well_key"]: row for row in rows}
        catalog_id = int(key.split(":", 1)[1]) if key.startswith("surface-catalog:") else None
        item = conn.execute(
            "SELECT id,filename,relative_path,file_path FROM project_catalog_items WHERE project_root=? AND " + ("id=?" if catalog_id is not None else "file_path=?"),
            (project_root, catalog_id if catalog_id is not None else (snapshot.get("surface") or {}).get("path")),
        ).fetchone()
        if item:
            name = _base_label(item["filename"])
            versions = [{"id": item["id"], "file": item["filename"], "relative_path": item["relative_path"], "metadata": surface_metadata(item["file_path"])}]
    else:
        raise ValueError("未知层位类型")
    intervals = _production_intervals(conn)
    rows_out = []
    for well_key, hit in intersections.items():
        well = well_map.get(well_key)
        if not well:
            continue
        md = hit.get("intersection_md")
        perforations = intervals.get(well_key, [])
        perforation_length = sum(max(0.0, float(row["base_md"]) - float(row["top_md"])) for row in perforations if row.get("top_md") is not None and row.get("base_md") is not None)
        deeper = sorted({
            _canonical_layer(row["surface_key"], naming_registry)[1] or row["surface"]
            for row in tops_by_well.get(well_key, [])
            if md is not None and row.get("md") is not None and row["md"] > md + .01
        })
        max_md = well.get("max_survey_md") or well.get("total_depth") or well.get("log_stop_md")
        rows_out.append({
            "well_key": well_key, "well_id": well.get("id"), "well_name": well["canonical_name"],
            "x": well.get("x"), "y": well.get("y"), "intersection_md": md, "surface_z": hit.get("surface_z"),
            "method": hit.get("method"), "perforation_count": len(perforations), "perforation_length": round(perforation_length, 3),
            "deeper_layers": deeper[:12], "drilled_below": bool(md is not None and max_md is not None and max_md > md + .01),
            "drilled_below_md": round(max(0.0, max_md - md), 3) if md is not None and max_md is not None else None,
            "curve_count": len(well.get("curve_names") or []), "curves": well.get("curve_names") or [],
            "has_las": "las" in set(well.get("source_types") or []), "has_dev": bool(well.get("_dev_paths")),
        })
    rows_out.sort(key=lambda row: row["well_name"].upper())
    hit_keys = set(intersections) & set(well_map)
    missing = [
        {"well_key": row["project_key"], "well_name": row["canonical_name"], "x": row.get("x"), "y": row.get("y")}
        for row in hard_wells if row["project_key"] not in hit_keys
    ]
    return {
        "kind": kind, "key": key, "name": name, "well_count": len(rows_out),
        "unified_wells": len(hard_wells), "coverage_percentage": round(len(rows_out) / max(1, len(hard_wells)) * 100, 2),
        "versions": versions, "wells": rows_out, "missing_wells": missing,
        "method": "Well Top 表示井上已划分层位；构造面表示 DEV 轨迹与空间面的交会。钻遇 MD 以下的 DEV 轨迹作为层下钻进段显示。",
    }


def selected_trajectories(
    wells: list[dict[str, Any]], selections: list[dict[str, Any]], max_wells: int = 20,
) -> dict[str, Any]:
    well_map = {row["project_key"]: row for row in wells}
    result = []
    for selection in selections[:max_wells]:
        well = well_map.get(str(selection.get("well_key") or ""))
        if not well or not well.get("_dev_paths"):
            continue
        try:
            stations = parse_dev_stations(well["_dev_paths"][0])
        except OSError:
            continue
        if not stations:
            continue
        stride = max(1, math.ceil(len(stations) / 120))
        points = stations[::stride]
        if points[-1] is not stations[-1]:
            points.append(stations[-1])
        result.append({
            "well_key": well["project_key"], "well_name": well["canonical_name"],
            "intersection_md": _number(selection.get("intersection_md")),
            "points": [{key: row.get(key) for key in ("md", "x", "y", "z", "tvd")} for row in points],
        })
    return {"trajectories": result, "requested": min(len(selections), max_wells), "limit": max_wells}


def _read_xyz_surface(path: Path, max_points: int = 2_000_000) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            point = _xyz_triplet(raw_line)
            if point is None:
                continue
            points.append(point)
            if len(points) > max_points:
                raise ValueError(
                    f"XYZ 点数超过 {max_points:,}；为避免一次性占用过多内存，请先导出规则网格 PTD 或拆分构造面"
                )
    if len(points) < 3:
        raise ValueError("没有读取到足够的 XYZ 坐标点")
    return points


def _xyz_surface_hits(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    wells: list[dict[str, Any]],
    horizon_key: str,
    path: Path,
) -> dict[str, Any]:
    points = _read_xyz_surface(path)
    xs, ys, zs = [row[0] for row in points], [row[1] for row in points], [row[2] for row in points]
    x_min, x_max, y_min, y_max = min(xs), max(xs), min(ys), max(ys)
    area = max(0.0, (x_max - x_min) * (y_max - y_min))
    nominal_spacing = math.sqrt(area / max(1, len(points))) if area else max(x_max - x_min, y_max - y_min) / max(1, len(points) - 1)
    cell_size = max(1e-6, nominal_spacing * 3.0, max(x_max - x_min, y_max - y_min) / 5000.0)
    tolerance = cell_size * 2.5
    cells: dict[tuple[int, int], list[tuple[float, float, float]]] = defaultdict(list)
    for point in points:
        cells[(math.floor((point[0] - x_min) / cell_size), math.floor((point[1] - y_min) / cell_size))].append(point)

    def nearest(x: float, y: float) -> tuple[float, float] | None:
        col, row = math.floor((x - x_min) / cell_size), math.floor((y - y_min) / cell_size)
        best: tuple[float, float] | None = None
        for radius in range(4):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if radius and abs(dx) != radius and abs(dy) != radius:
                        continue
                    for px, py, pz in cells.get((col + dx, row + dy), []):
                        distance = math.hypot(px - x, py - y)
                        if best is None or distance < best[0]:
                            best = (distance, pz)
            if best is not None and best[0] <= (radius + 1) * cell_size:
                break
        return best if best is not None and best[0] <= tolerance else None

    # Most depth-domain exports use positive depth; elevation-domain exports use negative Z.
    z_median = median(zs)
    z_transform = (lambda value: -value) if z_median < 0 else (lambda value: value)
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    result_rows = []
    hit_count = 0
    for well in wells:
        stations = []
        if well.get("_dev_paths"):
            try:
                stations = parse_dev_stations(well["_dev_paths"][0])
            except OSError:
                stations = []
        if not stations and well.get("x") is not None and well.get("y") is not None:
            total_depth = float(well.get("total_depth") or 0.0)
            kb = float(well.get("kb_elevation") or 0.0)
            stations = [
                {"md": 0.0, "x": well["x"], "y": well["y"], "z": kb},
                {"md": total_depth, "x": well["x"], "y": well["y"], "z": kb - total_depth},
            ]
        sampled = []
        for station in stations:
            match = nearest(float(station["x"]), float(station["y"]))
            if match is None:
                continue
            distance, raw_z = match
            surface_depth = z_transform(raw_z)
            sampled.append((float(station["md"]), -float(station["z"]) - surface_depth, surface_depth, distance))
        hit = False
        intersection_md = None
        surface_z = None
        previous = None
        for current in sampled:
            if current[1] >= 0:
                hit, surface_z = True, current[2]
                if previous is not None and current[1] != previous[1]:
                    fraction = -previous[1] / (current[1] - previous[1])
                    intersection_md = previous[0] + (current[0] - previous[0]) * fraction
                else:
                    intersection_md = current[0]
                break
            previous = current
        if hit:
            hit_count += 1
        method = (
            f"DEV XYZ/TVDSS 与 XYZ/ZMAP+ 最近点交会；XY 容差 {tolerance:.3g}；"
            f"Z 解释为{'高程负值' if z_median < 0 else '正深度'}"
            if len(stations) > 2 else
            f"井口垂向近似与 XYZ/ZMAP+ 最近点交会；XY 容差 {tolerance:.3g}"
        )
        result_rows.append((project_root, horizon_key, well["project_key"], int(hit), intersection_md, surface_z, method, utcnow()))
    conn.executemany(
        """INSERT INTO horizon_well_hits(project_root,horizon_key,well_key,hit,intersection_md,surface_z,method,computed_at)
           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_root,horizon_key,well_key) DO UPDATE SET
           hit=excluded.hit,intersection_md=excluded.intersection_md,surface_z=excluded.surface_z,method=excluded.method,computed_at=excluded.computed_at""",
        result_rows,
    )
    conn.commit()
    return {
        "horizon_key": horizon_key, "wells": len(result_rows), "hit_wells": hit_count,
        "coverage_percentage": round(hit_count / max(1, len(result_rows)) * 100, 2),
        "point_count": len(points), "xy_tolerance": round(tolerance, 6),
        "z_convention": "negative_elevation" if z_median < 0 else "positive_depth",
        "method": "DEV 轨迹逐站匹配 XYZ/ZMAP+ 最近点并在 TVDSS–构造面深度变号处插值交会 MD",
    }


def compute_catalog_surface_hits(
    conn: sqlite3.Connection, snapshot: dict[str, Any], wells: list[dict[str, Any]], item_id: int,
) -> dict[str, Any]:
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    item = conn.execute(
        "SELECT id,filename,file_path FROM project_catalog_items WHERE id=? AND project_root=? AND category_key='horizons'",
        (item_id, project_root),
    ).fetchone()
    if not item:
        raise ValueError("没有找到该构造面文件")
    metadata = surface_metadata(item["file_path"])
    horizon_key = _horizon_key(snapshot, dict(item))
    if metadata.get("kind") == "regular_surface" and metadata.get("computable"):
        surface = {**metadata, "path": item["file_path"], "filename": item["filename"]}
        result = compute_surface_horizon_hits(
            conn, snapshot, wells, horizon_key, surface_override=surface,
        )
    elif metadata.get("kind") == "xyz_surface" and metadata.get("computable"):
        result = _xyz_surface_hits(conn, snapshot, wells, horizon_key, Path(item["file_path"]))
    else:
        raise ValueError("该文件没有可计算的规则网格或 XYZ 三列；2D 层位需先建立地震线邻近容差")
    result.update({"item_id": item_id, "file": item["filename"]})
    return result
