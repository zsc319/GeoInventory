from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .curve_analysis import curve_statistics, is_time_depth_mnemonic
from .importers import normalize_well_name
from .project_catalog import CATEGORY_LABELS, petrel_well_top_names


TYPE_META = {
    "las": ("LAS 测井", True),
    "time_depth": ("时深 / Checkshot", True),
    "seismic": ("SEG-Y 地震", True),
    "well_head": ("井头表", True),
    "deviation": ("井斜轨迹", True),
    "interpretation": ("解释结论", True),
    "core": ("岩心资料", True),
    "polygon": ("Polygon", True),
    "alias": ("井名别名", True),
    "table": ("待识别表格", True),
    "ofm_mdb": ("OFM Access 数据库", True),
    "image": ("图片 / 岩心照片", False),
    "horizon": ("层位 / 网格", False),
    "fault": ("断层", False),
    "sidecar": ("空间数据附属文件", False),
    "other": ("其他文件", False),
}

TABLE_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
SIDECAR_EXTENSIONS = {".dbf", ".shx", ".prj", ".sbn", ".sbx", ".xml"}
IGNORED_DIRECTORIES = {".git", "__pycache__", ".pytest_cache", "$recycle.bin", "system volume information"}
SIDECAR_EXTENSIONS = {".xml", ".prj", ".shx", ".dbf", ".sbn", ".sbx"}


def _well_key_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"(?i)(?:_?LOGS?|_?TZ(?:_?3D)?)$", "", stem)
    return normalize_well_name(stem)


