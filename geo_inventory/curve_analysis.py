from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

from .importers import normalize_well_name, parse_las, parse_las_header


DEFAULT_CURVE_TYPES = [
    {"key": "unclassified", "name": "人工未分类", "role": "PENDING", "color": "#718079", "aliases": []},
    {"key": "caliper", "name": "井径 CAL", "role": "CAL", "color": "#5aa9e6", "aliases": ["CAL", "CALI", "HCAL", "DCAL"]},
    {"key": "sp", "name": "自然电位 SP", "role": "SP", "color": "#9b8afb", "aliases": ["SP", "SSP"]},
    {"key": "gamma_ray", "name": "自然伽马 GR", "role": "GR", "color": "#b9e94b", "aliases": ["GR", "GAM", "GRC", "SGR", "CGR"]},
    {"key": "neutron", "name": "中子孔隙度 NPHI", "role": "NPHI", "color": "#e6a75d", "aliases": ["NPHI", "TNPH", "CNL", "NPOR"]},
    {"key": "density", "name": "密度 RHOB / DEN", "role": "RHOB", "color": "#ef7b74", "aliases": ["RHOB", "RHOZ", "DEN", "DENS", "ZDEN"]},
    {"key": "sonic", "name": "声波 AC / DT", "role": "DT", "color": "#53c7b7", "aliases": ["DT", "DTC", "DTCO", "AC", "SONIC"]},
    {"key": "resistivity_deep", "name": "深电阻率 RT", "role": "RT", "color": "#ee8554", "aliases": ["RT", "LLD", "RILD", "ILD", "AT90", "RD"]},
    {"key": "resistivity_medium", "name": "中电阻率 RM", "role": "RM", "color": "#d2b34c", "aliases": ["RM", "ILM", "LLM", "AT30"]},
    {"key": "resistivity_shallow", "name": "浅电阻率 RS", "role": "RS", "color": "#c588de", "aliases": ["RS", "LLS", "RILS", "MSFL", "RXO", "AT10"]},
    {"key": "laterolog", "name": "侧向电阻率 RLL", "role": "RLL", "color": "#4eb3d3", "aliases": ["RLL", "RLL3", "RLL8", "LL"]},
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_time_depth_mnemonic(mnemonic: str) -> bool:
    """Identify LAS channels that represent a time-depth relation, not a log."""
    value = re.sub(r"[^A-Z0-9]", "", mnemonic.upper())
    return value.startswith(("ONEWAYTIME", "OWT", "TWT", "GENERALTIME", "VELOCITY"))


def ensure_default_curve_types(conn: sqlite3.Connection, project_root: str) -> None:
    for item in DEFAULT_CURVE_TYPES:
        conn.execute(
            """INSERT OR IGNORE INTO curve_types(project_root,type_key,name,canonical_role,color,aliases_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (project_root, item["key"], item["name"], item["role"], item["color"], json.dumps(item["aliases"]), utcnow()),
        )
    conn.commit()


def suggest_curve_type(mnemonic: str, description: str | None = None) -> dict[str, Any]:
    value = re.sub(r"[^A-Z0-9]", "", mnemonic.upper())
    description_value = (description or "").upper()
    suffix = r"(?:\d+|DS|RAW|EDIT|NEW|OLD|MERGE|V\d+|$)"
    rules = [
        ("caliper", rf"^(?:CAL|CALI|HCAL|DCAL){suffix}", "CAL / CALI 井径命名"),
        ("sp", rf"^(?:SP|SSP){suffix}", "SP 自然电位命名"),
        ("gamma_ray", rf"^(?:GR|GAM|GRC|SGR|CGR){suffix}", "GR / GAM 自然伽马命名"),
        ("neutron", rf"^(?:NPHI|TNPH|CNL|NPOR){suffix}", "NPHI / CNL 中子命名"),
        ("density", rf"^(?:RHOB|RHOZ|DEN|DENS|ZDEN){suffix}", "RHOB / DEN 密度命名"),
        ("sonic", rf"^(?:DTCO|DTC|DT|AC|SONIC){suffix}", "AC / DT 声波命名"),
        ("resistivity_deep", rf"^(?:RILD|LLD|ILD|AT90|RT|RD){suffix}", "RT / LLD 深电阻率候选"),
        ("resistivity_medium", rf"^(?:ILM|LLM|AT30|RM){suffix}", "RM / ILM 中电阻率候选"),
        ("resistivity_shallow", rf"^(?:RILS|LLS|MSFL|RXO|AT10|RS){suffix}", "RS / LLS 浅电阻率候选"),
        ("laterolog", rf"^(?:RLL|LL){suffix}", "RLL / LL 侧向测井候选"),
    ]
    for key, pattern, reason in rules:
        if re.search(pattern, value):
            return {"type_key": key, "confidence": 0.94, "reason": reason}
    description_rules = [
        ("gamma_ray", ("GAMMA", "伽马")), ("density", ("DENSITY", "密度")),
        ("neutron", ("NEUTRON", "中子")), ("sonic", ("SONIC", "声波")),
        ("caliper", ("CALIPER", "井径")), ("sp", ("SPONTANEOUS", "自然电位")),
        ("laterolog", ("RESIST", "电阻率", "LATEROLOG")),
    ]
    for key, tokens in description_rules:
        if any(token in description_value for token in tokens):
            return {"type_key": key, "confidence": 0.68, "reason": "由曲线描述关键词推断"}
    return {"type_key": None, "confidence": 0.0, "reason": "未命中常规九类规则"}


def profile_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    wells: list[dict[str, Any]] = []
    for sample in snapshot.get("representative_analysis", {}).get("las_samples", []):
        curves = []
        details = {row.get("mnemonic"): row for row in sample.get("curve_details", [])}
        for mnemonic in sample.get("curves", []):
            detail = details.get(mnemonic, {})
            curves.append({
                "mnemonic": mnemonic, "unit": detail.get("unit"), "description": detail.get("description"),
                "sample_count": detail.get("sample_count"), "value_min": detail.get("value_min"), "value_max": detail.get("value_max"),
                "value_mean": detail.get("value_mean"), "value_std": detail.get("value_std"),
                "p05": detail.get("p05"), "p50": detail.get("p50"), "p95": detail.get("p95"),
            })
        wells.append({
            "well_key": normalize_well_name(sample.get("well") or Path(sample.get("filename", "")).stem),
            "well_name": sample.get("well"), "filename": sample.get("filename"),
            "start": sample.get("start"), "stop": sample.get("stop"), "step": sample.get("step"),
            "depth_unit": sample.get("depth_unit"), "curves": curves,
        })
    return {"source": "representative_snapshot", "sample_wells": len(wells), "wells": wells, "generated_at": snapshot.get("project", {}).get("scanned_at")}


def build_curve_profile(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    target: str | Path,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    paths = las_inventory_paths(conn, snapshot)
    wells, errors = [], []
    for index, item in enumerate(paths, 1):
        if progress_callback:
            progress_callback((index - 1) / max(1, len(paths)), f"索引 LAS 头段 {index}/{len(paths)}：{item['filename']}")
        try:
            parsed = parse_las_header(item["file_path"])
            name = parsed["well"].get("WELL", {}).get("value") or Path(item["filename"]).stem
            wells.append({
                "well_key": normalize_well_name(name), "well_name": name, "filename": item["filename"],
                "file_path": item["file_path"], "start": parsed["start"], "stop": parsed["stop"], "step": parsed["step"],
                "depth_unit": parsed.get("depth_unit"),
                "curves": [{key: curve.get(key) for key in ("mnemonic", "unit", "description", "sample_count", "value_min", "value_max", "value_mean", "value_std", "p05", "p50", "p95")} for curve in parsed["curves"][1:]],
            })
        except Exception as exc:
            errors.append({"path": item["file_path"], "error": str(exc)})
    unique_wells = {row["well_key"] for row in wells if row.get("well_key")}
    profile = {
        "schema_version": 2,
        "source": "all_las_headers",
        "scope": "all_las_headers",
        "sample_wells": len(unique_wells),
        "las_files": len(wells),
        "catalog_las_count": len(paths),
        "inventory_signature": las_inventory_signature(paths),
        "wells": wells,
        "errors": errors,
        "generated_at": utcnow(),
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    if progress_callback:
        progress_callback(1.0, "全量 LAS 头段曲线索引已建立")
    return profile


def las_inventory_paths(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every LAS known either to the directory catalog or import DB."""
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    candidates = [dict(row) for row in conn.execute(
        """SELECT file_path,filename FROM project_catalog_items
             WHERE project_root=? AND category_key='well_logs'
           UNION ALL
           SELECT file_path,filename FROM sources
             WHERE status='ready' AND data_type='las'
           ORDER BY filename""",
        (project_root,),
    )]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        path = Path(row["file_path"]).expanduser().resolve()
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append({"file_path": str(path), "filename": row.get("filename") or path.name})
    return result


def las_inventory_signature(paths: list[dict[str, Any]]) -> str:
    """Create a cheap freshness marker for the LAS-header cache."""
    parts: list[str] = []
    for row in paths:
        path = Path(row["file_path"])
        try:
            stat = path.stat()
            marker = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            marker = "missing"
        parts.append(f"{str(path.resolve()).casefold()}:{marker}")
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def curve_profile_is_current(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    profile: dict[str, Any],
) -> bool:
    profile_scope = profile.get("scope") or profile.get("source")
    if profile.get("schema_version") != 2 or profile_scope != "all_las_headers":
        return False
    paths = las_inventory_paths(conn, snapshot)
    return bool(profile.get("inventory_signature")) and profile["inventory_signature"] == las_inventory_signature(paths)


def load_curve_profile(snapshot: dict[str, Any], cache_path: str | Path | None = None) -> dict[str, Any]:
    if cache_path and Path(cache_path).is_file():
        try:
            profile = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if profile.get("schema_version") == 2 and profile.get("scope") == "all_las_headers":
                return profile
        except (OSError, ValueError):
            pass
    return profile_from_snapshot(snapshot)


def curve_statistics(profile: dict[str, Any], allowed_wells: set[str] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for well in profile.get("wells", []):
        if allowed_wells is not None and well["well_key"] not in allowed_wells:
            continue
        for curve in well.get("curves", []):
            mnemonic = curve["mnemonic"].upper()
            row = grouped.setdefault(mnemonic, {"mnemonic": mnemonic, "wells": set(), "files": set(), "units": set(), "steps": [], "depths": [], "distributions": [], "descriptions": set(), "well_details": {}})
            row["wells"].add(well["well_key"]);row["files"].add(well.get("filename"));
            if curve.get("unit"): row["units"].add(curve["unit"])
            if well.get("step") is not None: row["steps"].append(abs(float(well["step"])))
            if well.get("start") is not None and well.get("stop") is not None: row["depths"].append({"well_key":well["well_key"],"start":well["start"],"stop":well["stop"],"step":well.get("step")})
            if curve.get("description"): row["descriptions"].add(curve["description"])
            if curve.get("value_min") is not None: row["distributions"].append({key:curve.get(key) for key in ("value_min","value_max","value_mean","value_std","p05","p50","p95")})
            detail = row["well_details"].setdefault(well["well_key"], {
                "well_key": well["well_key"], "well_name": well.get("well_name") or well["well_key"],
                "filenames": set(), "start_md": None, "stop_md": None, "steps": set(), "units": set(),
                "sample_count": 0, "value_min": None, "value_max": None,
            })
            if well.get("filename"): detail["filenames"].add(well["filename"])
            if well.get("start") is not None: detail["start_md"] = min(value for value in (detail["start_md"], well["start"]) if value is not None)
            if well.get("stop") is not None: detail["stop_md"] = max(value for value in (detail["stop_md"], well["stop"]) if value is not None)
            if well.get("step") is not None: detail["steps"].add(round(abs(float(well["step"])), 6))
            if curve.get("unit"): detail["units"].add(curve["unit"])
            if curve.get("sample_count") is not None: detail["sample_count"] += int(curve["sample_count"])
            if curve.get("value_min") is not None: detail["value_min"] = min(value for value in (detail["value_min"], curve["value_min"]) if value is not None)
            if curve.get("value_max") is not None: detail["value_max"] = max(value for value in (detail["value_max"], curve["value_max"]) if value is not None)
    result=[]
    for row in grouped.values():
        steps=sorted({round(value,6) for value in row["steps"]});mins=[d["value_min"] for d in row["distributions"] if d.get("value_min") is not None];maxs=[d["value_max"] for d in row["distributions"] if d.get("value_max") is not None]
        suggestion=suggest_curve_type(row["mnemonic"]," ".join(row["descriptions"]))
        well_details = []
        for detail in row["well_details"].values():
            well_details.append({**detail, "filenames": sorted(detail["filenames"]), "steps": sorted(detail["steps"]), "units": sorted(detail["units"])})
        result.append({"mnemonic":row["mnemonic"],"well_count":len(row["wells"]),"file_count":len(row["files"]),"wells":sorted(row["wells"]),"well_details":sorted(well_details,key=lambda item:item["well_name"]),"units":sorted(row["units"]),"steps":steps,"depths":row["depths"],"distributions":row["distributions"],"value_min":min(mins) if mins else None,"value_max":max(maxs) if maxs else None,"suggestion":suggestion})
    return sorted(result,key=lambda row:(-row["well_count"],row["mnemonic"]))


def curve_workbench(conn: sqlite3.Connection, snapshot: dict[str, Any], profile: dict[str, Any], allowed_wells: set[str] | None = None) -> dict[str, Any]:
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    ensure_default_curve_types(conn, project_root)
    all_stats = curve_statistics(profile, allowed_wells)
    time_depth_stats = [row for row in all_stats if is_time_depth_mnemonic(row["mnemonic"])]
    stats = [row for row in all_stats if not is_time_depth_mnemonic(row["mnemonic"])]
    interpretation_owned = {
        str(row["attribute_name"]).upper()
        for row in conn.execute(
            """SELECT attribute_name FROM interpretation_type_assignments
               WHERE project_root=? AND status='confirmed' AND type_key!='unclassified'""",
            (project_root,),
        )
    }
    # A human-confirmed interpretation field belongs to the interpretation
    # workbench.  Keep it out of the LAS curve candidate pool so the two
    # classification views cannot silently claim the same mnemonic.
    stats = [row for row in stats if row["mnemonic"].upper() not in interpretation_owned]
    assignments = {row["mnemonic"]: dict(row) for row in conn.execute("SELECT * FROM curve_type_assignments WHERE project_root=?", (project_root,))}
    for row in stats:
        if row["mnemonic"] not in assignments and row["suggestion"]["type_key"]:
            conn.execute("INSERT OR IGNORE INTO curve_type_assignments(project_root,mnemonic,type_key,status,updated_at) VALUES(?,?,?,?,?)",(project_root,row["mnemonic"],row["suggestion"]["type_key"],"automatic",utcnow()))
    conn.commit()
    assignments = {row["mnemonic"]: dict(row) for row in conn.execute("SELECT * FROM curve_type_assignments WHERE project_root=?", (project_root,))}
    types=[dict(row) for row in conn.execute("SELECT * FROM curve_types WHERE project_root=? ORDER BY rowid",(project_root,))]
    for item in types:item["aliases"]=json.loads(item.pop("aliases_json") or "[]")
    for row in stats:row["assignment"]=assignments.get(row["mnemonic"])
    return {"profile_source":profile.get("source"),"sample_wells":profile.get("sample_wells",0),"las_files":profile.get("las_files",len(profile.get("wells",[]))),"catalog_las_count":profile.get("catalog_las_count"),"profile_errors":len(profile.get("errors",[])),"curves":stats,"time_depth_curves":time_depth_stats,"types":types,"sampling_audit":sampling_audit(stats,types),"filtered":allowed_wells is not None}


def curve_type_coverage(workbench: dict[str, Any]) -> dict[str, Any]:
    """Summarise current curve-type assignments for the cockpit.

    The summary deliberately consumes the same workbench payload used by the
    curve page.  A later manual drag/drop therefore changes this output on the
    next refresh without any second naming rule or a source-file rewrite.
    """
    types = workbench.get("types") or []
    type_order = {str(row.get("type_key")): index for index, row in enumerate(types)}
    type_map = {str(row.get("type_key")): row for row in types}
    grouped: dict[str, dict[str, Any]] = {}
    all_wells: set[str] = set()

    for curve in workbench.get("curves") or []:
        assignment = curve.get("assignment") or {}
        suggestion = curve.get("suggestion") or {}
        type_key = str(assignment.get("type_key") or suggestion.get("type_key") or "unclassified")
        type_info = type_map.get(type_key) or {
            "type_key": type_key,
            "name": "人工未分类" if type_key == "unclassified" else type_key,
            "canonical_role": None,
            "color": "#718079",
        }
        row = grouped.setdefault(type_key, {
            "type_key": type_key,
            "type_name": type_info.get("name") or type_key,
            "canonical_role": type_info.get("canonical_role"),
            "color": type_info.get("color") or "#718079",
            "_wells": set(), "_files": set(), "_mnemonics": set(), "_confirmed": set(),
        })
        mnemonic = str(curve.get("mnemonic") or "").upper()
        if mnemonic:
            row["_mnemonics"].add(mnemonic)
            if assignment.get("status") == "confirmed":
                row["_confirmed"].add(mnemonic)
        for key in curve.get("wells") or []:
            if key:
                row["_wells"].add(str(key))
                all_wells.add(str(key))
        for detail in curve.get("well_details") or []:
            for filename in detail.get("filenames") or []:
                if filename:
                    row["_files"].add(str(filename))

    denominator = len(all_wells)
    rows = []
    for row in grouped.values():
        wells = row.pop("_wells")
        files = row.pop("_files")
        mnemonics = sorted(row.pop("_mnemonics"))
        confirmed = row.pop("_confirmed")
        row.update({
            "well_count": len(wells),
            "file_count": len(files),
            "mnemonics": mnemonics,
            "mnemonic_count": len(mnemonics),
            "confirmed_mnemonic_count": len(confirmed),
            "coverage_percentage": round(len(wells) / denominator * 100, 2) if denominator else 0.0,
        })
        rows.append(row)
    # Keep the conventional / user-created categories in front of the pending
    # pool.  The latter can cover most wells in an old project and should not
    # hide the recognised standard curves at the top of the cockpit.
    rows.sort(key=lambda row: (type_order.get(row["type_key"], 9999) if row["type_key"] != "unclassified" else 10000, -row["well_count"], row["type_name"]))
    return {
        "types": rows,
        "well_denominator": denominator,
        "profile_source": workbench.get("profile_source"),
        "profile_scope": "全量 LAS 头段" if workbench.get("profile_source") == "all_las_headers" else "代表 LAS 样本",
        "filtered": bool(workbench.get("filtered")),
    }


def create_curve_type(conn: sqlite3.Connection, project_root: str, name: str, role: str | None = None, color: str = "#37c8c2") -> dict[str, Any]:
    base=re.sub(r"[^a-z0-9]+","_",(role or name).lower()).strip("_") or "custom"
    key=base;index=2
    while conn.execute("SELECT 1 FROM curve_types WHERE project_root=? AND type_key=?",(project_root,key)).fetchone():key=f"{base}_{index}";index+=1
    conn.execute("INSERT INTO curve_types(project_root,type_key,name,canonical_role,color,aliases_json,created_at) VALUES(?,?,?,?,?,?,?)",(project_root,key,name.strip(),role,color,"[]",utcnow()));conn.commit()
    return dict(conn.execute("SELECT * FROM curve_types WHERE project_root=? AND type_key=?",(project_root,key)).fetchone())


def assign_curve_type(conn: sqlite3.Connection, project_root: str, mnemonic: str, type_key: str | None) -> None:
    if not type_key:
        type_key = "unclassified"
    if not conn.execute("SELECT 1 FROM curve_types WHERE project_root=? AND type_key=?",(project_root,type_key)).fetchone():raise ValueError("目标曲线类型不存在")
    normalized = mnemonic.upper()
    conn.execute("INSERT INTO curve_type_assignments(project_root,mnemonic,type_key,status,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(project_root,mnemonic) DO UPDATE SET type_key=excluded.type_key,status='confirmed',updated_at=excluded.updated_at",(project_root,normalized,type_key,"confirmed",utcnow()))
    if type_key != "unclassified":
        conn.execute(
            "DELETE FROM interpretation_type_assignments WHERE project_root=? AND UPPER(attribute_name)=?",
            (project_root, normalized),
        )
    conn.commit()


def focus_merge_payload(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    profile: dict[str, Any],
    type_key: str,
    allowed_wells: set[str] | None = None,
) -> dict[str, Any]:
    """Build a non-destructive, per-well curve-selection plan for one curve type."""
    workbench = curve_workbench(conn, snapshot, profile, allowed_wells)
    curve_index = {row["mnemonic"]: row for row in workbench["curves"]}
    selected_type = next((row for row in workbench["types"] if row["type_key"] == type_key), None)
    if not selected_type or type_key == "unclassified":
        raise ValueError("请选择一个已定义的测井曲线类型")
    project_root = str(Path(snapshot["project"]["root"]).resolve())
    grouped: dict[str, dict[str, Any]] = {}
    unit_counts: Counter[str] = Counter()
    for well in profile.get("wells", []):
        well_key = well.get("well_key")
        if not well_key or (allowed_wells is not None and well_key not in allowed_wells):
            continue
        for curve in well.get("curves", []):
            mnemonic = str(curve.get("mnemonic") or "").upper()
            if not mnemonic or is_time_depth_mnemonic(mnemonic):
                continue
            aggregate = curve_index.get(mnemonic)
            assigned_type = (aggregate.get("assignment") or {}).get("type_key") if aggregate else None
            if not assigned_type and aggregate:
                assigned_type = (aggregate.get("suggestion") or {}).get("type_key")
            if assigned_type != type_key:
                continue
            filename = str(well.get("filename") or "代表 LAS")
            source_path = str(well.get("file_path") or "").strip()
            candidate_id = f"{source_path}::{filename}::{mnemonic}" if source_path else f"{filename}::{mnemonic}"
            start, stop = well.get("start"), well.get("stop")
            step = abs(float(well["step"])) if well.get("step") not in (None, 0) else None
            span = max(0.0, float(stop) - float(start)) if start is not None and stop is not None else 0.0
            expected = span / step + 1 if step else None
            sample_count = curve.get("sample_count")
            completeness = min(1.0, float(sample_count) / expected) if sample_count is not None and expected else (0.55 if sample_count is None else 1.0)
            unit = str(curve.get("unit") or "").strip()
            if unit:
                unit_counts[unit.upper()] += 1
            row = grouped.setdefault(well_key, {
                "well_key": well_key, "well_name": well.get("well_name") or well_key, "candidates": []
            })
            row["candidates"].append({
                "candidate_id": candidate_id, "mnemonic": mnemonic, "filename": filename,
                "source_path": source_path,
                "start": start, "stop": stop, "span": round(span, 4), "step": step,
                "sample_count": sample_count, "expected_samples": round(expected) if expected else None,
                "completeness": round(completeness * 100, 1), "unit": unit or None,
                "value_min": curve.get("value_min"), "value_max": curve.get("value_max"),
                "p05": curve.get("p05"), "p95": curve.get("p95"),
            })
    modal_unit = unit_counts.most_common(1)[0][0] if unit_counts else None
    decisions = {
        row["well_key"]: dict(row) for row in conn.execute(
            "SELECT * FROM curve_focus_decisions WHERE project_root=? AND type_key=?", (project_root, type_key)
        )
    }
    mnemonics = sorted({candidate["mnemonic"] for row in grouped.values() for candidate in row["candidates"]})
    canonical = re.sub(r"[^A-Z0-9]", "", str(selected_type.get("canonical_role") or "").upper())
    rows = []
    for row in grouped.values():
        max_span = max((candidate["span"] for candidate in row["candidates"]), default=1.0) or 1.0
        for candidate in row["candidates"]:
            normalized = re.sub(r"[^A-Z0-9]", "", candidate["mnemonic"].upper())
            coverage_score = candidate["span"] / max_span
            completeness_score = candidate["completeness"] / 100
            canonical_score = 1.0 if canonical and normalized == canonical else 0.0
            unit_score = 1.0 if not modal_unit or not candidate["unit"] or candidate["unit"].upper() == modal_unit else 0.0
            distribution_score = 1.0 if candidate["value_min"] is not None and candidate["value_max"] is not None and candidate["value_max"] > candidate["value_min"] else 0.0
            step_score = 1.0 if candidate["step"] else 0.0
            score = 0.40 * coverage_score + 0.34 * completeness_score + 0.10 * canonical_score + 0.06 * unit_score + 0.06 * distribution_score + 0.04 * step_score
            candidate["score"] = round(score * 100, 1)
            candidate["reasons"] = [
                f"深度覆盖 {coverage_score * 100:.0f}%",
                f"有效样点 {candidate['completeness']:.1f}%",
            ]
            if canonical_score:
                candidate["reasons"].append("标准 mnemonic")
            if modal_unit and candidate["unit"] and not unit_score:
                candidate["reasons"].append(f"单位偏离主单位 {modal_unit}")
        # Flag strong summary-level duplicates without claiming sample-by-sample equality.
        for candidate in row["candidates"]:
            candidate["near_duplicates"] = []
        for left_index, left in enumerate(row["candidates"]):
            for right in row["candidates"][left_index + 1:]:
                similarity = _distribution_similarity([left], [right])
                span_ratio = min(left["span"], right["span"]) / max(1.0, max(left["span"], right["span"]))
                if similarity is not None and similarity >= 0.92 and span_ratio >= 0.95 and left["step"] == right["step"]:
                    left["near_duplicates"].append(right["candidate_id"])
                    right["near_duplicates"].append(left["candidate_id"])
        decision = decisions.get(row["well_key"])
        excluded = set(json.loads(decision["excluded_json"] or "[]")) if decision else set()
        available = [candidate for candidate in row["candidates"] if candidate["candidate_id"] not in excluded]
        manual_selected = decision.get("selected_candidate_id") if decision else None
        selected = next((candidate for candidate in available if candidate["candidate_id"] == manual_selected), None)
        if not selected:
            selected = max(available, key=lambda candidate: (candidate["score"], candidate["span"], candidate["mnemonic"]), default=None)
        for candidate in row["candidates"]:
            candidate["excluded"] = candidate["candidate_id"] in excluded
            candidate["selected"] = bool(selected and candidate["candidate_id"] == selected["candidate_id"])
        row.update({
            "candidate_count": len(row["candidates"]), "mnemonic_count": len({candidate["mnemonic"] for candidate in row["candidates"]}),
            "has_all": set(mnemonics) <= {candidate["mnemonic"] for candidate in row["candidates"]},
            "selected": selected, "decision_status": "manual" if decision else "automatic",
            "manual_selected_candidate_id": manual_selected,
            "excluded_count": len(excluded),
        })
        rows.append(row)
    rows.sort(key=lambda row: (-row["candidate_count"], row["well_name"].upper()))
    return {
        "type": {key: selected_type.get(key) for key in ("type_key", "name", "canonical_role", "color")},
        "mnemonics": mnemonics, "modal_unit": modal_unit, "rows": rows,
        "profile_source": profile.get("source"), "sample_wells": profile.get("sample_wells", 0),
        "filtered": allowed_wells is not None,
        "summary": {
            "wells": len(rows), "candidate_mnemonics": len(mnemonics),
            "multi_candidate_wells": sum(row["candidate_count"] > 1 for row in rows),
            "all_candidate_wells": sum(row["has_all"] for row in rows),
            "manual_wells": sum(row["decision_status"] == "manual" for row in rows),
            "unresolved_wells": sum(row["selected"] is None for row in rows),
        },
        "note": "结果是非破坏性的逻辑合并方案；原始 LAS 与曲线均保留。",
    }


def save_focus_decision(
    conn: sqlite3.Connection,
    project_root: str,
    type_key: str,
    well_key: str,
    selected_candidate_id: str | None,
    excluded_candidate_ids: list[str],
) -> None:
    if not conn.execute(
        "SELECT 1 FROM curve_types WHERE project_root=? AND type_key=?", (project_root, type_key)
    ).fetchone():
        raise ValueError("曲线类型不存在")
    conn.execute(
        """INSERT INTO curve_focus_decisions(
           project_root,type_key,well_key,selected_candidate_id,excluded_json,status,updated_at
           ) VALUES(?,?,?,?,?,'manual',?)
           ON CONFLICT(project_root,type_key,well_key) DO UPDATE SET
           selected_candidate_id=excluded.selected_candidate_id,excluded_json=excluded.excluded_json,
           status='manual',updated_at=excluded.updated_at""",
        (project_root, type_key, well_key, selected_candidate_id, json.dumps(sorted(set(excluded_candidate_ids)), ensure_ascii=False), utcnow()),
    )
    conn.commit()


def clear_focus_decision(conn: sqlite3.Connection, project_root: str, type_key: str, well_key: str) -> None:
    conn.execute(
        "DELETE FROM curve_focus_decisions WHERE project_root=? AND type_key=? AND well_key=?",
        (project_root, type_key, well_key),
    )
    conn.commit()


def compare_curves(stats: list[dict[str, Any]], mnemonic_a: str, mnemonic_b: str) -> dict[str, Any]:
    index={row["mnemonic"]:row for row in stats};a=index.get(mnemonic_a.upper());b=index.get(mnemonic_b.upper())
    if not a or not b:raise ValueError("请选择两条已识别曲线")
    wells_a,wells_b=set(a["wells"]),set(b["wells"]);common=sorted(wells_a&wells_b);depth_a={r["well_key"]:r for r in a["depths"]};depth_b={r["well_key"]:r for r in b["depths"]}
    overlap=[]
    for key in common:
        left,right=depth_a.get(key),depth_b.get(key)
        if not left or not right:continue
        shared=max(0,min(left["stop"],right["stop"])-max(left["start"],right["start"]));union=max(left["stop"],right["stop"])-min(left["start"],right["start"])
        overlap.append(shared/union if union>0 else 0)
    step_a=set(a["steps"]);step_b=set(b["steps"]);sampling_same=bool(step_a and step_b and step_a==step_b)
    distribution_similarity=_distribution_similarity(a["distributions"],b["distributions"])
    need_standardization=distribution_similarity is not None and distribution_similarity<0.55
    return {"curve_a":a["mnemonic"],"curve_b":b["mnemonic"],"intersection_count":len(common),"only_a_count":len(wells_a-wells_b),"only_b_count":len(wells_b-wells_a),"intersection_wells":common,"only_a_wells":sorted(wells_a-wells_b),"only_b_wells":sorted(wells_b-wells_a),"depth_overlap_median":round(median(overlap)*100,1) if overlap else None,"steps_a":sorted(step_a),"steps_b":sorted(step_b),"sampling_same":sampling_same,"distribution_similarity":round(distribution_similarity,3) if distribution_similarity is not None else None,"standardization_hint":"分布差异较大，后续建模前建议检查单位、环境校正和标准化需求" if need_standardization else "未发现强烈标准化信号；仍需结合单位和地层段复核","replacement_hint":"可进入同深度段逐井复核" if common and distribution_similarity is not None and distribution_similarity>=0.55 else "不建议直接择一替代"}


def _distribution_similarity(left:list[dict[str,Any]],right:list[dict[str,Any]])->float|None:
    left_ranges=[(r.get("p05") if r.get("p05") is not None else r.get("value_min"),r.get("p95") if r.get("p95") is not None else r.get("value_max")) for r in left];right_ranges=[(r.get("p05") if r.get("p05") is not None else r.get("value_min"),r.get("p95") if r.get("p95") is not None else r.get("value_max")) for r in right]
    left_ranges=[r for r in left_ranges if None not in r];right_ranges=[r for r in right_ranges if None not in r]
    if not left_ranges or not right_ranges:return None
    a=(median([r[0] for r in left_ranges]),median([r[1] for r in left_ranges]));b=(median([r[0] for r in right_ranges]),median([r[1] for r in right_ranges]));intersection=max(0,min(a[1],b[1])-max(a[0],b[0]));union=max(a[1],b[1])-min(a[0],b[0]);return intersection/union if union>0 else (1.0 if a==b else 0.0)


def sampling_audit(stats:list[dict[str,Any]],types:list[dict[str,Any]])->list[dict[str,Any]]:
    type_names={row["type_key"]:row["name"] for row in types};groups:dict[str,list[dict[str,Any]]]=defaultdict(list)
    for row in stats:groups[(row.get("assignment")or{}).get("type_key") or row["suggestion"].get("type_key") or "unclassified"].append(row)
    result=[]
    for key,rows in groups.items():
        steps=[step for row in rows for step in row["steps"]];counts=Counter(steps);recommended=counts.most_common(1)[0][0] if counts else None;heterogeneous=len(counts)>1
        result.append({"type_key":key,"type_name":type_names.get(key,"未分类"),"mnemonics":[row["mnemonic"] for row in rows],"well_count":len(set(well for row in rows for well in row["wells"])),"steps":sorted(counts),"recommended_step":recommended,"needs_resampling":heterogeneous,"message":f"存在 {len(counts)} 种采样间隔，建议以 {recommended:g} 为评估目标" if heterogeneous and recommended is not None else "采样间隔一致" if counts else "代表快照缺少 STEP，需重建曲线画像"})
    return sorted(result,key=lambda row:(row["type_key"]=="unclassified",-row["well_count"]))
