from __future__ import annotations

import re
import shlex
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .curve_analysis import is_time_depth_mnemonic
from .importers import normalize_well_name


TIME_DEPTH_EXTENSIONS = {
    ".las", ".tdr", ".checkshot", ".chk", ".cs", ".txt", ".csv", ".dat", ".asc", ".xlsx", ".xls"
}
SIDECAR_EXTENSIONS = {".xml", ".prj", ".shx", ".dbf", ".sbn", ".sbx", ".crsmeta"}
SEISMIC_EXTENSIONS = {".sgy", ".segy", ".seg-y", ".zgy"}
SEISMIC_PATH_TOKENS = ("seismic", "sísmica", "地震", "migration", "stack", "pstm", "psdm")
_BASE_INVENTORY_CACHE: dict[tuple[Any, ...], tuple[list[dict[str, Any]], int]] = {}


def _path_text(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/").lower()
    except ValueError:
        return str(path).replace("\\", "/").lower()


def time_depth_evidence(root: Path, path: Path) -> list[str]:
    """Return strong time/depth evidence while rejecting seismic TWT products."""
    suffix = path.suffix.lower()
    if suffix in SIDECAR_EXTENSIONS or suffix in SEISMIC_EXTENSIONS:
        return []
    relative = _path_text(root, path)
    if any(token in relative for token in SEISMIC_PATH_TOKENS) and "checkshot" not in relative:
        return []
    if suffix and suffix not in TIME_DEPTH_EXTENSIONS:
        return []

    spaced = re.sub(r"[_\-.]+", " ", relative)
    evidence: list[str] = []
    if re.search(r"check\s*shot", spaced) or "校深" in relative or "井震标定" in relative:
        evidence.append("checkshot")
    if re.search(r"(?:^|[^a-z0-9])tdr(?:[^a-z0-9]|$)", relative):
        evidence.append("tdr")
    if "one way time" in spaced or "onewaytime" in relative or re.search(r"(?:^|[^a-z0-9])owt(?:[^a-z0-9]|$)", relative) or "单程时间" in relative:
        evidence.append("owt")
    if "two way time" in spaced or re.search(r"(?:^|[^a-z0-9])twt(?:[^a-z0-9]|$)", relative) or "双程时间" in relative:
        evidence.append("twt")
    if "time depth" in spaced or "timedepth" in relative or "时深" in relative:
        evidence.append("time_depth")
    # Petrel checkshot exports commonly use *_TZ or *_TZ_3D without saying
    # checkshot in the filename.  Only accept TZ when the directory already
    # supplies time/depth context, so ordinary filenames are not misclassified.
    if ("checkshot" in relative or "time depth" in spaced or "时深" in relative) and re.search(r"(?:^|[_\-.])tz(?:[_\-.]|$)", path.stem.lower()):
        evidence.append("checkshot")
    return list(dict.fromkeys(evidence))


def is_time_depth_file(root: Path, path: Path) -> bool:
    return bool(time_depth_evidence(root, path))


def _clean_well_from_filename(path: Path) -> str:
    stem = path.stem
    patterns = (
        r"(?i)(?:[_\- ]?(?:logs?|check\s*shot|tdr|owt|twt|time[_\- ]?depth|tz(?:[_\- ]?3d)?))+$",
        r"(?i)(?:[_\- ]?(?:picked|edited|final|revised|rev\d+|v\d+))+$",
    )
    previous = None
    while previous != stem:
        previous = stem
        for pattern in patterns:
            stem = re.sub(pattern, "", stem).strip(" _-")
    return stem or path.stem


def parse_petrel_checkshot(path: Path) -> dict[str, Any]:
    """Parse the lightweight Petrel checkshot exchange format.

    It often carries a .las suffix but is not LAS 2.0, so the normal LAS
    parser cannot discover its Well/MD/TWT columns.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"parsed": False, "error": str(exc)}
    if "petrel checkshots format" not in text[:4096].lower():
        return {"parsed": False}
    lines = text.splitlines()
    try:
        begin = next(i for i, line in enumerate(lines) if line.strip().upper() == "BEGIN HEADER")
        end = next(i for i, line in enumerate(lines[begin + 1 :], begin + 1) if line.strip().upper() == "END HEADER")
    except StopIteration:
        return {"parsed": False, "error": "Petrel Checkshot 缺少 HEADER 边界"}
    headers = [line.strip() for line in lines[begin + 1 : end] if line.strip() and not line.lstrip().startswith("#")]
    lower = [item.lower() for item in headers]
    well_index = next((i for i, value in enumerate(lower) if value == "well" or value.startswith("well ")), None)
    md_index = next((i for i, value in enumerate(lower) if value == "md" or value.startswith("measured depth")), None)
    time_index = next((i for i, value in enumerate(lower) if "twt" in value or "owt" in value or "time" in value), None)
    wells: set[str] = set()
    md_values: list[float] = []
    time_values: list[float] = []
    count = 0
    for raw in lines[end + 1 :]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            values = shlex.split(stripped)
        except ValueError:
            continue
        if len(values) < len(headers):
            continue
        count += 1
        if well_index is not None and well_index < len(values) and values[well_index].strip():
            wells.add(values[well_index].strip())
        for index, target in ((md_index, md_values), (time_index, time_values)):
            if index is not None and index < len(values):
                try:
                    target.append(float(values[index]))
                except ValueError:
                    pass
    return {
        "parsed": True,
        "format": "Petrel Checkshot",
        "columns": headers,
        "wells": sorted(wells),
        "point_count": count,
        "md_min": min(md_values) if md_values else None,
        "md_max": max(md_values) if md_values else None,
        "time_min": min(time_values) if time_values else None,
        "time_max": max(time_values) if time_values else None,
        "time_column": headers[time_index] if time_index is not None else None,
    }


def _relation_types(mnemonics: list[str]) -> list[str]:
    kinds: list[str] = []
    for mnemonic in mnemonics:
        value = re.sub(r"[^A-Z0-9]", "", str(mnemonic).upper())
        if value.startswith("TWT"):
            kinds.append("twt")
        elif value.startswith(("OWT", "ONEWAYTIME")):
            kinds.append("owt")
        else:
            kinds.append("time_depth")
    return list(dict.fromkeys(kinds))


def _entry_matches(entry: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    compact = normalize_well_name(query)
    haystack = " ".join(
        str(entry.get(key) or "") for key in ("well_name", "well_key", "filename", "relative_path", "source_folder")
    ).upper()
    return query.upper() in haystack or (compact and compact in re.sub(r"[^A-Z0-9]", "", haystack))


def build_time_depth_inventory(
    conn: sqlite3.Connection,
    project_root: str,
    profile: dict[str, Any],
    query: str = "",
    kind: str = "",
    allowed_wells: set[str] | None = None,
    limit: int = 250,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    catalog = [dict(row) for row in conn.execute(
        """SELECT id,file_path,relative_path,filename,extension,category_key,source_folder,bytes,modified_at
           FROM project_catalog_items WHERE project_root=? ORDER BY relative_path COLLATE NOCASE""",
        (str(root),),
    )]
    signature = (
        str(root), profile.get("generated_at"), len(catalog),
        max((row.get("id") or 0 for row in catalog), default=0),
        max((str(row.get("modified_at") or "") for row in catalog), default=""),
    )
    cached = _BASE_INVENTORY_CACHE.get(signature)
    if cached is not None:
        all_entries, rejected_twt = cached
    else:
        all_entries, rejected_twt = _build_base_entries(root, catalog, profile)
        _BASE_INVENTORY_CACHE[signature] = (all_entries, rejected_twt)
        while len(_BASE_INVENTORY_CACHE) > 4:
            _BASE_INVENTORY_CACHE.pop(next(iter(_BASE_INVENTORY_CACHE)))

    all_entries = list(all_entries)
    if allowed_wells is not None:
        normalized_allowed = {normalize_well_name(item) for item in allowed_wells}
        all_entries = [row for row in all_entries if not row.get("well_key") or normalize_well_name(row["well_key"]) in normalized_allowed]
    source_counts = Counter(row["source_kind"] for row in all_entries)
    evidence_counts = Counter(value for row in all_entries for value in row["evidence"])
    unique_wells = {row["well_key"] for row in all_entries if row.get("well_key")}
    filtered = [row for row in all_entries if _entry_matches(row, query.strip())]
    if kind:
        filtered = [row for row in filtered if kind in row.get("evidence", []) or kind == row.get("source_kind")]
    filtered.sort(key=lambda row: (row.get("well_name") or "", 0 if row["source_kind"] == "independent" else 1, row["filename"]))
    return {
        "summary": {
            "file_count": len(all_entries), "well_count": len(unique_wells),
            "independent_files": source_counts["independent"], "las_curve_files": source_counts["las_curve"],
            "parsed_files": sum(bool(row.get("parsed")) for row in all_entries),
            "unmatched_well_files": sum(not row.get("well_key") for row in all_entries),
            "evidence_counts": dict(evidence_counts), "rejected_seismic_twt": rejected_twt,
        },
        "files": filtered[: max(1, min(int(limit), 1000))],
        "match_count": len(filtered), "query": query.strip(), "kind": kind,
        "truncated": len(filtered) > limit,
    }


def _build_base_entries(
    root: Path, catalog: list[dict[str, Any]], profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    by_path = {str(Path(row["file_path"]).resolve()).lower(): row for row in catalog}
    by_filename: dict[str, list[dict[str, Any]]] = {}
    for row in catalog:
        by_filename.setdefault(str(row["filename"]).lower(), []).append(row)

    entries: dict[str, dict[str, Any]] = {}
    rejected_twt = 0
    for row in catalog:
        path = Path(row["file_path"])
        evidence = time_depth_evidence(root, path)
        if not evidence:
            if "twt" in row["filename"].lower() and (row["category_key"] in {"seismic_2d", "seismic_3d"} or path.suffix.lower() in SEISMIC_EXTENSIONS):
                rejected_twt += 1
            continue
        parsed = parse_petrel_checkshot(path) if path.suffix.lower() == ".las" else {"parsed": False}
        parsed_wells = parsed.get("wells") or []
        well_name = parsed_wells[0] if len(parsed_wells) == 1 else _clean_well_from_filename(path)
        well_key = normalize_well_name(well_name)
        entries[str(path.resolve()).lower()] = {
            "catalog_id": row["id"], "file_path": row["file_path"], "relative_path": row["relative_path"],
            "filename": row["filename"], "source_folder": row["source_folder"], "extension": row["extension"],
            "well_name": well_name, "well_key": well_key, "evidence": evidence,
            "source_kind": "independent", "format": parsed.get("format") or path.suffix.lower().lstrip(".").upper() or "无扩展名",
            "parsed": bool(parsed.get("parsed")), "columns": parsed.get("columns") or [],
            "point_count": parsed.get("point_count"), "md_min": parsed.get("md_min"), "md_max": parsed.get("md_max"),
            "time_min": parsed.get("time_min"), "time_max": parsed.get("time_max"), "time_column": parsed.get("time_column"),
            "mnemonics": [], "bytes": row["bytes"], "modified_at": row["modified_at"],
            "well_ambiguous": len(parsed_wells) > 1,
        }

    for well in profile.get("wells", []):
        mnemonics = [str(curve.get("mnemonic") or "").upper() for curve in well.get("curves", []) if is_time_depth_mnemonic(curve.get("mnemonic", ""))]
        if not mnemonics:
            continue
        path_text = str(well.get("file_path") or "")
        row = by_path.get(str(Path(path_text).resolve()).lower()) if path_text else None
        if row is None:
            matches = by_filename.get(str(well.get("filename") or "").lower(), [])
            row = matches[0] if len(matches) == 1 else None
        key = str(Path(row["file_path"]).resolve()).lower() if row else f"profile:{well.get('well_key')}:{well.get('filename')}"
        entry = entries.get(key)
        if entry is None:
            entry = {
                "catalog_id": row["id"] if row else None,
                "file_path": row["file_path"] if row else path_text,
                "relative_path": row["relative_path"] if row else str(well.get("filename") or ""),
                "filename": row["filename"] if row else str(well.get("filename") or ""),
                "source_folder": row["source_folder"] if row else "LAS 曲线画像",
                "extension": row["extension"] if row else ".las",
                "well_name": well.get("well_name") or well.get("well_key"), "well_key": well.get("well_key"),
                "evidence": [], "source_kind": "las_curve", "format": "LAS 曲线",
                "parsed": True, "columns": [], "point_count": None,
                "md_min": well.get("start"), "md_max": well.get("stop"), "time_min": None, "time_max": None,
                "time_column": None, "mnemonics": [], "bytes": row["bytes"] if row else None,
                "modified_at": row["modified_at"] if row else None, "well_ambiguous": False,
            }
            entries[key] = entry
        entry["mnemonics"] = sorted(set(entry.get("mnemonics", [])) | set(mnemonics))
        entry["evidence"] = list(dict.fromkeys(entry.get("evidence", []) + ["las_curve"] + _relation_types(mnemonics)))

    return list(entries.values()), rejected_twt