def overview_composition(
    conn,
    snapshot: dict[str, Any],
    wells: list[dict[str, Any]],
    filter_meta: dict[str, Any],
) -> dict[str, Any]:
    """Return two non-mixed scopes: unified-well coverage and physical-file share."""
    root = str(Path(snapshot["project"]["root"]).resolve())
    catalog = [dict(row) for row in conn.execute(
        "SELECT id,filename,file_path,extension,category_key,representative FROM project_catalog_items WHERE project_root=?",
        (root,),
    )]
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in catalog:
        by_category.setdefault(item["category_key"], []).append(item)
    snapshot_categories = {row["key"]: row for row in snapshot.get("categories", [])}
    denominator = len(wells)

    well_definitions = [
        ("well_heads", "Well Head", "well_head", "井口清单，可提供井名、UWI、坐标、KB 与井深"),
        ("well_paths", "Well DEV", "deviation", "井斜轨迹，以文件名规范化后关联统一井"),
        ("well_logs", "Well LAS", "las", "测井文件，以 LAS 井名/文件名关联统一井"),
        ("checkshots", "Checkshot", "checkshot", "井震校正时深关系，连接 MD/TVD 与 OWT/TWT"),
        ("well_tops", "Well Top", "well_top", "井顶/层位钻遇表；当前仅解析该目录的一份代表文件"),
    ]
    well_rows = []
    for category, label, source_type, description in well_definitions:
        items = by_category.get(category, [])
        primary = [item for item in items if item["extension"] not in SIDECAR_EXTENSIONS]
        supporting = len(items) - len(primary)
        count = sum(source_type in row.get("source_types", []) for row in wells)
        representative_scope = category == "well_tops"
        coverage = round(count / denominator * 100, 2) if denominator else 0
        row = {
            "data_type": category, "label": label, "description": description,
            "well_count": count, "denominator": denominator, "coverage_percentage": coverage,
            "missing_wells": None if representative_scope else max(0, denominator - count),
            "catalog_files": len(items), "primary_files": len(primary), "supporting_files": supporting,
            "snapshot_files": int(snapshot_categories.get(category, {}).get("files", 0)),
            "scope": "representative" if representative_scope else "full_catalog",
        }
        if category == "well_heads":
            row["raw_records"] = int(snapshot.get("wellheads", {}).get("count", 0))
            row["explanation"] = f"{row['raw_records']:,} 条原始井头记录规范化后覆盖 {count:,} 口统一井；文件份数不作为覆盖率分母。"
        elif category == "checkshots":
            grouped: dict[str, list[str]] = {}
            for item in primary:
                grouped.setdefault(_well_key_from_filename(item["filename"]), []).append(item["filename"])
            duplicates = [
                {"well_key": key, "files": filenames}
                for key, filenames in grouped.items() if key and len(filenames) > 1
            ]
            extra = sum(len(item["files"]) - 1 for item in duplicates)
            row["duplicate_groups"] = duplicates
            row["extra_file_records"] = extra
            row["explanation"] = f"{len(primary)} 份文件对应 {len(grouped)} 个规范化井名；多出的 {extra} 份是同井多版本，不是缺失文件。"
        elif representative_scope:
            representative = next((item for item in primary if item["representative"]), None)
            names = petrel_well_top_names(representative["file_path"]) if representative else []
            row["representative_file"] = representative["filename"] if representative else None
            row["representative_unique_wells"] = len({normalize_well_name(name) for name in names})
            row["explanation"] = f"{len(primary)} 个主数据表 + {supporting} 个附属文件；覆盖井数仅来自代表文件，不能当作全部版本并集。"
        else:
            extra = max(0, len(primary) - count)
            row["extra_file_records"] = extra
            row["explanation"] = f"{len(primary)} 个主数据文件关联 {count:,} 口统一井；差额通常来自同井多版本或同井多个文件。"
        well_rows.append(row)

    group_definitions = [
        ("seismic", "地震", ("seismic_3d", "seismic_2d")),
        ("horizons", "层位", ("horizons",)),
        ("faults", "断层", ("faults",)),
        ("polygons", "Polygon", ("polygons",)),
        ("production", "生产数据", ("production",)),
        ("core", "岩心资料", ("core",)),
        ("interpretations", "解释结论", ("interpretations",)),
        ("other", "其他 / 未分类", ("other",)),
    ]
    total_files = len(catalog) or int(snapshot.get("project", {}).get("total_files", 0))
    file_rows = []
    for key, label, categories in group_definitions:
        items = [item for category in categories for item in by_category.get(category, [])]
        if not items:
            continue
        primary_count = sum(item["extension"] not in SIDECAR_EXTENSIONS for item in items)
        components = [
            {"key": category, "label": CATEGORY_LABELS.get(category, category), "files": len(by_category.get(category, []))}
            for category in categories if by_category.get(category)
        ]
        file_rows.append({
            "group_key": key, "label": label, "files": len(items),
            "primary_files": primary_count, "supporting_files": len(items) - primary_count,
            "denominator": total_files,
            "percentage": round(len(items) / total_files * 100, 3) if total_files else 0,
            "components": components,
        })
    return {
        "well_source_coverage": {
            "denominator": denominator,
            "denominator_label": "筛选后统一井数（井类来源并集）" if filter_meta.get("active") else "统一井数（井类来源并集）",
            "rows": well_rows,
            "filter_active": bool(filter_meta.get("active")),
        },
        "file_source_composition": {
            "denominator": total_files, "denominator_label": "项目物理文件总数",
            "rows": file_rows,
        },
    }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def predict_file_type(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    normalized = str(path).replace("/", "\\").lower()
    stem = path.stem.lower()
    if suffix == ".las":
        key = "time_depth" if any(token in normalized for token in ("checkshot", "time-depth", "时深", "校深")) else "las"
    elif suffix in {".mdb", ".accdb"}:
        key = "ofm_mdb"
    elif suffix in {".sgy", ".segy", ".seg-y"}:
        key = "seismic"
    elif suffix in {".geojson", ".json"}:
        key = "polygon"
    elif suffix == ".shp":
        key = "polygon"
    elif suffix == ".dev":
        key = "deviation"
    elif suffix == ".ptd" or "horizon" in normalized or "层位" in normalized:
        key = "horizon"
    elif "fault" in normalized or "断层" in normalized:
        key = "fault"
    elif suffix in IMAGE_EXTENSIONS:
        key = "image"
    elif suffix in SIDECAR_EXTENSIONS:
        key = "sidecar"
    elif suffix in TABLE_EXTENSIONS:
        if any(token in normalized for token in ("wellhead", "well head", "井头", "井坐标")):
            key = "well_head"
        elif any(token in normalized for token in ("deviation", "wellpath", "well path", "轨迹", "井斜")):
            key = "deviation"
        elif any(token in normalized for token in ("interpret", "reservoir", "解释", "孔渗", "饱和")):
            key = "interpretation"
        elif any(token in normalized for token in ("core", "rock", "岩心", "岩芯", "取心")):
            key = "core"
        elif any(token in stem for token in ("alias", "井名别名", "井名对应")):
            key = "alias"
        elif any(token in normalized for token in ("polygon", "边界", "区块")):
            key = "polygon"
        else:
            key = "table"
    else:
        key = "other"
    label, importable = TYPE_META[key]
    # Project-native DEV/SHP are visible to the catalog, but the structured
    # importer currently requires tabular DEV and GeoJSON/XY polygon inputs.
    if suffix in {".dev", ".shp"}:
        importable = False
    return {"type_key": key, "type_label": label, "importable": importable}


def preflight_folder(folder: str | Path, max_files: int = 100_000) -> dict[str, Any]:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"文件夹不存在：{root}")
    files: list[dict[str, Any]] = []
    folder_count = 0
    for directory, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories
            if name.lower() not in IGNORED_DIRECTORIES and not name.lower().endswith(".nvt")
        ]
        folder_count += 1
        parent = Path(directory)
        for filename in filenames:
            if len(files) >= max_files:
                raise ValueError(f"文件数超过安全上限 {max_files:,}，请缩小预统计目录")
            path = parent / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            predicted = predict_file_type(path)
            files.append({
                "id": len(files),
                "name": path.name,
                "relative_path": str(path.relative_to(root)),
                "path": str(path),
                "extension": path.suffix.lower() or "[none]",
                "bytes": stat.st_size,
                **predicted,
            })
    grouped: dict[str, dict[str, Any]] = {}
    for item in files:
        row = grouped.setdefault(item["type_key"], {
            "type_key": item["type_key"], "type_label": item["type_label"],
            "count": 0, "bytes": 0, "importable_count": 0, "extensions": Counter(),
        })
        row["count"] += 1
        row["bytes"] += item["bytes"]
        row["importable_count"] += int(item["importable"])
        row["extensions"][item["extension"]] += 1
    groups = []
    for row in grouped.values():
        row["extensions"] = [
            {"extension": extension, "count": count}
            for extension, count in row["extensions"].most_common()
        ]
        groups.append(row)
    groups.sort(key=lambda row: (-row["count"], row["type_label"]))
    return {
        "root": str(root), "scanned_at": _utcnow(), "folder_count": folder_count,
        "total_files": len(files), "total_bytes": sum(item["bytes"] for item in files),
        "importable_files": sum(item["importable"] for item in files),
        "groups": groups, "files": files,
    }


def tree_card_payload(
    conn,
    snapshot: dict[str, Any],
    curve_profile: dict[str, Any],
    category: str,
    item_id: int | None = None,
) -> dict[str, Any]:
    root = str(Path(snapshot["project"]["root"]).resolve())
    item = None
    if item_id is not None:
        found = conn.execute(
            "SELECT * FROM project_catalog_items WHERE id=? AND project_root=?", (item_id, root)
        ).fetchone()
        item = dict(found) if found else None
        if not item:
            raise ValueError("资料对象不存在")
        category = item["category_key"]
    counts = conn.execute(
        """SELECT COUNT(*) files,COALESCE(SUM(bytes),0) bytes,COUNT(DISTINCT source_folder) folders
           FROM project_catalog_items WHERE project_root=? AND category_key=?""",
        (root, category),
    ).fetchone()
    extensions = [dict(row) for row in conn.execute(
        """SELECT extension,COUNT(*) count FROM project_catalog_items
           WHERE project_root=? AND category_key=? GROUP BY extension ORDER BY count DESC LIMIT 6""",
        (root, category),
    )]
    payload: dict[str, Any] = {
        "category": category,
        "title": item["filename"] if item else CATEGORY_LABELS.get(category, category),
        "subtitle": item["relative_path"] if item else "延迟统计资料卡",
        "item_id": item["id"] if item else None,
        "file_path": item["file_path"] if item else None,
        "files": int(counts["files"]), "bytes": int(counts["bytes"]),
        "folders": int(counts["folders"]), "extensions": extensions,
        "metrics": [], "highlights": [], "curves": [], "generated_at": _utcnow(),
    }
    if item:
        payload["metrics"] = [
            {"label": "文件大小", "value": item["bytes"]},
            {"label": "扩展名", "value": item["extension"]},
            {"label": "代表样本", "value": "是" if item["representative"] else "否"},
        ]
        if category == "well_logs":
            sample = next(
                (row for row in curve_profile.get("wells", []) if row.get("file_path") == item["file_path"]),
                None,
            ) or next(
                (row for row in curve_profile.get("wells", []) if row.get("filename") == item["filename"]),
                None,
            )
            if sample:
                regular_curves = [
                    {"name": curve["mnemonic"], "wells": 1, "start": sample.get("start"), "stop": sample.get("stop")}
                    for curve in sample.get("curves", []) if not is_time_depth_mnemonic(curve["mnemonic"])
                ]
                payload["curves"] = regular_curves[:18]
                payload["metrics"].append({"label": "常规曲线", "value": len(regular_curves)})
                if sample.get("start") is not None and sample.get("stop") is not None:
                    payload["highlights"].append(f"MD {sample['start']:,.1f} – {sample['stop']:,.1f}")
        return payload

    category_map = {row["key"]: row for row in snapshot.get("categories", [])}
    if category == "well_heads":
        head = snapshot.get("wellheads", {})
        points = head.get("points", [])
        payload["metrics"] = [
            {"label": "解析井数", "value": head.get("count", 0)},
            {"label": "有坐标井", "value": len(points)},
            {"label": "坐标系", "value": head.get("crs") or "未标注"},
        ]
        if points:
            payload["highlights"] = [
                f"X {min(p[0] for p in points):,.1f} – {max(p[0] for p in points):,.1f}",
                f"Y {min(p[1] for p in points):,.1f} – {max(p[1] for p in points):,.1f}",
            ]
    elif category == "well_logs":
        stats = [row for row in curve_statistics(curve_profile) if not is_time_depth_mnemonic(row["mnemonic"])]
        depths = [depth for row in stats for depth in row.get("depths", [])]
        payload["metrics"] = [
            {"label": "LAS 文件", "value": counts["files"]},
            {"label": "物理曲线", "value": len(stats)},
            {"label": "代表井", "value": curve_profile.get("sample_wells", 0)},
        ]
        if depths:
            payload["highlights"].append(
                f"MD {min(row['start'] for row in depths):,.1f} – {max(row['stop'] for row in depths):,.1f}"
            )
        payload["curves"] = [
            {"name": row["mnemonic"], "wells": row["well_count"],
             "start": min((d["start"] for d in row["depths"]), default=None),
             "stop": max((d["stop"] for d in row["depths"]), default=None)}
            for row in stats[:12]
        ]
    elif category == "well_paths":
        samples = snapshot.get("representative_analysis", {}).get("dev_samples", [])
        payload["metrics"] = [
            {"label": "DEV 文件", "value": counts["files"]},
            {"label": "估算覆盖井", "value": snapshot.get("well_coverage", {}).get("dev_wells", 0)},
            {"label": "代表解析", "value": len(samples)},
        ]
        max_values = [row.get("max_md") for row in samples if row.get("max_md") is not None]
        if max_values:
            payload["highlights"].append(f"代表轨迹最大 MD {max(max_values):,.1f}")
    elif category in {"seismic_3d", "seismic_2d"}:
        seismic = snapshot.get("seismic") or {}
        row = seismic.get("three_d" if category == "seismic_3d" else "two_d") or {}
        payload["metrics"] = [
            {"label": "文件", "value": category_map.get(category, {}).get("files", counts["files"])},
            {"label": "代表道数", "value": row.get("trace_count", 0)},
            {"label": "维度", "value": row.get("dimension", "—")},
        ]
        if row.get("inline_min") is not None:
            payload["highlights"].extend([
                f"Inline {row['inline_min']} – {row['inline_max']}",
                f"Crossline {row['crossline_min']} – {row['crossline_max']}",
            ])
    elif category == "polygons":
        payload["metrics"] = [
            {"label": "Polygon 文件", "value": counts["files"]},
            {"label": "井点筛选", "value": "可用"},
            {"label": "图层显隐", "value": "可用"},
        ]
    else:
        payload["metrics"] = [
            {"label": "文件", "value": counts["files"]},
            {"label": "目录", "value": counts["folders"]},
            {"label": "代表样本", "value": category_map.get(category, {}).get("files", 0)},
        ]
    return payload
