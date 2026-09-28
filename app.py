from __future__ import annotations

import csv
import atexit
import ctypes
import io
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipfile
from collections import defaultdict
from datetime import datetime
from importlib.metadata import version as package_version
from pathlib import Path
from contextlib import nullcontext
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from geo_inventory import analytics
from geo_inventory import curve_analysis as curve_tools
from geo_inventory import curve_distribution as distribution_tools
from geo_inventory import directory_translation as translation_tools
from geo_inventory import format_conversion as conversion_tools
from geo_inventory import global_filter as filter_tools
from geo_inventory import horizon_analysis as horizon_tools
from geo_inventory import interpretation_core as interpretation_tools
from geo_inventory import inventory_insights as insight_tools
from geo_inventory import las_export as las_export_tools
from geo_inventory import model_inventory as model_tools
from geo_inventory import ofm_mdb as ofm_tools
from geo_inventory import production as production_tools
from geo_inventory import production_correction as correction_tools
from geo_inventory import production_clustering as clustering_tools
from geo_inventory import reserves as reserve_tools
from geo_inventory import surface_qc as surface_qc_tools
from geo_inventory.production_training import ProductionTrainingManager
from geo_inventory import relationship_search as relationship_tools
from geo_inventory import seismic_inventory as seismic_inventory_tools
from geo_inventory import seismic_section as seismic_section_tools
from geo_inventory import time_depth as time_depth_tools
from geo_inventory.db import Database
from geo_inventory.identity import public_identity

# Some Windows installations register .js as text/plain.  Explicitly serving
# front-end bundles as JavaScript avoids browser-specific strict MIME blocking.
mimetypes.add_type("application/javascript", ".js", strict=True)
from geo_inventory.importers import ImportService, identity_similarity, normalize_well_name
from geo_inventory.jobs import ImportJobManager
from geo_inventory.project_catalog import (
    CATEGORY_LABELS,
    assign_items_to_group,
    catalog_payload,
    compare_project_trajectories,
    create_group,
    create_well_group,
    delete_catalog_group,
    delete_well_group,
    ensure_project_catalog,
    project_identity_suggestions,
    project_well_detail,
    project_well_public,
    project_wells,
    list_well_groups,
    parse_dev_stations,
    save_well_alias,
    replace_well_group_members,
    sync_imported_sources_to_catalog,
    sync_project_catalog,
    unassign_items_from_group,
    update_catalog_group,
    update_well_group,
)
from geo_inventory.project_scan import build_project_snapshot, load_snapshot, save_snapshot
from geo_inventory.workspace import APP_VERSION, WORKSPACE_FORMAT_VERSION, WorkspaceManager
from geo_inventory.path_relocation import relocate_paths


IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
BASE_DIR = RESOURCE_DIR
if IS_FROZEN:
    default_data_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "GeoInventory" / "data"
else:
    default_data_dir = RESOURCE_DIR / "data"
DATA_DIR = Path(os.environ.get("GEOINVENTORY_DATA_DIR") or default_data_dir).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def reveal_in_windows_explorer(target: Path, mode: str = "select") -> dict[str, Any]:
    """Route reveal requests through the interactive Windows desktop shell."""
    if os.name != "nt":
        raise OSError("当前系统不支持 Windows 资源管理器定位")
    target = target.resolve()
    folder = target if target.is_dir() else target.parent
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell_execute.restype = ctypes.c_void_p
    selected = False
    if mode == "folder":
        # Open the directory object through its registered shell handler. The
        # legacy "explore" verb returns access denied on some Windows builds.
        result = shell_execute(None, "open", str(folder), None, None, 1)
    else:
        result = shell_execute(None, "open", "explorer.exe", f'/select,"{target}"', str(folder), 1)
        selected = int(result or 0) > 32
        if not selected:
            # Selection can be blocked by Explorer's single-instance policy;
            # opening the containing folder is still a useful, honest fallback.
            result = shell_execute(None, "open", str(folder), None, None, 1)
    status = int(result or 0)
    if status <= 32:
        messages = {
            2: "指定文件不存在", 3: "指定目录不存在", 5: "系统拒绝访问",
            8: "系统内存不足", 31: "文件关联不可用", 32: "动态链接库不可用",
        }
        raise OSError(messages.get(status, f"Windows Shell 返回错误码 {status}"))
    return {"launcher": "Windows Shell", "shell_status": status, "folder": str(folder), "selected": selected}
SERVER_HOST = "127.0.0.1"
try:
    SERVER_PORT = int(os.environ.get("GEOINVENTORY_PORT", "5178"))
except ValueError:
    SERVER_PORT = 5178
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_SNAPSHOT = DATA_DIR / "project_snapshot.json"


def _csv_bytes(headers: list[str], rows: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    stream.write("\ufeff")
    writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _production_export_sets(conn: sqlite3.Connection) -> dict[str, tuple[list[str], list[dict]]]:
    wells = [dict(row) for row in conn.execute(
        """SELECT ow.*,s.filename source_file FROM ofm_wells ow
           JOIN sources s ON s.id=ow.source_id WHERE s.status='ready'
           ORDER BY ow.well_name"""
    )]
    intervals = [dict(row) for row in conn.execute(
        """SELECT pi.*,s.filename source_file FROM production_intervals pi
           JOIN sources s ON s.id=pi.source_id WHERE s.status='ready'
           ORDER BY pi.well_name,pi.top_md"""
    )]
    monthly = [dict(row) for row in conn.execute(
        """SELECT pm.*,s.filename source_file FROM production_monthly pm
           JOIN sources s ON s.id=pm.source_id WHERE s.status='ready'
           ORDER BY pm.well_name,pm.production_month,pm.id"""
    )]
    injection = [dict(row) for row in conn.execute(
        """SELECT im.*,s.filename source_file FROM ofm_injection_monthly im
           JOIN sources s ON s.id=im.source_id WHERE s.status='ready'
           ORDER BY im.well_name,im.production_month,im.id"""
    )]
    events = [dict(row) for row in conn.execute(
        """SELECT pe.*,s.filename source_file FROM production_events pe
           JOIN sources s ON s.id=pe.source_id WHERE s.status='ready'
           ORDER BY pe.well_name,pe.event_date,pe.id"""
    )]
    deviations = [dict(row) for row in conn.execute(
        """SELECT od.*,s.filename source_file FROM ofm_deviation_stations od
           JOIN sources s ON s.id=od.source_id WHERE s.status='ready'
           ORDER BY od.well_name,od.md,od.id"""
    )]
    markers = [dict(row) for row in conn.execute(
        """SELECT om.*,s.filename source_file FROM ofm_markers om
           JOIN sources s ON s.id=om.source_id WHERE s.status='ready'
           ORDER BY om.well_name,om.depth_md,om.id"""
    )]
    summaries = production_tools.summarize_production(monthly, events)
    well_by_key = {row["well_key"]: row for row in wells}
    interval_by_key: dict[str, list[str]] = {}
    for row in intervals:
        if row.get("interval_name"):
            interval_by_key.setdefault(row["well_key"], []).append(str(row["interval_name"]))
    summary_rows = []
    for row in summaries:
        well = well_by_key.get(row["well_key"], {})
        formations = [value for value in [well.get("zone_name"), *interval_by_key.get(row["well_key"], [])] if value]
        summary_rows.append({
            "井名": row["well_name"], "X": well.get("x"), "Y": well.get("y"),
            "生产层系": " / ".join(dict.fromkeys(formations)), "投产时间": row.get("onstream_date"),
            "生产月份": row.get("production_months"), "初期日产液_bbl_d": row.get("initial_liquid_rate"),
            "初期日产油_bbl_d": row.get("initial_oil_rate"), "初期含水率_pct": row.get("initial_water_cut"),
            "累产油_kbbl": row.get("cumulative_oil") / 1000 if row.get("cumulative_oil") is not None else None,
            "累产水_kbbl": row.get("cumulative_water") / 1000 if row.get("cumulative_water") is not None else None,
            "重点显示_INTEREST": well.get("interest"), "当前状态": row.get("latest_status") or well.get("status"),
            "压力初值": row.get("pressure_first"), "压力末值": row.get("pressure_latest"),
            "压力变化": row.get("pressure_change"), "来源文件": well.get("source_file"),
        })
    coordinate_rows = [{
        "井名": row["well_name"], "别名": row.get("alias"), "X": row.get("x"), "Y": row.get("y"),
        "井口X": row.get("surface_x"), "井口Y": row.get("surface_y"), "KB": row.get("kb_elevation"),
        "总井深": row.get("total_depth"), "完井日期": row.get("completion_date"), "井型": row.get("well_type"),
        "油田": row.get("field_name"), "层系": row.get("zone_name"), "状态": row.get("status"),
        "重点显示_INTEREST": row.get("interest"), "来源文件": row.get("source_file"),
    } for row in wells]
    interval_rows = [{
        "井名": row["well_name"], "层段": row.get("interval_name"), "顶深_MD": row.get("top_md"),
        "底深_MD": row.get("base_md"), "厚度_MD": (row["base_md"] - row["top_md"]) if row.get("base_md") is not None and row.get("top_md") is not None else None,
        "状态": row.get("status"), "日期": row.get("event_date"), "来源文件": row.get("source_file"),
    } for row in intervals]
    history_rows = [{
        "井名": row["well_name"], "日期": row.get("production_month"), "开井天数": row.get("days_on"),
        "日产液_bbl_d": row.get("liquid_rate"), "日产油_bbl_d": row.get("oil_rate"),
        "日产水_bbl_d": row.get("water_rate"), "含水率_pct": row.get("water_cut"),
        "月产油_bbl": row.get("monthly_oil"), "月产水_bbl": row.get("monthly_water"),
        "累产油_bbl": row.get("cumulative_oil"), "累产水_bbl": row.get("cumulative_water"),
        "压力": row.get("pressure"), "压力类型": row.get("pressure_type"), "状态": row.get("status"),
        "来源文件": row.get("source_file"),
    } for row in monthly]
    injection_rows = [{
        "井名": row["well_name"], "日期": row.get("production_month"), "注气": row.get("gas_injection"),
        "注水": row.get("water_injection"), "注汽": row.get("steam_injection"), "其他注入": row.get("misc_injection"),
        "溶剂注入": row.get("solvent_injection"), "空气注入": row.get("air_injection"), "来源文件": row.get("source_file"),
    } for row in injection]
    event_rows = [{
        "井名": row["well_name"], "日期": row.get("event_date"), "事件": row.get("event_type"),
        "状态": row.get("status"), "来源文件": row.get("source_file"),
    } for row in events]
    deviation_rows = [{
        "井名": row["well_name"], "MD": row.get("md"), "TVD": row.get("tvd"),
        "井口相对X偏移": row.get("x_offset"), "井口相对Y偏移": row.get("y_offset"),
        "来源文件": row.get("source_file"),
    } for row in deviations]
    marker_rows = [{
        "井名": row["well_name"], "标志 / 层位": row.get("marker_name"), "深度_MD": row.get("depth_md"),
        "日期": row.get("marker_date"), "拾取人": row.get("picker"), "来源文件": row.get("source_file"),
    } for row in markers]
    keywords = []
    for row in conn.execute(
        """SELECT os.id source_id,s.filename,tc.table_name,tc.category,tc.columns_json
           FROM ofm_table_catalog tc JOIN ofm_sources os ON os.id=tc.ofm_source_id
           JOIN sources s ON s.id=os.source_id ORDER BY os.id,tc.table_name"""
    ):
        for column in json.loads(row["columns_json"] or "[]"):
            keywords.append({
                "来源文件": row["filename"], "表名": row["table_name"], "表类别": row["category"],
                "字段": column.get("name"), "Access类型": column.get("type"), "长度": column.get("size"),
                "允许空值": "是" if column.get("nullable") else "否",
                "字段说明": ofm_tools.column_explanation(row["table_name"], column.get("name") or ""),
            })
    return {
        "coordinates": (list(coordinate_rows[0]) if coordinate_rows else ["井名", "别名", "X", "Y", "井口X", "井口Y", "KB", "总井深", "完井日期", "井型", "油田", "层系", "状态", "重点显示_INTEREST", "来源文件"], coordinate_rows),
        "perforations": (list(interval_rows[0]) if interval_rows else ["井名", "层段", "顶深_MD", "底深_MD", "厚度_MD", "状态", "日期", "来源文件"], interval_rows),
        "summary": (list(summary_rows[0]) if summary_rows else ["井名", "X", "Y", "生产层系", "投产时间", "生产月份", "初期日产液_bbl_d", "初期日产油_bbl_d", "初期含水率_pct", "累产油_kbbl", "累产水_kbbl", "重点显示_INTEREST", "当前状态", "压力初值", "压力末值", "压力变化", "来源文件"], summary_rows),
        "history": (list(history_rows[0]) if history_rows else ["井名", "日期", "开井天数", "日产液_bbl_d", "日产油_bbl_d", "日产水_bbl_d", "含水率_pct", "月产油_bbl", "月产水_bbl", "累产油_bbl", "累产水_bbl", "压力", "压力类型", "状态", "来源文件"], history_rows),
        "injection": (list(injection_rows[0]) if injection_rows else ["井名", "日期", "注气", "注水", "注汽", "其他注入", "溶剂注入", "空气注入", "来源文件"], injection_rows),
        "events": (list(event_rows[0]) if event_rows else ["井名", "日期", "事件", "状态", "来源文件"], event_rows),
        "deviations": (list(deviation_rows[0]) if deviation_rows else ["井名", "MD", "TVD", "井口相对X偏移", "井口相对Y偏移", "来源文件"], deviation_rows),
        "markers": (list(marker_rows[0]) if marker_rows else ["井名", "标志 / 层位", "深度_MD", "日期", "拾取人", "来源文件"], marker_rows),
        "keywords": (list(keywords[0]) if keywords else ["来源文件", "表名", "表类别", "字段", "Access类型", "长度", "允许空值", "字段说明"], keywords),
    }


def _ofm_deviation_dev_zip(conn: sqlite3.Connection) -> tuple[bytes, int]:
    """Create Petrel-formatted DEV files from OFM deviation stations.

    The exporter follows an existing Petrel ``.dev`` file in this workspace:
    CRLF text with no BOM, the full comment header, and the 11 numeric columns
    ``MD X Y Z TVD DX DY AZIM_TN INCL DLS AZIM_GN``.  The MDB itself has no
    azimuth/inclination fields, so these are recomputed station-by-station
    from its MD/TVD/XDELT/YDELT survey geometry.
    """
    def normalize_angle(value: float) -> float:
        return value % 360.0

    def signed_angle(value: float) -> float:
        return (value + 180.0) % 360.0 - 180.0

    def petrel_reference() -> tuple[str, float]:
        """Reuse coordinate-system and grid-to-true correction from a DEV sample."""
        candidates = conn.execute(
            """SELECT file_path FROM project_catalog_items
               WHERE category_key='well_paths' AND lower(extension)='.dev'
               ORDER BY representative DESC,id LIMIT 80"""
        )
        fallback = '# XYZ TRACE IS GIVEN IN COORDINATE SYSTEM UNDEFINED (confirm project CRS before import)'
        for candidate in candidates:
            path = Path(candidate["file_path"])
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:80]
            except OSError:
                continue
            crs_line = next((line.strip() for line in lines if line.lstrip().startswith('# XYZ TRACE IS GIVEN IN COORDINATE SYSTEM')), fallback)
            for line in lines:
                fields = line.split()
                if len(fields) < 11 or not re.match(r"^[+-]?\d", fields[0]):
                    continue
                try:
                    # The sample has AZIM_TN in col 8 and AZIM_GN in col 11.
                    return crs_line, signed_angle(float(fields[7]) - float(fields[10]))
                except ValueError:
                    break
            return crs_line, 0.0
        return fallback, 0.0

    def directional_fields(points: list[dict], true_north_correction: float) -> list[dict]:
        previous_vector: tuple[float, float, float] | None = None
        previous_azimuth = 0.0
        previous = None
        result = []
        for point in points:
            azimuth_gn = previous_azimuth
            inclination = dls = 0.0
            vector = previous_vector
            if previous is not None:
                delta_md = point["md"] - previous["md"]
                delta_x, delta_y = point["x"] - previous["x"], point["y"] - previous["y"]
                delta_tvd = point["tvd"] - previous["tvd"]
                horizontal = math.hypot(delta_x, delta_y)
                if delta_md > 1e-9:
                    inclination = math.degrees(math.atan2(horizontal, delta_tvd))
                    if horizontal > 1e-9:
                        azimuth_gn = normalize_angle(math.degrees(math.atan2(delta_x, delta_y)))
                    inclination_radians, azimuth_radians = math.radians(inclination), math.radians(azimuth_gn)
                    vector = (
                        math.sin(inclination_radians) * math.sin(azimuth_radians),
                        math.sin(inclination_radians) * math.cos(azimuth_radians),
                        math.cos(inclination_radians),
                    )
                    if previous_vector is not None:
                        cosine = max(-1.0, min(1.0, sum(left * right for left, right in zip(previous_vector, vector))))
                        dls = math.degrees(math.acos(cosine)) * 30.0 / delta_md
            point.update({
                "azimuth_gn": azimuth_gn,
                "azimuth_tn": normalize_angle(azimuth_gn + true_north_correction),
                "inclination": inclination,
                "dls": dls,
            })
            previous, previous_vector, previous_azimuth = point, vector, azimuth_gn
            result.append(point)
        return result

    crs_line, true_north_correction = petrel_reference()
    rows = [dict(row) for row in conn.execute(
        """SELECT od.well_key,od.well_name,od.md,od.tvd,od.x_offset,od.y_offset,
                  ow.x AS wellhead_x,ow.y AS wellhead_y,ow.kb_elevation,ow.well_type,
                  s.filename AS source_file
           FROM ofm_deviation_stations od
           JOIN sources s ON s.id=od.source_id
           LEFT JOIN ofm_wells ow ON ow.source_id=od.source_id AND ow.well_key=od.well_key
           WHERE s.status='ready'
           ORDER BY od.well_name,od.md,od.id"""
    )]
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row.get("well_key") or ""), str(row.get("source_file") or ""))
        if key[0]:
            groups.setdefault(key, []).append(row)
    stream = io.BytesIO()
    manifest: list[dict] = []
    used_names: set[str] = set()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for index, ((well_key, source_file), stations) in enumerate(sorted(groups.items()), 1):
            first = stations[0]
            head_x, head_y = first.get("wellhead_x"), first.get("wellhead_y")
            valid_rows = [row for row in stations if all(row.get(field) is not None for field in ("md", "tvd", "x_offset", "y_offset", "wellhead_x", "wellhead_y"))]
            well_name = str(first.get("well_name") or well_key)
            kb_raw = first.get("kb_elevation")
            kb = float(kb_raw) if kb_raw is not None else 0.0
            kb_status = "MAESTRA.KB" if kb_raw is not None else "MDB 未提供 KB，按 0.000 m 导出"
            record = {
                "well_name": well_name, "well_key": well_key, "source_file": source_file,
                "station_count": len(stations), "exported_station_count": len(valid_rows),
                "wellhead_x": head_x, "wellhead_y": head_y, "kb_m": kb,
                "kb_status": kb_status, "max_md": max((row.get("md") or 0 for row in valid_rows), default=None),
                "status": "已导出" if valid_rows else "缺少井口坐标或轨迹字段，未导出",
            }
            manifest.append(record)
            if not valid_rows:
                continue
            points = []
            for row in valid_rows:
                dx, dy, tvd = float(row["x_offset"]), float(row["y_offset"]), float(row["tvd"])
                points.append({
                    "md": float(row["md"]), "tvd": tvd, "dx": dx, "dy": dy,
                    "x": float(row["wellhead_x"]) + dx, "y": float(row["wellhead_y"]) + dy,
                    "z": kb - tvd,
                })
            points = directional_fields(points, true_north_correction)
            base = secure_filename(well_name) or f"well_{index:04d}"
            candidate = f"DEV/{base}.dev"
            suffix = 2
            while candidate.lower() in used_names:
                candidate = f"DEV/{base}_{suffix}.dev"
                suffix += 1
            used_names.add(candidate.lower())
            well_type = str(first.get("well_type") or "").upper()
            petrel_well_type = well_type if well_type in {"OIL", "GAS", "WATER", "INJECTION"} else "UNKNOWN"
            lines = [
                "# WELL TRACE FROM PETREL ",
                f"# WELL NAME:              {well_name}",
                "# DEFINITIVE SURVEY:      Reconstructed from OFM_DATA_Deviation",
                f"# WELL HEAD X-COORDINATE: {float(head_x):.8f} (m)",
                f"# WELL HEAD Y-COORDINATE: {float(head_y):.8f} (m)",
                f"# WELL DATUM (KB, Kelly bushing, from MSL): {kb:.8f} (m)",
                f"# WELL TYPE:              {petrel_well_type}",
                "# MD AND TVD ARE REFERENCED (=0) AT WELL DATUM AND INCREASE DOWNWARDS",
                "# ANGLES ARE GIVEN IN DEGREES",
                crs_line,
                "# AZIM_TN: azimuth in True North ",
                "# AZIM_GN: azimuth in Grid North ",
                "# DX DY ARE GIVEN IN GRID NORTH IN m-UNITS",
                "# DEPTH (Z, tvd_z) GIVEN IN m-UNITS",
                "#===============================================================================================================================================",
                "      MD            X            Y            Z           TVD           DX          DY        AZIM_TN        INCL         DLS        AZIM_GN",
                "#===============================================================================================================================================",
            ]
            for row in points:
                lines.append(
                    f"{row['md']:14.8f} {row['x']:14.8f} {row['y']:14.8f} {row['z']:14.8f} {row['tvd']:14.8f} "
                    f"{row['dx']:12.8f} {row['dy']:12.8f} {row['azimuth_tn']:14.8f} {row['inclination']:12.8f} "
                    f"{row['dls']:12.8f} {row['azimuth_gn']:14.8f}"
                )
            # Petrel source DEV files are ASCII/CRLF without a UTF-8 BOM.
            archive.writestr(candidate, ("\r\n".join(lines) + "\r\n").encode("ascii", errors="replace"))
        archive.writestr("DEV/Export_manifest.csv", _csv_bytes([
            "well_name", "well_key", "source_file", "station_count", "exported_station_count",
            "wellhead_x", "wellhead_y", "kb_m", "kb_status", "max_md", "status",
        ], manifest))
        archive.writestr("README.txt", (
            "Petrel-compatible OFM reconstructed DEV export\r\n\r\n"
            "Each DEV uses the Petrel column order: MD X Y Z TVD DX DY AZIM_TN INCL DLS AZIM_GN.\r\n"
            "X/Y = MAESTRA wellhead X/Y + OFM_DATA_Deviation XDELT/YDELT.\r\n"
            "Z = KB - TVD. If MAESTRA.KB is empty, KB is set to 0.000 m and identified in DEV/Export_manifest.csv.\r\n"
            "AZIM_GN, INCL and DLS are reconstructed from consecutive OFM stations. AZIM_TN applies the grid-to-true correction measured from a representative Petrel DEV in this project.\r\n"
            "Check the coordinate reference and the KB fallback list before importing to another Petrel project.\r\n"
        ).encode("ascii"))
    return stream.getvalue(), sum(1 for row in manifest if row["status"] == "已导出")


def _production_dashboard_summaries(conn: sqlite3.Connection, events: list[dict], correction_run_id: int | None = None) -> list[dict]:
    """Summarize a large OFM history in SQLite instead of transferring every row to Python.

    The production page only needs per-well metrics.  Full monthly records are
    fetched on demand for the selected well, which keeps a 200k+ row MDB
    responsive while retaining the same calculation contract as exports.
    """
    table = "production_corrected_monthly" if correction_run_id else "production_monthly"
    where = " WHERE run_id=?" if correction_run_id else ""
    params = (correction_run_id,) if correction_run_id else ()
    gas_rate_column = "gas_rate" if correction_run_id else "NULL"
    monthly_gas_column = "monthly_gas" if correction_run_id else "NULL"
    cumulative_gas_column = "cumulative_gas" if correction_run_id else "NULL"
    aggregate_rows = [dict(row) for row in conn.execute(
        f"""SELECT well_key, MIN(well_name) well_name,
                  COUNT(DISTINCT NULLIF(production_month,'')) production_months,
                  SUM(CASE WHEN days_on>0 THEN days_on ELSE 0 END) production_days,
                  SUM(COALESCE(monthly_oil,CASE WHEN oil_rate IS NOT NULL AND days_on>0 THEN oil_rate*days_on END)) cumulative_oil,
                  SUM(COALESCE(monthly_water,CASE WHEN water_rate IS NOT NULL AND days_on>0 THEN water_rate*days_on END)) cumulative_water,
                  SUM(COALESCE({monthly_gas_column},CASE WHEN {gas_rate_column} IS NOT NULL AND days_on>0 THEN {gas_rate_column}*days_on END)) cumulative_gas,
                  AVG(oil_rate) mean_oil_rate,AVG(water_rate) mean_water_rate,
                  AVG(liquid_rate) mean_liquid_rate,AVG({gas_rate_column}) mean_gas_rate
           FROM {table}{where} GROUP BY well_key""", params
    )]
    initial_rows = [dict(row) for row in conn.execute(
        f"""SELECT well_key,well_name,production_month,liquid_rate,oil_rate,water_rate,water_cut
           FROM (
             SELECT well_key,well_name,production_month,liquid_rate,oil_rate,water_rate,water_cut,id,
                    ROW_NUMBER() OVER(PARTITION BY well_key ORDER BY production_month,id) ordinal
             FROM {table}
             WHERE {'run_id=? AND (' if correction_run_id else '('}liquid_rate IS NOT NULL OR oil_rate IS NOT NULL OR water_rate IS NOT NULL
                OR monthly_oil IS NOT NULL OR monthly_water IS NOT NULL)
           ) WHERE ordinal=1""", params
    )]
    pressure_rows = [dict(row) for row in conn.execute(
        f"""SELECT well_key,
                  MAX(CASE WHEN first_rank=1 THEN pressure END) pressure_first,
                  MAX(CASE WHEN last_rank=1 THEN pressure END) pressure_latest,
                  MIN(pressure) pressure_min, MAX(pressure) pressure_max
           FROM (
             SELECT well_key,pressure,production_month,id,
                    ROW_NUMBER() OVER(PARTITION BY well_key ORDER BY production_month,id) first_rank,
                    ROW_NUMBER() OVER(PARTITION BY well_key ORDER BY production_month DESC,id DESC) last_rank
             FROM {table} WHERE pressure IS NOT NULL{' AND run_id=?' if correction_run_id else ''}
           ) GROUP BY well_key""", params
    )]
    status_rows = [dict(row) for row in conn.execute(
        f"""SELECT well_key,status FROM (
             SELECT well_key,status,production_month,id,
                    ROW_NUMBER() OVER(PARTITION BY well_key ORDER BY production_month DESC,id DESC) ordinal
             FROM {table} WHERE status IS NOT NULL AND status<>''{' AND run_id=?' if correction_run_id else ''}
           ) WHERE ordinal=1""", params
    )]
    cumulative_rows = [dict(row) for row in conn.execute(
        f"""SELECT well_key,
                  MAX(CASE WHEN oil_rank=1 THEN cumulative_oil END) cumulative_oil,
                  MAX(CASE WHEN water_rank=1 THEN cumulative_water END) cumulative_water,
                  MAX(CASE WHEN gas_rank=1 THEN cumulative_gas END) cumulative_gas
           FROM (
             SELECT well_key,cumulative_oil,cumulative_water,{cumulative_gas_column} cumulative_gas,production_month,id,
                    ROW_NUMBER() OVER(PARTITION BY well_key ORDER BY CASE WHEN cumulative_oil IS NULL THEN 1 ELSE 0 END,production_month DESC,id DESC) oil_rank,
                    ROW_NUMBER() OVER(PARTITION BY well_key ORDER BY CASE WHEN cumulative_water IS NULL THEN 1 ELSE 0 END,production_month DESC,id DESC) water_rank,
                    ROW_NUMBER() OVER(PARTITION BY well_key ORDER BY CASE WHEN {cumulative_gas_column} IS NULL THEN 1 ELSE 0 END,production_month DESC,id DESC) gas_rank
             FROM {table}{where}
           ) GROUP BY well_key""", params
    )]
    latest_rows = [dict(row) for row in conn.execute(
        f"""SELECT well_key,liquid_rate,oil_rate,water_rate,gas_rate,production_month
           FROM (
             SELECT well_key,liquid_rate,oil_rate,water_rate,{gas_rate_column} gas_rate,production_month,id,
                    ROW_NUMBER() OVER(PARTITION BY well_key ORDER BY production_month DESC,id DESC) ordinal
             FROM {table}
             WHERE {'run_id=? AND (' if correction_run_id else '('}liquid_rate IS NOT NULL OR oil_rate IS NOT NULL
                OR water_rate IS NOT NULL{' OR gas_rate IS NOT NULL' if correction_run_id else ''})
           ) WHERE ordinal=1""", params
    )]
    by_initial = {row["well_key"]: row for row in initial_rows}
    by_pressure = {row["well_key"]: row for row in pressure_rows}
    by_status = {row["well_key"]: row.get("status") for row in status_rows}
    by_cumulative = {row["well_key"]: row for row in cumulative_rows}
    by_latest = {row["well_key"]: row for row in latest_rows}
    events_by_key: dict[str, list[dict]] = {}
    for row in events:
        events_by_key.setdefault(str(row.get("well_key") or ""), []).append(row)
    summaries = []
    for aggregate in aggregate_rows:
        key = aggregate["well_key"]
        initial = by_initial.get(key, {})
        pressure = by_pressure.get(key, {})
        rate_values = [initial.get("oil_rate"), initial.get("water_rate")]
        liquid_rate = initial.get("liquid_rate")
        if liquid_rate is None:
            liquid_rate = sum(value for value in rate_values if value is not None) if any(value is not None for value in rate_values) else None
        water_cut = initial.get("water_cut")
        if water_cut is not None and 0 <= water_cut <= 1:
            water_cut *= 100
        elif water_cut is None and liquid_rate not in (None, 0) and initial.get("water_rate") is not None:
            water_cut = initial["water_rate"] / liquid_rate * 100
        events_for_well = sorted(events_by_key.get(key, []), key=lambda row: str(row.get("event_date") or ""))
        onstream = next((row.get("event_date") for row in events_for_well if "投产" in str(row.get("event_type") or "")), None)
        if not onstream:
            onstream = initial.get("production_month")
        latest_status = next((row.get("status") for row in reversed(events_for_well) if row.get("status")), None) or by_status.get(key)
        pressure_first, pressure_latest = pressure.get("pressure_first"), pressure.get("pressure_latest")
        cumulative = by_cumulative.get(key, {})
        cumulative_oil = cumulative.get("cumulative_oil") if cumulative.get("cumulative_oil") is not None else aggregate.get("cumulative_oil")
        cumulative_water = cumulative.get("cumulative_water") if cumulative.get("cumulative_water") is not None else aggregate.get("cumulative_water")
        cumulative_gas = cumulative.get("cumulative_gas") if cumulative.get("cumulative_gas") is not None else aggregate.get("cumulative_gas")
        production_days = float(aggregate.get("production_days") or 0)
        average_oil_rate = cumulative_oil / production_days if cumulative_oil is not None and production_days > 0 else aggregate.get("mean_oil_rate")
        average_water_rate = cumulative_water / production_days if cumulative_water is not None and production_days > 0 else aggregate.get("mean_water_rate")
        average_gas_rate = cumulative_gas / production_days if cumulative_gas is not None and production_days > 0 else aggregate.get("mean_gas_rate")
        average_liquid_rate = (
            (cumulative_oil + cumulative_water) / production_days
            if cumulative_oil is not None and cumulative_water is not None and production_days > 0
            else aggregate.get("mean_liquid_rate")
        )
        latest = by_latest.get(key, {})
        latest_liquid_rate = latest.get("liquid_rate")
        if latest_liquid_rate is None and any(latest.get(field) is not None for field in ("oil_rate", "water_rate")):
            latest_liquid_rate = sum(latest.get(field) or 0 for field in ("oil_rate", "water_rate"))
        summaries.append({
            "well_key": key, "well_name": initial.get("well_name") or aggregate.get("well_name") or key,
            "onstream_date": onstream, "first_production_month": initial.get("production_month"),
            "production_months": aggregate.get("production_months") or 0,
            "initial_liquid_rate": liquid_rate, "initial_oil_rate": initial.get("oil_rate"),
            "initial_water_cut": water_cut, "production_days": production_days,
            "cumulative_oil": cumulative_oil, "cumulative_water": cumulative_water,
            "cumulative_gas": cumulative_gas,
            "average_oil_rate": average_oil_rate, "average_water_rate": average_water_rate,
            "average_liquid_rate": average_liquid_rate, "average_gas_rate": average_gas_rate,
            "latest_oil_rate": latest.get("oil_rate"), "latest_water_rate": latest.get("water_rate"),
            "latest_liquid_rate": latest_liquid_rate, "latest_gas_rate": latest.get("gas_rate"),
            "latest_rate_month": latest.get("production_month"), "pressure_first": pressure_first,
            "pressure_latest": pressure_latest, "pressure_min": pressure.get("pressure_min"),
            "pressure_max": pressure.get("pressure_max"),
            "pressure_change": pressure_latest - pressure_first if pressure_first is not None and pressure_latest is not None else None,
            "event_count": len(events_for_well), "latest_status": latest_status,
        })
    # Some OFM wells have a status-change or intervention record but no usable
    # monthly PROD/pressure row.  They remain hard production-operation
    # evidence and must appear in the dashboard just as they do in the export
    # summary, otherwise the two displayed well counts disagree.
    summarized_keys = {str(row["well_key"]) for row in summaries}
    for key, rows in events_by_key.items():
        if not key or key in summarized_keys:
            continue
        events_for_well = sorted(rows, key=lambda row: str(row.get("event_date") or ""))
        first = events_for_well[0]
        latest_status = next((row.get("status") for row in reversed(events_for_well) if row.get("status")), None)
        summaries.append({
            "well_key": key, "well_name": first.get("well_name") or key,
            "onstream_date": next((row.get("event_date") for row in events_for_well if "投产" in str(row.get("event_type") or "")), None),
            "first_production_month": None, "production_months": 0,
            "initial_liquid_rate": None, "initial_oil_rate": None, "initial_water_cut": None,
            "production_days": 0, "cumulative_oil": None, "cumulative_water": None, "cumulative_gas": None,
            "average_oil_rate": None, "average_water_rate": None, "average_liquid_rate": None, "average_gas_rate": None,
            "latest_oil_rate": None, "latest_water_rate": None, "latest_liquid_rate": None, "latest_gas_rate": None,
            "latest_rate_month": None,
            "pressure_first": None, "pressure_latest": None, "pressure_min": None, "pressure_max": None,
            "pressure_change": None, "event_count": len(events_for_well), "latest_status": latest_status,
        })
    return summaries


def _build_production_export_package(database: Database) -> tuple[bytes, int]:
    """Build one authoritative export used by downloads and direct-folder export."""
    with database.connect() as conn:
        exports = _production_export_sets(conn)
        sources = [dict(row) for row in conn.execute(
            """SELECT os.id,os.file_path,s.filename,s.metadata_json
               FROM ofm_sources os JOIN sources s ON s.id=os.source_id ORDER BY os.id"""
        )]
        catalog = [dict(row) for row in conn.execute(
            """SELECT tc.ofm_source_id,tc.table_name,tc.category,tc.row_count
               FROM ofm_table_catalog tc ORDER BY tc.ofm_source_id,tc.table_name"""
        )]
    tables_by_source = {
        source["id"]: [row["table_name"] for row in catalog if row["ofm_source_id"] == source["id"]]
        for source in sources
    }
    raw_counts: dict[str, int] = {}
    for row in catalog:
        key = str(row["table_name"]).upper()
        raw_counts[key] = raw_counts.get(key, 0) + int(row["row_count"] or 0)

    profile_sources: dict[str, list[str]] = {}
    mapped_source_rows: list[str] = []
    for source in sources:
        try:
            details = json.loads(source.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            details = {}
        mapped = details.get("mapped") or {}
        if mapped:
            mapped_source_rows.append(
                f"{source.get('filename') or Path(source['file_path']).name}：" + "、".join(
                    f"{label} {int(mapped.get(key, 0)):,} 条"
                    for key, label in (("wells", "井位"), ("production", "生产"), ("pressure", "压力"),
                                       ("injection", "注入"), ("deviation", "井轨迹"), ("markers", "井上标志"),
                                       ("events", "事件"))
                    if int(mapped.get(key, 0))
                )
            )
        for item in details.get("profile") or []:
            if item.get("available") and item.get("table"):
                profile_sources.setdefault(str(item.get("kind")), []).extend(
                    [name.strip() for name in str(item["table"]).split("/") if name.strip()]
                )

    def mapped_tables(*kinds: str) -> str:
        values: list[str] = []
        for kind in kinds:
            values.extend(profile_sources.get(kind, []))
        return " + ".join(dict.fromkeys(values)) or "未识别"

    filenames = {
        "coordinates": "01_井位坐标.csv", "perforations": "02_射孔层段.csv",
        "summary": "03_井基本动态汇总.csv", "history": "04_各井生产历史.csv",
        "injection": "05_各井注入历史.csv", "keywords": "06_OFM关键字列表.csv",
        "events": "07_井状态与干预事件.csv", "deviations": "08_OFM井轨迹.csv", "markers": "09_OFM井上标志与分层.csv",
    }
    export_labels = {
        "coordinates": "井位坐标", "perforations": "射孔层段", "summary": "井基本动态汇总",
        "history": "各井生产历史", "injection": "各井注入历史", "events": "井状态与干预事件",
        "deviations": "OFM 井轨迹", "markers": "OFM 井上标志与分层", "keywords": "OFM 字段与关键字目录",
    }
    business_sources = {
        "coordinates": mapped_tables("井位与井主数据"),
        "perforations": "OFM_DATA_WBD_EQUIPMENT / 已识别射孔业务表",
        "summary": mapped_tables("井位与井主数据", "逐月生产历史", "压力测试 / 流压"),
        "history": mapped_tables("逐月生产历史", "压力测试 / 流压"),
        "injection": mapped_tables("注入历史"),
        "events": mapped_tables("井状态与干预事件"),
        "deviations": mapped_tables("井轨迹"),
        "markers": mapped_tables("井上标志 / 分层"),
        "keywords": "MDB 表结构",
    }
    availability_rows = []
    for key, filename in filenames.items():
        headers, rows = exports[key]
        source_names = [name.strip().upper() for name in business_sources[key].split("+")]
        source_rows = sum(raw_counts.get(name, 0) for name in source_names)
        if key in {"keywords", "events", "deviations", "markers"}:
            source_rows = len(rows)
        elif rows and not source_rows:
            # Localized OFM workspaces may use MAESTRA/PROD rather than XY/PRD.
            source_rows = len(rows)
        availability_rows.append({
            "分组": "标准业务导出", "文件或表": filename, "用途": export_labels[key],
            "依赖源表": business_sources[key], "源表记录数": source_rows,
            "导出记录数": len(rows), "状态": "有数据" if rows else "仅表头",
            "说明": "依赖源表当前为 0 行，因此只有表头；不是导出失败。" if not rows else "已写入结构化记录。",
        })
    for row in catalog:
        explanation = ofm_tools.table_explanation(row["table_name"], row["row_count"])
        availability_rows.append({
            "分组": "MDB 原表", "文件或表": row["table_name"], "用途": explanation["purpose"],
            "依赖源表": row["table_name"], "源表记录数": row["row_count"],
            "导出记录数": row["row_count"], "状态": "有数据" if row["row_count"] else "仅表头",
            "说明": explanation["note"],
        })
    availability_headers = ["分组", "文件或表", "用途", "依赖源表", "源表记录数", "导出记录数", "状态", "说明"]
    nonempty_tables = [(row["table_name"], int(row["row_count"])) for row in catalog if row["row_count"]]
    empty_business = [row["文件或表"] for row in availability_rows if row["分组"] == "标准业务导出" and not row["导出记录数"]]

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr("00_先读我_数据可用性.csv", _csv_bytes(availability_headers, availability_rows))
        for key, (headers, rows) in exports.items():
            archive.writestr(filenames[key], _csv_bytes(headers, rows))
        nonempty_text = "、".join(f"{name}（{count} 行）" for name, count in nonempty_tables) or "无"
        empty_text = "、".join(empty_business) or "无"
        archive.writestr(
            "README_导出说明.txt",
            (
                "GeoInventory OFM 拆分导出\n"
                "\n先看结论\n"
                f"- 标准业务导出中仅有表头的文件：{empty_text}\n"
                "- 仅表头表示当前识别到的对应业务表没有可映射的有效记录；这不是导出程序丢失数据。\n"
                f"- 当前 MDB 非空原表：{nonempty_text}\n"
                "- 所有 MDB 表都在 OFM原表/ 下逐表导出；请先查看 00_先读我_数据可用性.csv。\n"
                f"- 本次结构化映射：{'；'.join(mapped_source_rows) or '未识别到可映射业务记录'}\n"
                "\n口径与单位\n"
                "- 原始 MDB 仅以只读方式访问，未被改写。\n"
                "- 标准 PRD.OIL/WATER 按月体积、HOURS/24 作为开井天数；本工区 PROD.ACEITE/AGUA 按日率并以 DIAS 推算月量。实际采用的源表见数据可用性清单。\n"
                "- 汇总表累产量以 kbbl 输出（数据库 bbl 值除以 1000）。\n"
                "- XY.INTEREST 以“重点显示_INTEREST”原值保留，不擅自解释其业务枚举。\n"
                "- DCA 表是递减分析参数/结果，PVT 表是流体属性；二者不能替代逐月 PRD 实测生产历史。\n"
            ).encode("utf-8-sig"),
        )
        for source in sources:
            path = Path(source["file_path"])
            if not path.is_file():
                continue
            prefix = f"OFM原表/{source['id']}_{path.stem}"
            ofm_tools.add_access_tables_to_zip(archive, path, tables_by_source[source["id"]], prefix)
    return stream.getvalue(), len(availability_rows)


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(RESOURCE_DIR / "templates"),
        static_folder=str(RESOURCE_DIR / "static"),
    )
    app.config.update(
        DATABASE=os.environ.get("GEOINVENTORY_DB", str(DATA_DIR / "inventory.sqlite")),
        PROJECT_SNAPSHOT=str(PROJECT_SNAPSHOT),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    app.json.ensure_ascii = False
    workspace_manager = WorkspaceManager(DATA_DIR / "active_workspace.json")
    active_workspace = None if app.config.get("TESTING") else workspace_manager.active_path()
    startup_workspace_info = None
    if active_workspace:
        startup_workspace_info = workspace_manager.open(active_workspace)
        app.config["DATABASE"] = str(active_workspace / "inventory.sqlite")
    workspace_context = {
        "path": active_workspace,
        "snapshot_path": active_workspace / "project_snapshot.json" if active_workspace else Path(app.config["PROJECT_SNAPSHOT"]),
    }
    database = Database(app.config["DATABASE"])
    importer = ImportService(database)
    job_manager = ImportJobManager()
    correction_manager = correction_tools.ProductionCorrectionManager()
    training_manager = ProductionTrainingManager()
    clustering_cache_lock = threading.Lock()
    app.extensions["geo_database"] = database
    app.extensions["geo_jobs"] = job_manager
    app.extensions["production_correction_jobs"] = correction_manager
    app.extensions["production_clustering_jobs"] = training_manager

    def active_production_correction(conn: sqlite3.Connection) -> dict | None:
        row = conn.execute(
            "SELECT * FROM production_correction_runs WHERE status='complete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["summary"] = json.loads(result.pop("summary_json") or "{}")
        except (TypeError, ValueError):
            result["summary"] = {}
        result["available"] = Path(result["corrected_mdb_path"]).is_file()
        return result if result["available"] else None

    def production_source(conn: sqlite3.Connection, requested: str | None = None) -> tuple[str, dict | None]:
        correction = active_production_correction(conn)
        requested = str(requested or "").strip().lower()
        mode = "original" if requested == "original" or not correction else "corrected"
        return mode, correction

    def production_monthly_rows(conn: sqlite3.Connection, mode: str, correction: dict | None, well_keys: list[str] | None = None) -> list[dict]:
        keys = list(dict.fromkeys(value for value in (well_keys or []) if value))
        if mode == "corrected" and correction:
            params: list[Any] = [int(correction["id"])]
            where = "run_id=?"
            if keys:
                where += f" AND well_key IN ({','.join('?' for _ in keys)})"
                params.extend(keys)
            return [dict(row) for row in conn.execute(
                f"SELECT *, '生产动态_校正.mdb' filename, '校正快照' batch, 'active' version FROM production_corrected_monthly WHERE {where} ORDER BY well_key,production_month,id",
                params,
            )]
        params = list(keys)
        where = f"WHERE pm.well_key IN ({','.join('?' for _ in keys)})" if keys else ""
        return [dict(row) for row in conn.execute(
            f"""SELECT pm.*,s.filename,s.batch,s.version FROM production_monthly pm
                JOIN sources s ON s.id=pm.source_id {where} ORDER BY pm.well_key,pm.production_month,pm.id""",
            params,
        )]

    def snapshot_or_none():
        snapshot = load_snapshot(workspace_context["snapshot_path"])
        if snapshot:
            ensure_project_catalog(database, snapshot)
        return snapshot

    def switch_workspace(path: str | Path) -> None:
        resolved = Path(path).resolve()
        database.path = resolved / "inventory.sqlite"
        database.initialize()
        workspace_context["path"] = resolved
        workspace_context["snapshot_path"] = resolved / "project_snapshot.json"
        app.config["DATABASE"] = str(database.path)

    def active_upload_dir() -> Path:
        path = (workspace_context["path"] / "uploads") if workspace_context["path"] else UPLOAD_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def process_directory() -> Path:
        path = (workspace_context["path"] / "process") if workspace_context["path"] else DATA_DIR / "process"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def curve_profile(snapshot):
        return curve_tools.load_curve_profile(snapshot, process_directory() / "curve_profile.json")

    def project_wells_with_filter(conn, snapshot, apply_filter=True):
        rows = project_wells(conn, snapshot)
        if not apply_filter:
            return rows, {"active": False, "matched": len(rows), "total": len(rows)}
        allowed, meta = filter_tools.evaluate_filter(conn, snapshot, rows, curve_profile(snapshot))
        return [row for row in rows if row["project_key"] in allowed], meta

    def combined_wells(conn, apply_filter=True):
        imported = analytics.wells(conn)
        snapshot = snapshot_or_none()
        if not snapshot:
            return imported
        project_rows, _ = project_wells_with_filter(conn, snapshot, apply_filter)
        scanned = [project_well_public(row) for row in project_rows]
        if not imported:
            return scanned
        imported_keys = {row["normalized_key"] for row in imported}
        return imported + [row for row in scanned if row["project_key"] not in imported_keys]

    def export_scope_project_wells(conn, snapshot, requested_keys=None):
        """Return project wells after the active global filter and an optional map scope."""
        rows, filter_meta = project_wells_with_filter(conn, snapshot)
        if requested_keys:
            normalized = {normalize_well_name(value) for value in requested_keys if value}
            rows = [row for row in rows if row["project_key"] in normalized]
        return rows, filter_meta

    def complete_las_profile_for_export(conn, snapshot):
        """Use the cached full LAS-header profile, building it only when an export needs it."""
        profile = curve_profile(snapshot)
        if curve_tools.curve_profile_is_current(conn, snapshot, profile):
            return profile, False
        profile = curve_tools.build_curve_profile(conn, snapshot, process_directory() / "curve_profile.json")
        return profile, True

    def reconcile_imported_sources(rows: list[dict[str, Any]], include_historical: bool = False) -> dict[str, Any]:
        """Commit imported source objects to the tree and refresh LAS export metadata."""
        snapshot = snapshot_or_none()
        if not snapshot:
            return {"catalog": {"sources": 0, "items": 0, "missing": 0}, "profile_rebuilt": False}
        source_ids = None if include_historical else [int(row["source_id"]) for row in rows if row.get("source_id")]
        root = str(Path(snapshot["project"]["root"]).resolve())
        with database.connect() as conn:
            catalog = sync_imported_sources_to_catalog(conn, root, source_ids)
            rebuild = include_historical or any(row.get("data_type") == "las" for row in rows)
            if rebuild:
                profile = curve_tools.load_curve_profile(snapshot, process_directory() / "curve_profile.json")
                rebuild = not curve_tools.curve_profile_is_current(conn, snapshot, profile)
                if rebuild:
                    curve_tools.build_curve_profile(conn, snapshot, process_directory() / "curve_profile.json")
        return {"catalog": catalog, "profile_rebuilt": rebuild}

    def export_curve_type_inventory(conn, snapshot, profile, allowed_wells):
        """Return selectable curve types and the LAS mnemonics they currently own."""
        workbench = curve_tools.curve_workbench(conn, snapshot, profile, allowed_wells)
        by_type = {}
        for curve in workbench["curves"]:
            assignment = curve.get("assignment") or {}
            suggestion = curve.get("suggestion") or {}
            type_key = assignment.get("type_key") or suggestion.get("type_key")
            if not type_key or type_key == "unclassified":
                continue
            item = by_type.setdefault(type_key, {"mnemonics": set(), "wells": set(), "files": set()})
            item["mnemonics"].add(curve["mnemonic"])
            item["wells"].update(curve.get("wells") or [])
            item["files"].update(curve.get("files") or [])
        labels = {row["type_key"]: row for row in workbench["types"]}
        items = []
        for type_key, values in by_type.items():
            definition = labels.get(type_key, {})
            items.append({
                "type_key": type_key,
                "name": definition.get("name") or type_key,
                "canonical_role": definition.get("canonical_role"),
                "color": definition.get("color") or "#37c8c2",
                "mnemonics": sorted(values["mnemonics"]),
                "well_count": len(values["wells"]),
                "file_count": len(values["files"]),
            })
        return sorted(items, key=lambda row: (-row["well_count"], row["name"]))

    def archive_target(prefix):
        descriptor, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".zip", dir=str(process_directory()))
        os.close(descriptor)
        return Path(raw_path)

    def _safe_las_float(value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).replace(",", "").strip()
        if text == "":
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    def _parse_las_header_line_local(line: str) -> dict[str, Any] | None:
        # Petrel commonly pads mnemonics before the LAS separator, for example
        # ``DEPT .m`` or ``ManifestacionesyPérdidas ._``.  LAS permits this
        # whitespace, so do not require the dot to touch the mnemonic.
        match = re.match(r"^\s*([^\.\s]+)\s*\.\s*([^\s]*)\s*(.*?)\s*$", line)
        if not match:
            return None
        mnemonic, unit, value_and_desc = match.groups()
        value, _, desc = value_and_desc.partition(":")
        return {
            "mnemonic": mnemonic.upper(),
            "unit": unit or None,
            "value": value.strip(),
            "description": desc.strip() or None,
        }

    def _las_curve_filename(well_name: str, used: dict[str, int], index: int) -> str:
        """Return a unique LAS filename using only the unified well name."""
        base = secure_filename((well_name or f"well_{index}").strip().replace("/", "_"))
        base = base[:88].strip(" ._") if base else f"well_{index:04d}"
        if not base:
            base = f"well_{index:04d}"
        if not base.lower().endswith(".las"):
            base = f"{base}.las"
        if base.lower() not in used:
            used[base.lower()] = 1
            return base
        used[base.lower()] += 1
        stem = base[:-4] if base.lower().endswith(".las") else base
        return f"{stem}_{used[base.lower()]}.las"

    def _build_focus_curve_las(
        source_path: Path,
        mnemonic: str,
        output_mnemonic: str,
        temp_output: Path,
    ) -> tuple[dict[str, Any], int]:
        if not source_path.is_file():
            raise FileNotFoundError(f"源 LAS 文件不存在：{source_path}")
        raw = source_path.read_bytes()
        section = ""
        encoding_used: str | None = None
        for encoding in ("utf-8-sig", "latin-1", "gb18030"):
            try:
                raw.decode(encoding)
                encoding_used = encoding
                break
            except UnicodeDecodeError:
                continue
        if encoding_used is None:
            raise ValueError(f"{source_path.name}: LAS 编码无法识别")
        text = raw.decode(encoding_used)

        curve_headers: list[tuple[str, str, str]] = []
        selected_index: int | None = None
        depth_index: int | None = None
        null_value = None
        wrap = False
        headers_v: list[str] = []
        headers_w: list[str] = []
        canonical_target = re.sub(r"[^A-Z0-9]", "", str(mnemonic).upper().strip())

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("~"):
                section = line[1:2].upper()
                if section == "A":
                    break
                continue
            if section not in {"V", "W", "C"}:
                continue
            parsed = _parse_las_header_line_local(line)
            if not parsed:
                continue
            mnemonic_upper = parsed["mnemonic"]
            if section == "V":
                headers_v.append(line)
                if mnemonic_upper == "WRAP":
                    wrap = str(parsed["value"]).strip().upper() == "YES"
                continue
            if section == "W":
                headers_w.append(line)
                if mnemonic_upper == "NULL":
                    null_value = _safe_las_float(parsed["value"])
                continue
            if section == "C":
                index = len(curve_headers)
                normalized = re.sub(r"[^A-Z0-9]", "", mnemonic_upper)
                curve_headers.append((mnemonic_upper, normalized, raw_line.rstrip("\n\r")))
                if normalized and normalized == canonical_target:
                    selected_index = index
                if normalized in {"DEPT", "MD", "DEPTH", "TDEP"} and depth_index is None:
                    depth_index = index

        if depth_index is None and curve_headers:
            depth_index = 0

        if depth_index is None:
            raise ValueError(f"{source_path.name}: LAS 未识别到曲线段")
        if selected_index is None:
            raise ValueError(f"{source_path.name}: 未找到 {mnemonic} 曲线")

        selected_curve_header = None
        depth_header = None
        for curve_index, (curve_name, normalized_name, raw_line) in enumerate(curve_headers):
            if normalized_name == canonical_target:
                selected_curve_header = raw_line
            if curve_index == depth_index:
                depth_header = raw_line

        if depth_header is None:
            depth_header = curve_headers[depth_index][1] if depth_index is not None else ""
        if selected_curve_header is None:
            raise ValueError(f"{source_path.name}: 未找到 {mnemonic} 曲线定义")
        selected_curve_header = re.sub(
            r"^(\s*)[^\.\s]+(?=\s*\.)",
            lambda match: f"{match.group(1)}{output_mnemonic}",
            selected_curve_header,
            count=1,
        )

        remaining_lines = text.splitlines()
        start = 0
        for index, raw_line in enumerate(remaining_lines):
            if raw_line.strip().upper().startswith("~A"):
                start = index + 1
                break
        data_lines = remaining_lines[start:]
        expected = len(curve_headers)
        if expected <= selected_index or expected <= depth_index:
            raise ValueError(f"{source_path.name}: 曲线列映射异常")

        count = 0
        null_token = str(int(null_value)) if isinstance(null_value, (int, float)) else (str(null_value) if null_value is not None else "-999.25")

        def emit_row(values: list[str], file_handle) -> None:
            nonlocal count
            if len(values) <= max(selected_index, depth_index):
                return
            depth_raw = values[depth_index].strip()
            target_raw = values[selected_index].strip() if selected_index < len(values) else ""
            depth_value = _safe_las_float(depth_raw)
            if depth_value is None:
                return
            if target_raw == "":
                target = null_token
            else:
                target_value = _safe_las_float(target_raw)
                if target_value is None:
                    target = null_token
                elif null_value is not None and abs(target_value - null_value) < 1e-10:
                    target = null_token
                else:
                    target = target_raw
            file_handle.write(f"{depth_raw} {target}\n")
            count += 1

        with temp_output.open("w", encoding=encoding_used, newline="\n") as handle:
            handle.write("~V\n")
            for line in headers_v:
                handle.write(f"{line}\n")
            if not any("VERS" in line.upper() for line in headers_v):
                handle.write("VERS. 2.0 : CWLS LOG ASCII STANDARD\n")
            if not any("WRAP" in line.upper() for line in headers_v):
                handle.write("WRAP. NO :\n")
            handle.write("~Well Information\n")
            if headers_w:
                for line in headers_w:
                    handle.write(f"{line}\n")
            else:
                handle.write("WELL. UNKNOWN :\n")
            handle.write("~Curve Information\n")
            if depth_header:
                handle.write(f"{depth_header}\n")
            if selected_curve_header:
                handle.write(f"{selected_curve_header}\n")
            handle.write("~A\n")

            if wrap:
                tokens = " ".join(data_lines).replace(",", " ").split()
                for i in range(0, len(tokens), expected):
                    emit_row(tokens[i:i + expected], handle)
            else:
                for line in data_lines:
                    text_line = line.strip()
                    if not text_line or text_line.startswith("~"):
                        continue
                    emit_row(text_line.replace(",", " ").split(), handle)

        return {
            "source_path": str(source_path),
            "curve": str(mnemonic).upper(),
            "output_curve": output_mnemonic,
            "depth_count": count,
            "null_token": null_token,
            "encoding": encoding_used,
            "wrap": wrap,
            "null_value": null_value,
        }, count

    def archive_response(path, download_name):
        response = send_file(path, as_attachment=True, download_name=download_name, mimetype="application/zip")
        response.call_on_close(lambda: path.unlink(missing_ok=True))
        return response

    def archive_name(folder, path, project_root, fallback):
        try:
            relative = Path(path).resolve().relative_to(Path(project_root).resolve())
            return f"{folder}/{relative.as_posix()}"
        except (OSError, ValueError):
            return f"{folder}/{fallback}"

    def reconstruction_export_name(value: str) -> str:
        text = re.sub(r"[\\/:*?\"<>|]+", "_", value or "well")
        return text.strip(" ._")[:96] or "well"

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.get("/api/software-identity")
    def api_software_identity():
        """Build-embedded authorship, copyright, license, and trademark notice."""
        return jsonify(public_identity(APP_VERSION))

    @app.get("/api/project")
    def api_project_snapshot():
        snapshot = load_snapshot(workspace_context["snapshot_path"])
        # The project snapshot provides spatial/sample metadata, but the catalog
        # is the authoritative current object inventory.  Overlay its counts so
        # the left tree, cockpit counters and database always speak one file
        # count after users classify, rescan or import more local material.
        if snapshot and not snapshot.get("empty") and snapshot.get("project", {}).get("root"):
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                catalog_rows = [dict(row) for row in conn.execute(
                    """SELECT category_key,COUNT(*) files,COALESCE(SUM(bytes),0) bytes
                       FROM project_catalog_items WHERE project_root=? GROUP BY category_key""",
                    (root,),
                )]
            if catalog_rows:
                # Do not mutate the cached on-disk snapshot while serving a
                # request: classification changes should be reflected at once,
                # whereas the scan result remains auditable on disk.
                snapshot = json.loads(json.dumps(snapshot))
                original = {row.get("key"): row for row in snapshot.get("categories", [])}
                catalog_by_key = {row["category_key"]: row for row in catalog_rows}
                keys = list(dict.fromkeys([
                    *(row.get("key") for row in snapshot.get("categories", []) if row.get("key")),
                    *(row["category_key"] for row in catalog_rows),
                ]))
                categories = []
                for key in keys:
                    catalog = catalog_by_key.get(key)
                    base = dict(original.get(key) or {"key": key, "label": CATEGORY_LABELS.get(key, key)})
                    if catalog:
                        base.update({"files": int(catalog["files"]), "bytes": int(catalog["bytes"])})
                    else:
                        base.update({"files": 0, "bytes": 0})
                    categories.append(base)
                total_files = sum(int(row["files"]) for row in catalog_rows)
                total_bytes = sum(int(row["bytes"]) for row in catalog_rows)
                snapshot["categories"] = categories
                snapshot["project"]["total_files"] = total_files
                snapshot["project"]["total_bytes"] = total_bytes
                snapshot["project"]["total_gb"] = round(total_bytes / 1024 ** 3, 3)
                snapshot["project"]["inventory_source"] = "资料数据库目录索引"
        return jsonify(snapshot or {"empty": True})

    @app.post("/api/project/scan")
    def api_project_scan():
        payload = request.get_json(silent=True) or {}
        root = payload.get("root")
        if not root:
            return jsonify({"error": "请输入项目数据根目录"}), 400
        try:
            snapshot = build_project_snapshot(
                root,
                seismic_3d_path=payload.get("seismic_3d_path"),
                seismic_2d_path=payload.get("seismic_2d_path"),
                surface_path=payload.get("surface_path"),
                horizon_2d_path=payload.get("horizon_2d_path"),
                fault_path=payload.get("fault_path"),
                scan_surface_values=payload.get("scan_surface_values", True),
            )
            save_snapshot(snapshot, workspace_context["snapshot_path"])
            sync_project_catalog(database, snapshot)
            return jsonify(snapshot)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/summary")
    def api_summary():
        with database.connect() as conn:
            result = analytics.summary(conn)
            snapshot = snapshot_or_none()
            if snapshot and result["wells"] == 0:
                wells, filter_meta = project_wells_with_filter(conn, snapshot)
                categories = snapshot.get("categories", [])
                category_map = {row["key"]: row for row in categories}
                coverage = snapshot.get("well_coverage", {})
                category_wells = {
                    "well_heads": coverage.get("wellhead_wells", 0),
                    "well_paths": coverage.get("dev_wells", 0),
                    "well_logs": coverage.get("las_wells", 0),
                    "checkshots": coverage.get("checkshot_wells", 0),
                }
                result.update({
                    "wells": len(wells),
                    "located_wells": sum(row["x"] is not None and row["y"] is not None for row in wells),
                    "curve_types": len([
                        row for row in curve_tools.curve_statistics(curve_profile(snapshot), {well["project_key"] for well in wells})
                        if not curve_tools.is_time_depth_mnemonic(row.get("mnemonic", ""))
                    ]),
                    "interpretation_types": int(category_map.get("interpretations", {}).get("files", 0)),
                    "seismic_surveys": int(category_map.get("seismic_3d", {}).get("files", 0)) + int(category_map.get("seismic_2d", {}).get("files", 0)),
                    "polygons": int(category_map.get("polygons", {}).get("files", 0)),
                    "sources": int(snapshot["project"].get("representative_count", 0)),
                    "source_breakdown": [
                        {"data_type": row["key"], "files": row["files"], "records": row["files"],
                         "well_count": category_wells.get(row["key"], 0)}
                        for row in categories if row["files"]
                    ],
                    "confidence": [
                        {"level": level, "count": sum(row["identity_status"] == level for row in wells)}
                        for level in ("多源互证", "单源硬证据", "待核实身份")
                    ],
                    "identity_standard": {
                        "existence": "Well Head、DEV、LAS、生产动态任一出现，即确认井实体存在",
                        "verified": "两类及以上硬数据指向同一标准井名，记为多源互证",
                        "single": "只有一类硬数据仍是已存在井，只是缺少跨来源互证",
                        "pending": "仅有 Well Top/Checkshot 等辅助记录，或不同来源井口坐标偏差超过 5 个坐标单位",
                    },
                    "mode": "project_scan",
                    "global_filter": filter_meta,
                })
                result.update(insight_tools.overview_composition(conn, snapshot, wells, filter_meta))
            return jsonify(result)

    @app.get("/api/wells")
    def api_wells():
        polygon_id = request.args.get("polygon_id")
        bbox = request.args.get("bbox")
        apply_filter = request.args.get("ignore_filter") != "1"
        with database.connect() as conn:
            if bbox:
                try:
                    x_min, y_min, x_max, y_max = [float(value) for value in bbox.split(",")]
                except (TypeError, ValueError):
                    return jsonify({"error": "bbox 应为 xmin,ymin,xmax,ymax"}), 400
                rows = combined_wells(conn, apply_filter)
                return jsonify([row for row in rows if row.get("x") is not None and x_min <= row["x"] <= x_max and y_min <= row["y"] <= y_max])
            if polygon_id and polygon_id.startswith("project:"):
                snapshot = snapshot_or_none()
                rows = combined_wells(conn, apply_filter)
                polygon = next((row for row in filter_tools.project_polygons(conn, snapshot) if row["id"] == polygon_id), None) if snapshot else None
                if not polygon:
                    return jsonify({"error": "Polygon 不存在"}), 404
                rows = [row for row in rows if row.get("x") is not None and filter_tools.point_in_project_polygon(row["x"], row["y"], polygon)]
                return jsonify(rows)
            if polygon_id:
                return jsonify(analytics.wells(conn, int(polygon_id)))
            return jsonify(combined_wells(conn, apply_filter))

    @app.get("/api/wells/<well_ref>")
    def api_well_detail(well_ref: str):
        with database.connect() as conn:
            if well_ref.startswith("project:"):
                snapshot = snapshot_or_none()
                data = project_well_detail(conn, snapshot, well_ref.split(":", 1)[1]) if snapshot else None
            else:
                try:
                    data = analytics.well_detail(conn, int(well_ref))
                except ValueError:
                    data = None
        return jsonify(data) if data else (jsonify({"error": "井不存在"}), 404)

    @app.get("/api/well-evidence-matrix")
    def api_well_evidence_matrix():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"rows": [], "total": 0, "filter": {"active": False}})
        with database.connect() as conn:
            rows, filter_meta = project_wells_with_filter(conn, snapshot)
        result = []
        for row in rows:
            evidence = {"las": [], "deviation": [], "well_top": []}
            seen: dict[str, set[str]] = {key: set() for key in evidence}
            for source in row.get("_sources", []):
                data_type = source.get("data_type")
                if data_type not in evidence:
                    continue
                identity = str(source.get("catalog_id") or source.get("file_path") or source.get("filename"))
                if identity in seen[data_type]:
                    continue
                seen[data_type].add(identity)
                evidence[data_type].append({
                    "catalog_id": source.get("catalog_id"),
                    "filename": source.get("filename"),
                    "directory": source.get("batch"),
                    "file_path": source.get("file_path"),
                })
            result.append({
                "well_key": row["project_key"], "well_name": row["canonical_name"],
                "las": evidence["las"], "dev": evidence["deviation"], "top": evidence["well_top"],
            })
        return jsonify({"rows": result, "total": len(result), "filter": filter_meta})

    @app.get("/api/well-coordinate-quality")
    def api_well_coordinate_quality():
        """Coordinate evidence is intentionally calculated on demand.

        The result is a compact summary only: DEV station arrays are released
        after every file so opening the well page does not retain trajectories
        in memory.  A 0.01-coordinate-unit bucket is used for a reproducible
        definition of an overlapping surface/bottom position.
        """
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"total": 0, "coordinate_wells": 0, "head_overlap_wells": 0,
                            "head_overlap_groups": 0, "bottom_overlap_wells": 0,
                            "bottom_overlap_groups": 0, "missing_coordinate_wells": 0,
                            "bottom_checked_wells": 0, "filter": {"active": False}})
        with database.connect() as conn:
            rows, filter_meta = project_wells_with_filter(conn, snapshot)
        surface_points: defaultdict[tuple[float, float], set[str]] = defaultdict(set)
        bottom_points: defaultdict[tuple[float, float], set[str]] = defaultdict(set)
        coordinate_wells = 0
        bottom_checked = 0
        for row in rows:
            key = row["project_key"]
            if row.get("x") is not None and row.get("y") is not None:
                coordinate_wells += 1
            # Prefer an explicit Well Head source; fall back to the resolved
            # coordinate (which can originate from a matched production file).
            heads = [(source.get("x"), source.get("y")) for source in row.get("_sources", [])
                     if source.get("data_type") == "well_head" and source.get("x") is not None and source.get("y") is not None]
            if not heads and row.get("x") is not None and row.get("y") is not None:
                heads = [(row["x"], row["y"])]
            for x, y in heads:
                surface_points[(round(float(x), 2), round(float(y), 2))].add(key)
            # One indexed DEV is enough to establish a well-bottom position;
            # multiple DEV versions are handled by the dedicated strong match.
            paths = row.get("_dev_paths") or []
            if not paths:
                continue
            try:
                stations = parse_dev_stations(sorted(paths)[0])
                if stations:
                    last = stations[-1]
                    bottom_points[(round(float(last["x"]), 2), round(float(last["y"]), 2))].add(key)
                    bottom_checked += 1
            except (OSError, ValueError, KeyError):
                continue
        def overlap(points):
            groups = [keys for keys in points.values() if len(keys) > 1]
            return sum(len(keys) for keys in groups), len(groups)
        head_wells, head_groups = overlap(surface_points)
        bottom_wells, bottom_groups = overlap(bottom_points)
        return jsonify({
            "total": len(rows), "coordinate_wells": coordinate_wells,
            "head_overlap_wells": head_wells, "head_overlap_groups": head_groups,
            "bottom_overlap_wells": bottom_wells, "bottom_overlap_groups": bottom_groups,
            "missing_coordinate_wells": len(rows) - coordinate_wells,
            "bottom_checked_wells": bottom_checked,
            "filter": filter_meta,
            "method": "井头与井底坐标按 0.01 坐标单位归桶；井底取每口统一井的第一份有效 DEV 最末测点。",
        })

    @app.get("/api/wells/<well_ref>/trajectory")
    def api_trajectory(well_ref: str):
        with database.connect() as conn:
            if well_ref.startswith("project:"):
                snapshot = snapshot_or_none()
                key = well_ref.split(":", 1)[1]
                row = next((item for item in project_wells(conn, snapshot) if item["project_key"] == key), None) if snapshot else None
                if not row or not row["_dev_paths"]:
                    return jsonify([])
                return jsonify(parse_dev_stations(sorted(row["_dev_paths"])[0]))
            return jsonify(analytics.trajectory(conn, int(well_ref)))

    @app.get("/api/curves")
    def api_curves():
        with database.connect() as conn:
            rows = [row for row in analytics.curve_statistics(conn) if not curve_tools.is_time_depth_mnemonic(row["mnemonic"])]
            snapshot = snapshot_or_none()
            if not rows and snapshot:
                filtered_wells, _ = project_wells_with_filter(conn, snapshot)
                allowed = {row["project_key"] for row in filtered_wells}
                workbench = curve_tools.curve_workbench(conn, snapshot, curve_profile(snapshot), allowed)
                rows = [{**row, "units": ", ".join(row.get("units", [])), "sample_count": None, "sampling": True} for row in workbench["curves"]]
            return jsonify(rows)

    @app.get("/api/curve-workbench")
    def api_curve_workbench():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        with database.connect() as conn:
            filtered_wells, _ = project_wells_with_filter(conn, snapshot)
            allowed = {row["project_key"] for row in filtered_wells}
            return jsonify(curve_tools.curve_workbench(conn, snapshot, curve_profile(snapshot), allowed))

    @app.get("/api/project/curve-type-coverage")
    def api_project_curve_type_coverage():
        """Cockpit view of the exact same curve-type assignments as the workbench."""
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        with database.connect() as conn:
            filtered_wells, filter_meta = project_wells_with_filter(conn, snapshot)
            allowed = {row["project_key"] for row in filtered_wells}
            workbench = curve_tools.curve_workbench(conn, snapshot, curve_profile(snapshot), allowed)
            result = curve_tools.curve_type_coverage(workbench)
        result["filter"] = filter_meta
        return jsonify(result)

    @app.get("/api/time-depth")
    def api_time_depth():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        with database.connect() as conn:
            filtered_wells, _ = project_wells_with_filter(conn, snapshot)
            allowed = {row["project_key"] for row in filtered_wells}
            profile = curve_profile(snapshot)
            time_curves = [
                row for row in curve_tools.curve_statistics(profile, allowed)
                if curve_tools.is_time_depth_mnemonic(row["mnemonic"])
            ]
            inventory = time_depth_tools.build_time_depth_inventory(
                conn,
                str(Path(snapshot["project"]["root"]).resolve()),
                profile,
                query=request.args.get("q", ""),
                kind=request.args.get("kind", ""),
                allowed_wells=allowed,
                limit=request.args.get("limit", 250, type=int),
            )
        compact_curves = []
        for row in time_curves:
            starts = [item["start"] for item in row.get("depths", []) if item.get("start") is not None]
            stops = [item["stop"] for item in row.get("depths", []) if item.get("stop") is not None]
            compact_curves.append({
                "mnemonic": row["mnemonic"], "well_count": row["well_count"], "file_count": row["file_count"],
                "units": row.get("units", []), "steps": row.get("steps", []),
                "depth_min": min(starts) if starts else None, "depth_max": max(stops) if stops else None,
            })
        return jsonify({
            "profile_source": profile.get("source"),
            "sample_wells": profile.get("sample_wells", 0),
            "curves": compact_curves,
            "filtered": True,
            **inventory,
        })

    @app.post("/api/curve-profile/rebuild")
    def api_rebuild_curve_profile():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        # Whole-project inventory reads LAS headers only; sample arrays stay lazy.
        with database.connect() as conn:
            profile = curve_tools.build_curve_profile(conn, snapshot, process_directory() / "curve_profile.json")
        return jsonify({"ok": True, "sample_wells": profile["sample_wells"], "las_files": profile.get("las_files", 0), "errors": profile.get("errors", [])})

    @app.post("/api/curve-types")
    def api_create_curve_type():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                return jsonify(curve_tools.create_curve_type(conn, root, str(payload["name"]), payload.get("role"), payload.get("color", "#37c8c2")))
        except (KeyError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/curve-types/assign")
    def api_assign_curve_type():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                curve_tools.assign_curve_type(conn, root, str(payload["mnemonic"]), payload.get("type_key"))
            return jsonify({"ok": True})
        except (KeyError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/curve-compare")
    def api_curve_compare():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            with database.connect() as conn:
                filtered_wells, _ = project_wells_with_filter(conn, snapshot)
            stats = curve_tools.curve_statistics(curve_profile(snapshot), {row["project_key"] for row in filtered_wells})
            return jsonify(curve_tools.compare_curves(stats, str(payload["curve_a"]), str(payload["curve_b"])))
        except (KeyError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/curve-focus")
    def api_curve_focus():
        snapshot = snapshot_or_none()
        type_key = request.args.get("type_key")
        if not snapshot or not type_key:
            return jsonify({"error": "请选择需要聚焦合并的曲线类型"}), 400
        try:
            with database.connect() as conn:
                filtered_wells, _ = project_wells_with_filter(conn, snapshot)
                allowed = {row["project_key"] for row in filtered_wells}
                return jsonify(curve_tools.focus_merge_payload(conn, snapshot, curve_profile(snapshot), type_key, allowed))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/curve-focus/decision")
    def api_curve_focus_decision():
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                curve_tools.save_focus_decision(
                    conn, root, str(payload["type_key"]), str(payload["well_key"]),
                    payload.get("selected_candidate_id"),
                    [str(value) for value in payload.get("excluded_candidate_ids", [])],
                )
            return jsonify({"ok": True})
        except (KeyError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/curve-focus/decision")
    def api_clear_curve_focus_decision():
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                curve_tools.clear_focus_decision(conn, root, str(payload["type_key"]), str(payload["well_key"]))
            return jsonify({"ok": True})
        except (KeyError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/curve-focus/export")
    def api_curve_focus_export():
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        if not payload and request.form:
            payload = request.form.to_dict()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        type_key = str(payload.get("type_key") or "").strip()
        if not type_key:
            return jsonify({"error": "请选择需要导出的曲线类型"}), 400
        requested_output_mnemonic = str(payload.get("output_mnemonic") or "").strip()
        output_dir_raw = (payload.get("output_dir") or "").strip()
        output_dir = Path(output_dir_raw).expanduser() if output_dir_raw else None
        if output_dir is not None and not output_dir.is_dir():
            return jsonify({"error": f"导出目录不存在：{output_dir}"}), 400
        if output_dir is not None:
            output_dir = output_dir.resolve()
        archive_path: Path | None = None
        try:
            with database.connect() as conn:
                allowed_wells, _ = project_wells_with_filter(conn, snapshot)
                allowed = {row["project_key"] for row in allowed_wells}
                profile, _ = complete_las_profile_for_export(conn, snapshot)
                focus = curve_tools.focus_merge_payload(conn, snapshot, profile, type_key, allowed)
                if focus.get("type", {}).get("type_key") != type_key:
                    raise ValueError("无法读取该类型的聚焦方案")
                project_root = str(Path(snapshot["project"]["root"]).resolve())
                target_type = focus["type"]["name"] if focus.get("type") else type_key
                output_mnemonic = requested_output_mnemonic or target_type
                output_mnemonic = re.sub(r"[^\w\-]+", "_", output_mnemonic, flags=re.UNICODE).strip("_")
                if not output_mnemonic:
                    raise ValueError("最终目标曲线名不能为空或仅包含特殊字符")
                manifest_rows: list[dict[str, Any]] = []
                skipped: list[dict[str, Any]] = []
                used_names: dict[str, int] = {}
                archive_path = archive_target("curve_focus_export_") if output_dir is None else None
                with (zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) if output_dir is None else nullcontext()) as bundle:
                    for index, row in enumerate(focus.get("rows", []), 1):
                        selected = row.get("selected")
                        if not selected:
                            candidates = row.get("candidates") or []
                            if len(candidates) == 1:
                                candidate = candidates[0] if isinstance(candidates[0], dict) else None
                                if candidate:
                                    selected = candidate
                        if not selected:
                            skipped.append({
                                "well_key": row.get("well_key"),
                                "well_name": row.get("well_name"),
                                "reason": f"未选出可导出曲线（候选 {len(row.get('candidates') or [])} 条）",
                            })
                            continue
                        source_path = str(selected.get("source_path") or "").strip()
                        if not source_path:
                            fallback = conn.execute(
                                "SELECT file_path FROM project_catalog_items WHERE project_root=? AND category_key='well_logs' AND filename=? LIMIT 1",
                                (project_root, selected.get("filename")),
                            ).fetchone()
                            source_path = str(fallback["file_path"]) if fallback else ""
                        else:
                            candidate_path = Path(source_path)
                            if not candidate_path.is_absolute():
                                candidate_path = Path(project_root) / source_path
                                if candidate_path.is_file():
                                    source_path = str(candidate_path)
                        if not source_path:
                            skipped.append({
                                "well_key": row.get("well_key"),
                                "well_name": row.get("well_name"),
                                "reason": "未找到源文件路径",
                            })
                            continue
                        source = Path(source_path)
                        if not source.is_file():
                            fallback = conn.execute(
                                "SELECT file_path FROM project_catalog_items WHERE project_root=? AND category_key='well_logs' AND filename=? LIMIT 1",
                                (project_root, source.name),
                            ).fetchone()
                            if fallback:
                                source_path = str(fallback["file_path"])
                                source = Path(source_path)
                            if not fallback or not source.is_file():
                                source = Path(source_path) if source_path else source
                        if not source.is_file():
                            skipped.append({
                                "well_key": row.get("well_key"),
                                "well_name": row.get("well_name"),
                                "reason": "源文件不可访问",
                            })
                            continue
                        try:
                            with tempfile.NamedTemporaryFile(prefix="curve_focus_export_", suffix=".las", delete=False) as raw_temp:
                                temp_path = Path(raw_temp.name)
                            try:
                                meta, count = _build_focus_curve_las(source, selected["mnemonic"], output_mnemonic, temp_path)
                                archive_name = f"FocusMerge/{_las_curve_filename(row.get('well_name') or row.get('well_key') or f'well_{index:04d}', used_names, index)}"
                                output_file = archive_name.replace("FocusMerge/", "")
                                if output_dir is None:
                                    bundle.write(temp_path, arcname=archive_name)
                                else:
                                    destination = output_dir / output_file
                                    destination.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(temp_path, destination)
                                manifest_rows.append({
                                    "well_name": row.get("well_name") or row.get("well_key"),
                                    "well_key": row.get("well_key"),
                                    "mnemonic": selected["mnemonic"],
                                    "output_mnemonic": output_mnemonic,
                                    "curve_type": target_type,
                                    "source_file": source.name,
                                    "source_path": str(source),
                                    "output_file": output_file,
                                    "sample_rows": count,
                                    "depth_count": meta["depth_count"],
                                    "status": "ok",
                                    "reason": "ok",
                                })
                            except Exception as exc:
                                skipped.append({
                                    "well_name": row.get("well_name") or row.get("well_key"),
                                    "well_key": row.get("well_key"),
                                "mnemonic": selected.get("mnemonic") if isinstance(selected, dict) else None,
                                "output_mnemonic": output_mnemonic,
                                    "curve_type": target_type,
                                    "source_file": source.name,
                                    "source_path": str(source),
                                    "output_file": None,
                                    "sample_rows": "",
                                    "depth_count": "",
                                    "status": "failed",
                                    "reason": str(exc),
                                })
                            finally:
                                temp_path.unlink(missing_ok=True)
                        except Exception as exc:
                            skipped.append({
                                "well_name": row.get("well_name") or row.get("well_key"),
                                "well_key": row.get("well_key"),
                                "mnemonic": selected.get("mnemonic") if isinstance(selected, dict) else None,
                                "output_mnemonic": output_mnemonic,
                                "curve_type": target_type,
                                "source_file": source.name,
                                "source_path": str(source),
                                "output_file": None,
                                "sample_rows": "",
                                "depth_count": "",
                                "status": "failed",
                                "reason": str(exc),
                            })
                    for row in skipped:
                        if row.get("well_name") is None:
                            row["well_name"] = row.get("well_key")
                        row.setdefault("output_mnemonic", output_mnemonic)
                        if row.get("status") != "failed":
                            row["curve_type"] = target_type
                            row["sample_rows"] = ""
                            row["status"] = "skipped"
                        manifest_rows.append(row)

                    if output_dir is None:
                        bundle.writestr(
                            "FocusMerge/聚焦合并清单.csv",
                            _csv_bytes(
                                ["well_name", "well_key", "mnemonic", "output_mnemonic", "curve_type", "source_file", "source_path", "output_file", "sample_rows", "depth_count", "status", "reason"],
                                manifest_rows,
                            ),
                        )
                        bundle.writestr(
                            "FocusMerge/README.txt",
                            (
                                "地数镜 聚焦合并 LAS 导出\n\n"
                                "每口井导出 1 个 LAS，仅包含深度道与该井最终采用的曲线。\n"
                                "导出过程会优先使用“聚焦合并”里人工/系统最终方案。\n"
                            ),
                        )
                    else:
                        (output_dir / "FocusMerge_聚焦合并清单.csv").write_bytes(_csv_bytes(
                            ["well_name", "well_key", "mnemonic", "output_mnemonic", "curve_type", "source_file", "source_path", "output_file", "sample_rows", "depth_count", "status", "reason"],
                            manifest_rows,
                        ))
                        (output_dir / "FocusMerge_聚焦合并说明.txt").write_text(
                            "地数镜 聚焦合并 LAS 导出\n\n每口井导出 1 个 LAS，仅包含深度道与该井最终采用的曲线。\n"
                            "导出过程会优先使用“聚焦合并”里人工/系统最终方案。\n",
                            encoding="utf-8",
                        )

            if output_dir is None:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                return archive_response(archive_path, f"聚焦合并LAS_{target_type}_{stamp}.zip")
            return jsonify({
                "ok": True,
                "exported": sum(1 for row in manifest_rows if row.get("status") == "ok"),
                "skipped": len(skipped),
                "path": str(output_dir),
                "output_mnemonic": output_mnemonic,
                "manifest": manifest_rows,
            })
        except (OSError, ValueError, sqlite3.Error) as exc:
            if archive_path:
                archive_path.unlink(missing_ok=True)
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/layer-curve-coverage")
    def api_layer_curve_coverage():
        snapshot = snapshot_or_none()
        horizon_key = request.args.get("horizon_key")
        if not snapshot or not horizon_key:
            return jsonify({"error": "请选择已计算的层面"}), 400
        root = str(Path(snapshot["project"]["root"]).resolve())
        with database.connect() as conn:
            hits = {row["well_key"]: dict(row) for row in conn.execute("SELECT well_key,intersection_md,surface_z FROM horizon_well_hits WHERE project_root=? AND horizon_key=? AND hit=1", (root, horizon_key))}
        stats = [row for row in curve_tools.curve_statistics(curve_profile(snapshot)) if not curve_tools.is_time_depth_mnemonic(row["mnemonic"])]
        rows = []
        for curve in stats:
            curve_wells = set(curve["wells"])
            intersect = curve_wells & set(hits)
            depth_map = {row["well_key"]: row for row in curve["depths"]}
            covers_depth = [key for key in intersect if key in depth_map and hits[key]["intersection_md"] is not None and depth_map[key]["start"] <= hits[key]["intersection_md"] <= depth_map[key]["stop"]]
            rows.append({"mnemonic": curve["mnemonic"], "curve_wells": len(curve_wells), "horizon_wells": len(hits), "both_wells": len(intersect), "horizon_well_coverage": round(len(intersect) / max(1, len(hits)) * 100, 2), "depth_covers_horizon": len(covers_depth), "depth_coverage": round(len(covers_depth) / max(1, len(hits)) * 100, 2)})
        return jsonify({"horizon_key": horizon_key, "horizon_wells": len(hits), "curves": sorted(rows, key=lambda row: row["depth_coverage"], reverse=True)})

    @app.get("/api/interpretations")
    def api_interpretations():
        with database.connect() as conn:
            return jsonify(analytics.interpretation_statistics(conn, request.args.get("data_type")))

    @app.get("/api/interpretation-workbench")
    def api_interpretation_workbench():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        with database.connect() as conn:
            return jsonify(interpretation_tools.interpretation_workbench(conn, snapshot, curve_profile(snapshot)))

    @app.post("/api/interpretation-types")
    def api_create_interpretation_type():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                row = interpretation_tools.create_interpretation_type(
                    conn, root, str(payload["name"]), payload.get("role"), payload.get("color", "#37c8c2")
                )
            return jsonify(row)
        except (KeyError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/interpretation-types/assign")
    def api_assign_interpretation_type():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                interpretation_tools.assign_interpretation_type(
                    conn, root, str(payload["attribute_name"]), payload.get("type_key")
                )
            return jsonify({"ok": True})
        except (KeyError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/core-calibration")
    def api_core_calibration():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        with database.connect() as conn:
            wells, _ = project_wells_with_filter(conn, snapshot)
            return jsonify(interpretation_tools.core_calibration_payload(conn, snapshot, wells))

    @app.get("/api/core-media/<int:item_id>")
    def api_core_media(item_id: int):
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        root = str(Path(snapshot["project"]["root"]).resolve())
        with database.connect() as conn:
            row = conn.execute(
                "SELECT file_path,extension FROM project_catalog_items WHERE id=? AND project_root=?",
                (item_id, root),
            ).fetchone()
        if not row or row["extension"] not in interpretation_tools.IMAGE_EXTENSIONS or not Path(row["file_path"]).is_file():
            return jsonify({"error": "岩心图片不存在"}), 404
        return send_file(row["file_path"], conditional=True)

    @app.get("/api/seismic")
    def api_seismic():
        with database.connect() as conn:
            rows = analytics.seismic_statistics(conn)
            snapshot = snapshot_or_none()
            if not rows and snapshot:
                rows = []
                for dimension, item in (("3D", snapshot.get("seismic", {}).get("three_d")), ("2D", snapshot.get("seismic", {}).get("two_d"))):
                    if item:
                        bounds = item.get("coordinate_bounds") or {}
                        rows.append({**item, "dimension": dimension, "x_min": bounds.get("x_min"), "x_max": bounds.get("x_max"), "y_min": bounds.get("y_min"), "y_max": bounds.get("y_max"), "crs": snapshot.get("surface", {}).get("crs", {}).get("epsg"), "batch": "目录代表扫描", "version": None, "footprint_area": item.get("footprint_area")})
            return jsonify(rows)

    @app.get("/api/seismic-inventory")
    def api_seismic_inventory():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照，请先扫描或打开工区"}), 404
        root = Path(snapshot.get("project", {}).get("root") or "").resolve()
        if not root.is_dir():
            return jsonify({"error": f"项目目录当前不可访问：{root}"}), 400
        try:
            return jsonify(seismic_inventory_tools.build_inventory(root, snapshot))
        except (OSError, ValueError, struct.error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/seismic-inventory/analyze")
    def api_analyze_seismic_file():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照，请先扫描或打开工区"}), 404
        payload = request.get_json(silent=True) or {}
        path = str(payload.get("path") or "").strip()
        if not path:
            return jsonify({"error": "请选择需要完整解析的地震文件路径"}), 400
        root = Path(snapshot.get("project", {}).get("root") or "").resolve()
        try:
            return jsonify(seismic_inventory_tools.analyze_file(path, root))
        except (FileNotFoundError, OSError, ValueError, struct.error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/seismic-inventory/section")
    def api_seismic_section():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照，请先扫描或打开工区"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(seismic_section_tools.preview_section(
                payload.get("path") or "",
                snapshot["project"]["root"],
                axis=payload.get("axis"), value=payload.get("value"),
                max_traces=payload.get("max_traces", 160),
                max_samples=payload.get("max_samples", 500),
            ))
        except (KeyError, TypeError, ValueError, OSError, struct.error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/polygons")
    def api_polygons():
        with database.connect() as conn:
            rows = analytics.polygons(conn)
            snapshot = snapshot_or_none()
            if not rows and snapshot:
                rows = filter_tools.project_polygons(conn, snapshot)
            return jsonify(rows)

    @app.get("/api/well-map-layers")
    def api_well_map_layers():
        """Lazily prepare plan-view DEV paths and MD-referenced perforations."""
        limit = min(5000, max(1, request.args.get("limit", default=4000, type=int)))
        trajectories = []
        errors = []
        snapshot = snapshot_or_none()
        with database.connect() as conn:
            if snapshot:
                rows, filter_meta = project_wells_with_filter(conn, snapshot)
                candidates = [row for row in rows if row["_dev_paths"]]
                for row in candidates[:limit]:
                    path = sorted(row["_dev_paths"])[0]
                    try:
                        points = parse_dev_stations(path)
                        if len(points) < 2:
                            continue
                        stride = max(1, len(points) // 180)
                        trajectories.append({
                            "well_key": row["project_key"], "well_id": row["id"],
                            "well_name": row["canonical_name"], "source": path,
                            "points": points[::stride] + ([] if (len(points) - 1) % stride == 0 else [points[-1]]),
                        })
                    except (OSError, ValueError) as exc:
                        errors.append({"well": row["canonical_name"], "error": str(exc)})
            else:
                filter_meta = {"active": False}
            imported = analytics.wells(conn)
            existing_keys = {row["well_key"] for row in trajectories}
            for row in imported:
                if row["normalized_key"] in existing_keys or len(trajectories) >= limit:
                    continue
                stations = analytics.trajectory(conn, int(row["id"]))
                if len(stations) < 2:
                    continue
                x0, y0 = row.get("x"), row.get("y")
                if x0 is None or y0 is None:
                    continue
                points = [{"md": point["md"], "x": x0 + point["easting"], "y": y0 + point["northing"], "z": point.get("z_msl")} for point in stations]
                stride = max(1, len(points) // 180)
                trajectories.append({"well_key": row["normalized_key"], "well_id": row["id"], "well_name": row["canonical_name"], "source": "已入库 DEV", "points": points[::stride]})
            intervals = [dict(row) for row in conn.execute(
                """SELECT pi.*,s.filename,s.batch,s.version FROM production_intervals pi
                   JOIN sources s ON s.id=pi.source_id ORDER BY pi.well_key,pi.top_md"""
            )]
        return jsonify({
            "trajectories": trajectories,
            "perforations": intervals,
            "trajectory_count": len(trajectories),
            "perforation_count": len(intervals),
            "limit": limit,
            "truncated": bool(snapshot and len(candidates) > limit),
            "filter": filter_meta,
            "errors": errors[:30],
            "method": "每口井按需读取一份 DEV；射孔顶/底 MD 在线性插值后投影到轨迹",
        })

    @app.post("/api/production/series")
    def api_production_series():
        """Return one selected well's chart-ready OFM series on demand."""
        payload = request.get_json(silent=True) or {}
        well_key = normalize_well_name(str(payload.get("well") or ""))
        if not well_key:
            return jsonify({"error": "请选择或输入需要查看的生产井"}), 400
        requested_fields = payload.get("fields") or ["oil_rate", "water_rate", "pressure"]
        if not isinstance(requested_fields, list):
            return jsonify({"error": "绘图字段格式不正确"}), 400
        valid_fields = {row["key"] for row in production_tools.PRODUCTION_SERIES_FIELDS}
        fields = [str(field) for field in requested_fields if str(field) in valid_fields]
        if not fields:
            return jsonify({"error": "请至少选择一个可绘制的生产字段"}), 400
        x_axis = str(payload.get("x_axis") or "date")
        valid_axes = {row["key"] for row in production_tools.PRODUCTION_X_AXES}
        if x_axis not in valid_axes:
            return jsonify({"error": "未知横坐标"}), 400
        start, end = str(payload.get("start") or "").strip(), str(payload.get("end") or "").strip()
        try:
            with database.connect() as conn:
                mode, correction = production_source(conn, payload.get("source_mode"))
                rows = production_monthly_rows(conn, mode, correction, [well_key])
                events = [dict(row) for row in conn.execute(
                    """SELECT pe.*,s.filename,s.batch,s.version FROM production_events pe
                       JOIN sources s ON s.id=pe.source_id
                       WHERE pe.well_key=? ORDER BY pe.event_date,pe.id""", (well_key,)
                )]
                intervals = [dict(row) for row in conn.execute(
                    """SELECT pi.*,s.filename,s.batch,s.version FROM production_intervals pi
                       JOIN sources s ON s.id=pi.source_id
                       WHERE pi.well_key=? ORDER BY pi.top_md,pi.base_md""", (well_key,)
                )]
                ofm_well = conn.execute(
                    "SELECT * FROM ofm_wells WHERE well_key=? ORDER BY id DESC LIMIT 1", (well_key,)
                ).fetchone()
        except sqlite3.Error as exc:
            return jsonify({"error": str(exc)}), 400
        series = production_tools.build_production_series(rows)
        if start:
            series = [row for row in series if str(row.get("date") or "") >= start]
        if end:
            series = [row for row in series if str(row.get("date") or "") <= end]
        fit_metric = str(payload.get("fit_metric") or "")
        fit = production_tools.fit_hyperbolic_decline(series, fit_metric) if fit_metric in valid_fields else None
        summary_rows = production_tools.summarize_production(rows, events)
        summary = summary_rows[0] if summary_rows else None
        return jsonify({
            "well_key": well_key,
            "well_name": (summary or {}).get("well_name") or (dict(ofm_well).get("well_name") if ofm_well else well_key),
            "series": series, "events": events, "perforations": intervals,
            "fields": production_tools.PRODUCTION_SERIES_FIELDS,
            "x_axes": production_tools.PRODUCTION_X_AXES,
            "selected_fields": fields, "x_axis": x_axis,
            "fit": fit, "summary": summary,
            "source_mode": mode,
            "source_label": "校正 MDB" if mode == "corrected" else "原始 MDB",
            "production_formation": (dict(ofm_well).get("zone_name") if ofm_well else None) or "/".join(dict.fromkeys(str(row.get("interval_name")) for row in intervals if row.get("interval_name"))) or None,
            "range": {"start": start or None, "end": end or None},
            "method": ("校正 MDB：基础日率 × 生产天数重建月产与累产；仅按需读取当前井。" if mode == "corrected" else "原始 MDB：仅查询当前井并按日期合并；保留原始派生值。"),
        })

    @app.get("/api/production")
    def api_production():
        snapshot = snapshot_or_none()
        selected_well = normalize_well_name(request.args.get("well")) if request.args.get("well") else None
        with database.connect() as conn:
            mode, correction = production_source(conn, request.args.get("source"))
            intervals = [dict(row) for row in conn.execute(
                """SELECT pi.*,s.filename,s.batch,s.version FROM production_intervals pi
                   JOIN sources s ON s.id=pi.source_id ORDER BY pi.well_key,pi.top_md"""
            )]
            files = []
            if snapshot:
                root = str(Path(snapshot["project"]["root"]).resolve())
                files = [dict(row) for row in conn.execute(
                    """SELECT id,filename,relative_path,file_path,bytes,representative
                       FROM project_catalog_items WHERE project_root=? AND category_key='production'
                       ORDER BY representative DESC,relative_path LIMIT 200""", (root,)
                )]
            if mode == "corrected" and correction:
                monthly_record_count = int(conn.execute("SELECT COUNT(*) FROM production_corrected_monthly WHERE run_id=?", (correction["id"],)).fetchone()[0])
                dynamic_wells = int(conn.execute(
                    "SELECT COUNT(*) FROM (SELECT well_key FROM production_corrected_monthly WHERE run_id=? UNION SELECT well_key FROM production_events)",
                    (correction["id"],),
                ).fetchone()[0])
            else:
                monthly_record_count = int(conn.execute("SELECT COUNT(*) FROM production_monthly").fetchone()[0])
                dynamic_wells = int(conn.execute(
                    "SELECT COUNT(*) FROM (SELECT well_key FROM production_monthly UNION SELECT well_key FROM production_events)"
                ).fetchone()[0])
            monthly = production_monthly_rows(conn, mode, correction, [selected_well]) if selected_well else []
            events = [dict(row) for row in conn.execute(
                """SELECT pe.*,s.filename,s.batch,s.version FROM production_events pe
                   JOIN sources s ON s.id=pe.source_id ORDER BY pe.well_key,pe.event_date,pe.id"""
            )]
            summaries = _production_dashboard_summaries(conn, events, int(correction["id"]) if mode == "corrected" and correction else None)
            known_wells = project_wells(conn, snapshot) if snapshot else analytics.wells(conn)
            alias_map = {row["alias_key"]: row["canonical_key"] for row in conn.execute("SELECT alias_key,canonical_key FROM alias_rules")}
            if snapshot:
                project_root = str(Path(snapshot["project"]["root"]).resolve())
                alias_map.update({row["alias_key"]: row["canonical_key"] for row in conn.execute(
                    "SELECT alias_key,canonical_key FROM project_well_aliases WHERE project_root=?", (project_root,)
                )})
            reference_wells = [row for row in known_wells if any(source in row.get("source_types", []) for source in ("well_head", "deviation", "las"))]
            by_key = {str(row.get("project_key") or row.get("normalized_key")): row for row in reference_wells}
            # Do not compare every OFM well name with every project well on page
            # load.  A 2,000+ LAS project and an 800+ well OFM history used to
            # perform millions of SequenceMatcher operations here.  The numeric
            # well identifier is also the strongest safe candidate key for names
            # such as ``WELL-1040H`` / ``W-1040``. Broad fuzzy matching remains a
            # user-driven action in the well-identity view.
            reference_by_number: dict[str, list[dict]] = {}
            for reference in reference_wells:
                for number in set(re.findall(r"\d+", normalize_well_name(reference.get("canonical_name") or ""))):
                    reference_by_number.setdefault(number, []).append(reference)
            for summary in summaries:
                resolved_key = alias_map.get(summary["well_key"], summary["well_key"])
                matched = by_key.get(resolved_key)
                suggestion = None
                candidates = []
                if not matched:
                    for number in set(re.findall(r"\d+", normalize_well_name(summary["well_name"]))):
                        candidates.extend(reference_by_number.get(number, []))
                if not matched and candidates:
                    # Several numeric tokens can lead to the same well.  Keep
                    # only one copy before scoring so a multi-segment well name
                    # cannot alter the outcome or the response time.
                    candidates = list({str(row.get("project_key") or row.get("normalized_key")): row for row in candidates}.values())
                    candidate = max(candidates, key=lambda row: identity_similarity(summary["well_name"], row["canonical_name"]))
                    score = identity_similarity(summary["well_name"], candidate["canonical_name"])
                    if score >= 0.78:
                        suggestion = {"well_key": candidate.get("project_key") or candidate.get("normalized_key"), "well_name": candidate["canonical_name"], "score": round(score, 3)}
                summary.update({
                    "match_status": "matched" if matched else "suggested" if suggestion else "production_only",
                    "matched_well": matched["canonical_name"] if matched else None,
                    "match_suggestion": suggestion,
                    "hard_evidence": True,
                })
            selected_series = monthly
            selected_events = [row for row in events if row["well_key"] == selected_well] if selected_well else []
            ofm_sources = [dict(row) for row in conn.execute(
                """SELECT os.id,os.source_id,os.file_path,os.file_size,os.modified_at,os.driver,
                          os.table_count,os.nonempty_table_count,os.total_rows,os.mounted_at,os.warning,
                          s.filename,s.status,s.metadata_json
                   FROM ofm_sources os JOIN sources s ON s.id=os.source_id
                   ORDER BY os.id DESC"""
            )]
            for row in ofm_sources:
                try:
                    details = json.loads(row.pop("metadata_json") or "{}")
                    row["mapping"] = details.get("mapped", {})
                    row["profile"] = details.get("profile", [])
                except (TypeError, ValueError):
                    row["mapping"] = {}
                    row["profile"] = []
                row["available"] = Path(row["file_path"]).is_file()
            ofm_tables = [dict(row) for row in conn.execute(
                """SELECT tc.id,tc.ofm_source_id,tc.table_name,tc.category,tc.row_count,
                          tc.columns_json,tc.sample_json
                   FROM ofm_table_catalog tc ORDER BY tc.ofm_source_id DESC,tc.table_name"""
            )]
            keyword_rows = []
            for row in ofm_tables:
                row["columns"] = json.loads(row.pop("columns_json") or "[]")
                row["sample"] = json.loads(row.pop("sample_json") or "[]")
                row["explanation"] = ofm_tools.table_explanation(row["table_name"], row["row_count"])
                for column in row["columns"]:
                    column["explanation"] = ofm_tools.column_explanation(row["table_name"], column.get("name") or "")
                    keyword_rows.append({
                        "ofm_source_id": row["ofm_source_id"], "table": row["table_name"],
                        "category": row["category"], "field": column.get("name"),
                        "type": column.get("type"), "size": column.get("size"),
                        "nullable": column.get("nullable"), "explanation": column["explanation"],
                    })
            ofm_wells = [dict(row) for row in conn.execute(
                "SELECT * FROM ofm_wells ORDER BY well_name"
            )]
            ofm_deviation_count = int(conn.execute("SELECT COUNT(*) FROM ofm_deviation_stations").fetchone()[0])
            ofm_marker_count = int(conn.execute("SELECT COUNT(*) FROM ofm_markers").fetchone()[0])
            ofm_by_key = {row["well_key"]: row for row in ofm_wells}
            formation_by_key: dict[str, list[str]] = {}
            for interval in intervals:
                if interval.get("interval_name"):
                    formation_by_key.setdefault(interval["well_key"], []).append(interval["interval_name"])
            for summary in summaries:
                ofm_well = ofm_by_key.get(summary["well_key"], {})
                formations = [value for value in [ofm_well.get("zone_name"), *formation_by_key.get(summary["well_key"], [])] if value]
                summary.update({
                    "x": ofm_well.get("x"), "y": ofm_well.get("y"),
                    "production_formation": " / ".join(dict.fromkeys(formations)) or None,
                    "interest": ofm_well.get("interest"),
                    "cumulative_oil_kbbl": summary.get("cumulative_oil") / 1000 if summary.get("cumulative_oil") is not None else None,
                    "cumulative_water_kbbl": summary.get("cumulative_water") / 1000 if summary.get("cumulative_water") is not None else None,
                    "cumulative_liquid_kbbl": (
                        (summary.get("cumulative_oil") + summary.get("cumulative_water")) / 1000
                        if summary.get("cumulative_oil") is not None and summary.get("cumulative_water") is not None else None
                    ),
                    "cumulative_gas_raw": summary.get("cumulative_gas"),
                })
        return jsonify({
            "files": files,
            "file_count": int(next((row["files"] for row in (snapshot or {}).get("categories", []) if row["key"] == "production"), len(files))),
            "perforations": intervals,
            "perforation_count": len(intervals),
            "perforated_wells": len({row["well_key"] for row in intervals}),
            "monthly_record_count": monthly_record_count,
            "dynamic_wells": dynamic_wells,
            "event_count": len(events),
            "well_summaries": summaries,
            "series": selected_series,
            "events": selected_events,
            "selected_well": selected_well,
            "ofm_sources": ofm_sources,
            "ofm_tables": ofm_tables,
            "ofm_keywords": keyword_rows,
            "ofm_wells": ofm_wells,
            "ofm_deviation_count": ofm_deviation_count,
            "ofm_marker_count": ofm_marker_count,
            "field_contract": production_tools.PRODUCTION_FIELD_CONTRACT,
            "production_source": {
                "mode": mode,
                "label": "校正 MDB（聚类当前数据源）" if mode == "corrected" else "原始 MDB",
                "corrected_available": bool(correction),
                "corrected_mdb_path": correction.get("corrected_mdb_path") if correction else None,
                "run_id": correction.get("id") if correction else None,
                "completed_at": correction.get("completed_at") if correction else None,
            },
            "identity_rule": "生产动态是井实体存在的硬证据；精确名/已确认别名自动关联，模糊名只给候选、不得自动合并",
            "depth_reference": "MD",
        })

    @app.get("/api/production/correction")
    def api_production_correction_status():
        query = str(request.args.get("q") or "").strip()
        changed_only = str(request.args.get("changed_only") or "0").lower() in {"1", "true", "yes"}
        try:
            limit = max(20, min(1000, int(request.args.get("limit") or 300)))
            offset = max(0, int(request.args.get("offset") or 0))
        except (TypeError, ValueError):
            return jsonify({"error": "分页参数格式不正确"}), 400
        with database.connect() as conn:
            correction = active_production_correction(conn)
            if not correction:
                source = conn.execute("SELECT os.source_id,os.file_path,s.filename FROM ofm_sources os JOIN sources s ON s.id=os.source_id ORDER BY os.id DESC LIMIT 1").fetchone()
                return jsonify({"available": False, "source": dict(source) if source else None, "audit": [], "field_stats": []})
            clauses, params = ["run_id=?"], [int(correction["id"])]
            if query:
                clauses.append("(UPPER(well_name) LIKE ? OR UPPER(field_label) LIKE ?)")
                pattern = f"%{query.upper()}%"
                params.extend([pattern, pattern])
            if changed_only:
                clauses.append("changed=1")
            where = " AND ".join(clauses)
            total = int(conn.execute(f"SELECT COUNT(*) FROM production_correction_audit WHERE {where}", params).fetchone()[0])
            audit = [dict(row) for row in conn.execute(
                f"SELECT * FROM production_correction_audit WHERE {where} ORDER BY changed DESC,well_name,field_label LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )]
            field_stats = [dict(row) for row in conn.execute(
                """SELECT field_key,field_label,unit,COUNT(*) wells,SUM(changed) corrected
                   FROM production_correction_audit WHERE run_id=? GROUP BY field_key,field_label,unit
                   ORDER BY corrected DESC,field_label""", (correction["id"],)
            )]
        return jsonify({"available": True, "run": correction, "audit": audit, "field_stats": field_stats, "total": total, "limit": limit, "offset": offset})

    @app.post("/api/production/correction")
    def api_start_production_correction():
        if not workspace_context["path"]:
            return jsonify({"error": "请先打开或保存 .nvt 工区，再执行动态库校正"}), 400
        job = correction_manager.submit(database.path, workspace_context["path"])
        return jsonify(job), 202

    @app.get("/api/production/correction/jobs/<job_id>")
    def api_production_correction_job(job_id: str):
        try:
            return jsonify(correction_manager.status(job_id))
        except KeyError:
            return jsonify({"error": "校正任务不存在或软件已重新启动"}), 404

    def production_clustering_dataset(conn: sqlite3.Connection) -> dict:
        mode, correction = production_source(conn)
        def table_marker(table: str, where: str = "", params: tuple = ()) -> tuple[int, int]:
            row = conn.execute(
                f"SELECT COUNT(*) AS row_count,COALESCE(MAX(id),0) AS max_id FROM {table} {where}", params,
            ).fetchone()
            return int(row["row_count"]), int(row["max_id"])

        correction_id = int(correction["id"]) if mode == "corrected" and correction else None
        monthly_marker = table_marker(
            "production_corrected_monthly", "WHERE run_id=?", (correction_id,),
        ) if correction_id is not None else table_marker("production_monthly")
        signature = json.dumps({
            "schema": 5, "mode": mode, "correction_id": correction_id,
            "monthly": monthly_marker, "events": table_marker("production_events"),
            "intervals": table_marker("production_intervals"), "ofm_wells": table_marker("ofm_wells"),
            "wells": table_marker("wells"), "well_sources": table_marker("well_sources"),
        }, sort_keys=True, separators=(",", ":"))

        def cached_dataset() -> dict | None:
            row = conn.execute(
                "SELECT payload_json,built_at FROM production_clustering_feature_cache WHERE cache_key='active' AND source_signature=?",
                (signature,),
            ).fetchone()
            if not row:
                return None
            try:
                value = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                return None
            if not isinstance(value, dict) or not isinstance(value.get("wells"), list):
                return None
            value["performance"] = {"feature_cache": "hit", "built_at": row["built_at"]}
            return value

        dataset = cached_dataset()
        if dataset is not None:
            return dataset

        # Only one request builds this materialized feature set at a time.
        with clustering_cache_lock:
            dataset = cached_dataset()
            if dataset is not None:
                return dataset
            monthly = production_monthly_rows(conn, mode, correction)
            events = [dict(row) for row in conn.execute(
                "SELECT * FROM production_events ORDER BY well_key,event_date,id"
            )]
            intervals = [dict(row) for row in conn.execute(
                "SELECT * FROM production_intervals ORDER BY well_key,top_md,id"
            )]
            ofm_wells = [dict(row) for row in conn.execute(
                "SELECT * FROM ofm_wells ORDER BY well_key,id"
            )]
            # Standalone CSV imports may have coordinates in the unified well table
            # without a corresponding OFM master-well row.
            known = {row["well_key"]: row for row in ofm_wells}
            for row in conn.execute("SELECT normalized_key,canonical_name,x,y FROM wells ORDER BY id"):
                if row["normalized_key"] in known:
                    target = known[row["normalized_key"]]
                    if target.get("x") is None and row["x"] is not None:
                        target["x"] = row["x"]
                    if target.get("y") is None and row["y"] is not None:
                        target["y"] = row["y"]
                else:
                    ofm_wells.append({
                        "well_key": row["normalized_key"], "well_name": row["canonical_name"],
                        "x": row["x"], "y": row["y"],
                    })
            dataset = clustering_tools.build_feature_dataset(monthly, events, intervals, ofm_wells)
            feature_by_key = {row["well_key"]: row for row in dataset.get("wells", [])}
            map_wells = []
            seen_map_keys = set()
            for source in ofm_wells:
                key = str(source.get("well_key") or source.get("normalized_key") or "")
                if not key or key in seen_map_keys:
                    continue
                seen_map_keys.add(key)
                feature = feature_by_key.get(key) or {}
                map_wells.append({
                    "well_key": key,
                    "well_name": source.get("well_name") or source.get("canonical_name") or feature.get("well_name") or key,
                    "x": source.get("x") if source.get("x") is not None else source.get("surface_x"),
                     "y": source.get("y") if source.get("y") is not None else source.get("surface_y"),
                     "record_count": int(feature.get("record_count") or 0),
                     "has_production": bool(feature.get("record_count")),
                     "formations": list(feature.get("formations") or []),
                 })
            dataset["map_wells"] = map_wells
        if mode == "corrected" and correction:
            selected_path = str(correction.get("corrected_mdb_path") or "")
            dataset["data_source"] = {
                "mode": "corrected",
                "selected": True,
                "verified": True,
                "label": "生产动态校正 MDB",
                "filename": Path(selected_path).name or "生产动态_校正.mdb",
                "path": selected_path,
                "corrected_mdb_path": selected_path,
                "source_path": correction.get("source_path"),
                "run_id": correction.get("id"),
                "completed_at": correction.get("completed_at"),
                "well_count": correction.get("well_count"),
                "record_count": correction.get("record_count"),
                "corrected_well_count": correction.get("corrected_well_count"),
                "corrected_value_count": correction.get("corrected_value_count"),
                "inferred_days_count": correction.get("inferred_days_count"),
                "scope": "01 数据体检至 09 二维展示",
                "message": "本产能分型流程已锁定读取该校正快照，后续指标、训练、评价和成果不会回退到原始 MDB。",
            }
        else:
            original = conn.execute(
                """SELECT s.id source_id,s.filename,s.file_path,s.imported_at,s.record_count
                   FROM sources s
                   WHERE EXISTS(SELECT 1 FROM production_monthly pm WHERE pm.source_id=s.id)
                   ORDER BY s.id DESC LIMIT 1"""
            ).fetchone()
            original = dict(original) if original else {}
            selected_path = str(original.get("file_path") or "")
            dataset["data_source"] = {
                "mode": "original",
                "selected": True,
                "verified": False,
                "label": "原始生产 MDB",
                "filename": original.get("filename") or (Path(selected_path).name if selected_path else "尚未识别生产 MDB"),
                "path": selected_path or None,
                "corrected_mdb_path": None,
                "source_id": original.get("source_id"),
                "completed_at": None,
                "well_count": len({row.get("well_key") for row in monthly if row.get("well_key")}),
                "record_count": len(monthly),
                "scope": "01 数据体检至 09 二维展示",
                "message": "当前没有可用的校正 MDB，生产聚类仍在读取原始数据。请先到生产动态执行校正。",
            }
        built_at = datetime.now().astimezone().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO production_clustering_feature_cache(cache_key,source_signature,payload_json,well_count,built_at)
               VALUES('active',?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
               source_signature=excluded.source_signature,payload_json=excluded.payload_json,
               well_count=excluded.well_count,built_at=excluded.built_at""",
            (signature, json.dumps(dataset, ensure_ascii=False, separators=(",", ":")),
             len(dataset.get("wells") or []), built_at),
        )
        conn.commit()
        dataset["performance"] = {"feature_cache": "rebuilt", "built_at": built_at}
        return dataset

    @app.get("/api/production-clustering")
    def api_production_clustering():
        """Build an auditable, per-well feature inventory for the workflow UI."""
        with database.connect() as conn:
            dataset = production_clustering_dataset(conn)
        wells = dataset["wells"]
        available = [row for row in dataset["indicators"] if row["available"]]
        selected = [row for row in available if row["selected"]]
        sufficiently_long = sum(row["record_count"] >= 12 for row in wells)
        dataset["readiness"] = {
            "well_count": len(wells),
            "map_well_count": len(dataset.get("map_wells") or []),
            "training_candidates": sum(row["record_count"] >= 3 for row in wells),
            "long_series_wells": sufficiently_long,
            "available_indicators": len(available),
            "default_indicators": len(selected),
            "coordinate_wells": sum(
                row.get("x") is not None and row.get("y") is not None
                for row in dataset.get("map_wells") or []
            ),
            "formation_wells": sum(bool(row["formations"]) for row in wells),
            "intervention_wells": sum(row["has_intervention"] for row in wells),
            "transformer_ready": sufficiently_long >= 50,
            "transformer_reason": "至少建议50口井各有12个以上有效生产期；当前不满足时仅保留网络结构接口。",
        }
        dataset["workflow"] = [
            {"key": "inventory", "label": "数据体检"}, {"key": "indicators", "label": "指标筛选"},
            {"key": "dataset", "label": "数据集"}, {"key": "model", "label": "模型设计"},
            {"key": "preflight", "label": "训练自检"}, {"key": "training", "label": "模型训练"},
            {"key": "evaluation", "label": "批量评价"}, {"key": "results", "label": "成果汇总"},
            {"key": "map", "label": "二维展示"},
        ]
        return jsonify(dataset)

    def load_saved_clustering_folder(folder: Path, fallback_id: str) -> dict | None:
        """Read both current and pre-index clustering exports from one folder."""
        configuration_path = folder / "训练配置.json"
        summary_path = folder / "训练摘要.json"
        rows_path = folder / "井产能分类结果.csv"
        profiles_path = folder / "类别指标统计.csv"
        full_result_path = folder / "完整训练结果.json"
        if not configuration_path.is_file() and not summary_path.is_file():
            return None

        def read_json(path: Path) -> dict:
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                return value if isinstance(value, dict) else {}
            except (OSError, UnicodeError, ValueError):
                return {}

        def number(value: Any) -> float | None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if math.isfinite(parsed) else None

        configuration = read_json(configuration_path)
        summary = read_json(summary_path)
        result = read_json(full_result_path)
        definitions = {row["key"]: row for row in clustering_tools.INDICATORS}
        labels = {row["label"]: row for row in clustering_tools.INDICATORS}
        selected = list(summary.get("selected_indicators") or configuration.get("indicators") or [])
        category_order = list(summary.get("category_order") or [])
        category_meta = {str(row.get("category")): row for row in category_order if row.get("category")}

        if not result.get("ready") and rows_path.is_file():
            result_rows = []
            try:
                with rows_path.open("r", newline="", encoding="utf-8-sig") as stream:
                    for source in csv.DictReader(stream):
                        category = str(source.get("产能类别") or "")
                        meta = category_meta.get(category, {})
                        rank = int(number(source.get("相对级次")) or meta.get("rank") or 0)
                        cluster = int(number(meta.get("cluster")) or max(0, rank - 1))
                        level = str(meta.get("level") or ("相对较好" if rank == 1 else "相对偏弱" if rank == len(category_order) else "中间过渡"))
                        metrics = []
                        for key in selected:
                            definition = definitions.get(key, {"label": key, "unit": ""})
                            metrics.append({
                                "key": key, "label": definition.get("label", key),
                                "unit": definition.get("unit", ""), "value": number(source.get(key)),
                                "grade": "—",
                            })
                        name = str(source.get("井名") or "").strip()
                        result_rows.append({
                            "well_key": normalize_well_name(name), "well_name": name,
                            "scope": str(source.get("范围") or "评价井"), "cluster": cluster,
                            "category": category, "category_rank": rank, "category_level": level,
                            "formations": [value.strip() for value in str(source.get("开发层位") or "").split("/") if value.strip()],
                            "has_intervention": False, "metrics": metrics,
                            "x": number(source.get("X")), "y": number(source.get("Y")),
                            "x_plot": None, "y_plot": None,
                        })
            except (OSError, UnicodeError, csv.Error):
                result_rows = []

            profiles_by_category: dict[str, dict] = {}
            if profiles_path.is_file():
                try:
                    with profiles_path.open("r", newline="", encoding="utf-8-sig") as stream:
                        for source in csv.DictReader(stream):
                            category = str(source.get("类别") or "")
                            meta = category_meta.get(category, {})
                            rank = int(number(source.get("相对级次")) or meta.get("rank") or 0)
                            cluster = int(number(meta.get("cluster")) or max(0, rank - 1))
                            profile = profiles_by_category.setdefault(category, {
                                "cluster": cluster, "category": category, "rank": rank,
                                "count": int(number(source.get("样本数")) or 0), "metrics": [],
                            })
                            definition = labels.get(str(source.get("指标") or ""), {})
                            key = definition.get("key")
                            if not key:
                                position = len(profile["metrics"])
                                key = selected[position] if position < len(selected) else str(source.get("指标") or "")
                            profile["metrics"].append({
                                "key": key, "label": str(source.get("指标") or definition.get("label") or key),
                                "unit": str(source.get("单位") or definition.get("unit") or ""),
                                "min": number(source.get("最小值")), "q1": number(source.get("Q1")),
                                "median": number(source.get("中值")), "q3": number(source.get("Q3")),
                                "max": number(source.get("最大值")),
                            })
                except (OSError, UnicodeError, csv.Error):
                    profiles_by_category = {}
            counts = {category: sum(row["category"] == category for row in result_rows) for category in category_meta}
            result = {
                **summary, "ready": bool(result_rows), "selected_indicators": selected,
                "results": result_rows, "cluster_profiles": list(profiles_by_category.values()),
                "cluster_counts": counts, "category_order": category_order,
                "loss_history": [], "training_samples": [],
                "projection": {"method": "历史方案（原导出未保存投影点）", "x_label": "—", "y_label": "—"},
            }

        plan_name = str(configuration.get("plan_name") or folder.name).strip()[:120] or folder.name
        plan_id = str(configuration.get("plan_id") or fallback_id)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", plan_id):
            plan_id = fallback_id
        saved_at = str(configuration.get("saved_at") or "")
        if not saved_at:
            try:
                saved_at = datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            except OSError:
                saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return {
            "id": plan_id, "name": plan_name, "configuration": configuration,
            "result": result, "created_at": saved_at, "updated_at": saved_at,
        }

    def import_unindexed_clustering_plans(root: Path, conn: sqlite3.Connection) -> list[dict]:
        """Index scheme folders produced before workspace-backed plan persistence existed."""
        plans_root = root / "生产聚类"
        if not plans_root.is_dir():
            return []
        existing_rows = conn.execute("SELECT plan_id,plan_name FROM production_clustering_plans").fetchall()
        known_ids = {str(row["plan_id"]) for row in existing_rows}
        known_names = {str(row["plan_name"]).casefold() for row in existing_rows}
        imported = []
        for index, folder in enumerate(sorted((path for path in plans_root.iterdir() if path.is_dir()), key=lambda path: path.name), 1):
            # Folders created by the current save flow use the plan name.  Skip
            # them before reading the (potentially large) full-result JSON.
            # This keeps every later module mount from re-parsing exports that
            # are already indexed in the workspace database.
            if folder.name.casefold() in known_names:
                continue
            candidate = load_saved_clustering_folder(folder, f"legacy-{index:03d}")
            if not candidate or candidate["id"] in known_ids or candidate["name"].casefold() in known_names:
                continue
            conn.execute(
                """INSERT INTO production_clustering_plans(plan_id,plan_name,configuration_json,result_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (candidate["id"], candidate["name"], json.dumps(candidate["configuration"], ensure_ascii=False),
                 json.dumps(candidate["result"], ensure_ascii=False), candidate["created_at"], candidate["updated_at"]),
            )
            known_ids.add(candidate["id"])
            known_names.add(candidate["name"].casefold())
            imported.append(candidate)
        if imported:
            active = conn.execute(
                "SELECT value_json FROM workspace_settings WHERE setting_key='production_clustering_active_plan'"
            ).fetchone()
            if not active:
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                conn.execute(
                    "INSERT INTO workspace_settings(setting_key,value_json,updated_at) VALUES(?,?,?)",
                    ("production_clustering_active_plan", json.dumps(imported[0]["id"], ensure_ascii=False), timestamp),
                )
            conn.commit()
        return imported

    @app.get("/api/production-clustering/state")
    def api_production_clustering_state():
        """Restore saved schemes and map groups from the active .nvt database."""
        root = Path(workspace_context["path"] or Path(database.path).parent)
        compact = str(request.args.get("compact") or "").strip().lower() in {"1", "true", "yes"}
        with database.connect() as conn:
            imported = import_unindexed_clustering_plans(root, conn)
            if compact:
                plan_rows = [dict(row) for row in conn.execute(
                    """SELECT plan_id,plan_name,configuration_json,created_at,updated_at,
                              COALESCE(json_extract(result_json,'$.ready'),0) result_ready,
                              json_extract(result_json,'$.model') result_model,
                              json_extract(result_json,'$.model_type') result_model_type,
                              json_extract(result_json,'$.cluster_count') result_cluster_count,
                              json_extract(result_json,'$.training_sample_count') result_training_sample_count,
                              json_extract(result_json,'$.silhouette') result_silhouette
                       FROM production_clustering_plans
                       ORDER BY updated_at DESC,plan_id"""
                )]
            else:
                plan_rows = [dict(row) for row in conn.execute(
                    "SELECT * FROM production_clustering_plans ORDER BY updated_at DESC,plan_id"
                )]
            group_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM production_clustering_well_groups ORDER BY created_at,group_id"
            )]
            active_row = conn.execute(
                "SELECT value_json FROM workspace_settings WHERE setting_key='production_clustering_active_plan'"
            ).fetchone()
        groups = []
        for row in group_rows:
            try:
                well_keys = json.loads(row["well_keys_json"] or "[]")
            except (TypeError, ValueError):
                well_keys = []
            groups.append({
                "id": row["group_id"], "name": row["group_name"],
                "well_keys": list(dict.fromkeys(str(value) for value in well_keys if value)),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            })
        try:
            active_plan_id = json.loads(active_row["value_json"]) if active_row else None
        except (TypeError, ValueError):
            active_plan_id = None
        plan_ids = {str(row["plan_id"]) for row in plan_rows}
        if active_plan_id not in plan_ids:
            active_plan_id = str(plan_rows[0]["plan_id"]) if plan_rows else None
        plans = []
        for row in plan_rows:
            try:
                configuration = json.loads(row["configuration_json"] or "{}")
                result = (
                    {} if compact
                    else json.loads(row["result_json"] or "{}")
                )
            except (TypeError, ValueError):
                configuration, result = {}, {}
            summary = None
            has_result = bool(result.get("ready"))
            if compact:
                has_result = bool(row.get("result_ready"))
                summary = {
                    "ready": has_result,
                    "model": row.get("result_model") or row.get("result_model_type"),
                    "model_type": row.get("result_model_type"),
                    "cluster_count": row.get("result_cluster_count"),
                    "training_sample_count": row.get("result_training_sample_count"),
                    "silhouette": row.get("result_silhouette"),
                }
            plans.append({
                "id": row["plan_id"], "name": row["plan_name"],
                "configuration": configuration, "result": result,
                "result_summary": summary, "has_result": has_result,
                "result_deferred": bool(compact and has_result),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            })
        return jsonify({
            "plans": plans, "well_groups": groups, "active_plan_id": active_plan_id,
            "workspace": str(root), "migrated_plan_count": len(imported),
        })

    @app.get("/api/production-clustering/state/plans/<plan_id>")
    def api_production_clustering_saved_plan(plan_id: str):
        """Load one saved result only when the user activates or compares it."""
        with database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM production_clustering_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
        if not row:
            return jsonify({"error": "没有找到该产能分型方案"}), 404
        row = dict(row)
        try:
            configuration = json.loads(row["configuration_json"] or "{}")
            result = json.loads(row["result_json"] or "{}")
        except (TypeError, ValueError):
            return jsonify({"error": "方案数据损坏，无法读取"}), 422
        return jsonify({"plan": {
            "id": row["plan_id"], "name": row["plan_name"],
            "configuration": configuration, "result": result,
            "has_result": bool(result.get("ready")), "result_deferred": False,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }})

    def production_training_spec(payload: dict) -> dict:
        model = str(payload.get("model") or "kmeans")
        if model not in {"kmeans", "som", "hierarchical"}:
            if model == "transformer":
                raise ValueError("Transformer 为预留时序网络接口；达到至少50口长序列井后再启用，避免小样本过拟合。")
            raise ValueError("未知模型类型")
        try:
            cluster_count = max(2, min(12, int(payload.get("cluster_count") or 3)))
            max_iterations = max(5, min(500, int(payload.get("max_iterations") or 80)))
        except (TypeError, ValueError):
            raise ValueError("聚类数或迭代次数格式不正确")
        indicator_keys = payload.get("indicators") or []
        training_keys = payload.get("training_wells") or []
        evaluation_keys = payload.get("evaluation_wells") or []
        if not all(isinstance(value, list) for value in (indicator_keys, training_keys, evaluation_keys)):
            raise ValueError("指标与井清单格式不正确")
        return {
            "model": model, "cluster_count": cluster_count, "max_iterations": max_iterations,
            "indicator_keys": [str(value) for value in indicator_keys],
            "training_keys": [str(value) for value in training_keys],
            "evaluation_keys": [str(value) for value in evaluation_keys],
            "segmentation_mode": str(payload.get("segmentation_mode") or "split"),
            "model_parameters": payload.get("model_parameters") if isinstance(payload.get("model_parameters"), dict) else {},
        }

    def execute_production_training(
        payload: dict, database_path: str | Path | None = None,
        progress: Any = None,
    ) -> dict:
        spec = production_training_spec(payload)
        selected_database = Database(database_path) if database_path is not None else database
        if progress:
            progress(0.04, "读取生产特征缓存")
        with selected_database.connect() as conn:
            dataset = production_clustering_dataset(conn)
        if progress:
            cache_label = "复用工区特征缓存" if dataset.get("performance", {}).get("feature_cache") == "hit" else "完成生产特征缓存"
            progress(0.12, cache_label)
        mapped_progress = (lambda fraction, stage: progress(0.12 + fraction * 0.86, stage)) if progress else None
        result = clustering_tools.train_and_evaluate(
            dataset, spec["indicator_keys"], spec["training_keys"], spec["evaluation_keys"],
            spec["cluster_count"], spec["max_iterations"], spec["segmentation_mode"],
            spec["model"], spec["model_parameters"], mapped_progress,
        )
        if spec["model"] == "hierarchical" and result.get("ready"):
            # The current dependency-free engine uses the same standardized
            # centroid baseline.  Surface that fact instead of mislabelling it.
            result["requested_model"] = "层次聚类"
            result["model_note"] = "当前版本以可复现 K-Means 基线完成训练；层次聚类参数已保存为后续扩展接口。"
        result.setdefault("performance", {})["feature_cache"] = dataset.get("performance", {}).get("feature_cache", "unknown")
        return result

    @app.post("/api/production-clustering/train")
    def api_train_production_clustering():
        payload = request.get_json(silent=True) or {}
        try:
            result = execute_production_training(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.post("/api/production-clustering/training-jobs")
    def api_start_production_clustering_training_job():
        payload = request.get_json(silent=True) or {}
        try:
            production_training_spec(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        submitted_database = Path(database.path)
        job = training_manager.submit(
            lambda progress: execute_production_training(payload, submitted_database, progress),
        )
        return jsonify(job), 202

    @app.get("/api/production-clustering/training-jobs/<job_id>")
    def api_production_clustering_training_job(job_id: str):
        try:
            return jsonify(training_manager.status(job_id))
        except KeyError:
            return jsonify({"error": "训练任务不存在或软件已重新启动"}), 404

    @app.post("/api/production-clustering/save")
    def api_save_production_clustering():
        """Persist one auditable clustering scheme below the active .nvt workspace."""
        payload = request.get_json(silent=True) or {}
        result = payload.get("result")
        if not isinstance(result, dict) or not result.get("ready") or not isinstance(result.get("results"), list):
            return jsonify({"error": "请先完成模型训练，再保存训练信息"}), 400
        plan_name = str(payload.get("plan_name") or "产能分型方案 01").strip()[:120] or "产能分型方案"
        plan_id = str(payload.get("plan_id") or f"plan-{datetime.now().strftime('%Y%m%d%H%M%S')}").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", plan_id):
            return jsonify({"error": "方案标识格式不正确"}), 400
        with database.connect() as conn:
            duplicate = conn.execute(
                "SELECT plan_id FROM production_clustering_plans WHERE LOWER(plan_name)=LOWER(?) AND plan_id<>?",
                (plan_name, plan_id),
            ).fetchone()
        if duplicate:
            return jsonify({"error": f"方案名称“{plan_name}”已存在，请换一个名称"}), 409
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", plan_name).strip(". ") or "产能分型方案"
        root = Path(workspace_context["path"] or Path(database.path).parent)
        target = root / "生产聚类" / safe_name
        target.mkdir(parents=True, exist_ok=True)
        configuration = dict(payload.get("configuration")) if isinstance(payload.get("configuration"), dict) else {}
        configuration["plan_id"] = plan_id
        configuration["plan_name"] = plan_name
        (target / "训练配置.json").write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {key: result.get(key) for key in (
            "model", "model_type", "model_parameters", "selected_indicators", "training_count",
            "training_sample_count", "evaluation_count", "segmentation_mode", "cluster_count",
            "iterations", "inertia", "silhouette", "category_order", "notes",
        )}
        (target / "训练摘要.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "完整训练结果.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        indicator_keys = list(result.get("selected_indicators") or [])
        with (target / "井产能分类结果.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["井名", "范围", "产能类别", "相对级次", "开发层位", "X", "Y", *indicator_keys])
            for row in result["results"]:
                values = {metric.get("key"): metric.get("value") for metric in row.get("metrics") or []}
                writer.writerow([row.get("well_name"), row.get("scope"), row.get("category"), row.get("category_rank"), "/".join(row.get("formations") or []), row.get("x"), row.get("y"), *(values.get(key) for key in indicator_keys)])
        with (target / "类别指标统计.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["类别", "相对级次", "样本数", "指标", "单位", "最小值", "Q1", "中值", "Q3", "最大值"])
            for profile in result.get("cluster_profiles") or []:
                for metric in profile.get("metrics") or []:
                    writer.writerow([profile.get("category"), profile.get("rank"), profile.get("count"), metric.get("label"), metric.get("unit"), metric.get("min"), metric.get("q1"), metric.get("median"), metric.get("q3"), metric.get("max")])
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with database.connect() as conn:
            conn.execute(
                """INSERT INTO production_clustering_plans(plan_id,plan_name,configuration_json,result_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET
                   plan_name=excluded.plan_name,configuration_json=excluded.configuration_json,
                   result_json=excluded.result_json,updated_at=excluded.updated_at""",
                (plan_id, plan_name, json.dumps(configuration, ensure_ascii=False), json.dumps(result, ensure_ascii=False), timestamp, timestamp),
            )
            conn.execute(
                """INSERT INTO workspace_settings(setting_key,value_json,updated_at) VALUES('production_clustering_active_plan',?,?)
                   ON CONFLICT(setting_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (json.dumps(plan_id, ensure_ascii=False), timestamp),
            )
            conn.commit()
        files = [path.name for path in target.iterdir() if path.is_file()]
        return jsonify({
            "saved": True, "folder": str(target), "files": sorted(files),
            "plan": {"id": plan_id, "name": plan_name, "updated_at": timestamp},
        })

    @app.get("/api/production-clustering/well-groups")
    def api_list_production_clustering_well_groups():
        """List plan-independent well groups shared by production statistics and the 2D map."""
        with database.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM production_clustering_well_groups ORDER BY updated_at DESC,group_name"
            )]
        groups = []
        for row in rows:
            try:
                well_keys = json.loads(row["well_keys_json"] or "[]")
            except (TypeError, ValueError):
                well_keys = []
            groups.append({
                "id": row["group_id"], "name": row["group_name"],
                "well_keys": list(dict.fromkeys(str(value) for value in well_keys if value)),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            })
        return jsonify({"well_groups": groups})

    @app.post("/api/production-clustering/well-groups")
    def api_save_production_clustering_well_group():
        """Persist a plan-independent spatial well group in the active .nvt."""
        payload = request.get_json(silent=True) or {}
        group_id = str(payload.get("id") or f"group-{datetime.now().strftime('%Y%m%d%H%M%S%f')}").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", group_id):
            return jsonify({"error": "井组标识格式不正确"}), 400
        group_name = str(payload.get("name") or "").strip()[:120]
        if not group_name:
            return jsonify({"error": "请输入井组名称"}), 400
        raw_keys = payload.get("well_keys")
        if not isinstance(raw_keys, list):
            return jsonify({"error": "井组成员格式不正确"}), 400
        well_keys = list(dict.fromkeys(str(value).strip() for value in raw_keys if str(value).strip()))
        if not well_keys:
            return jsonify({"error": "井组至少需要一口井"}), 400
        if len(well_keys) > 50_000:
            return jsonify({"error": "单个井组成员数超过上限"}), 400
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with database.connect() as conn:
            duplicate = conn.execute(
                "SELECT group_id FROM production_clustering_well_groups WHERE LOWER(group_name)=LOWER(?) AND group_id<>?",
                (group_name, group_id),
            ).fetchone()
            if duplicate:
                return jsonify({"error": f"井组名称“{group_name}”已存在"}), 409
            conn.execute(
                """INSERT INTO production_clustering_well_groups(group_id,group_name,well_keys_json,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET
                   group_name=excluded.group_name,well_keys_json=excluded.well_keys_json,updated_at=excluded.updated_at""",
                (group_id, group_name, json.dumps(well_keys, ensure_ascii=False), timestamp, timestamp),
            )
            conn.commit()
        return jsonify({
            "saved": True,
            "group": {"id": group_id, "name": group_name, "well_keys": well_keys, "updated_at": timestamp},
        })

    @app.patch("/api/production-clustering/well-groups/<group_id>")
    def api_rename_production_clustering_well_group(group_id: str):
        """Rename one plan-independent spatial well group without changing members."""
        group_id = str(group_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", group_id):
            return jsonify({"error": "井组标识格式不正确"}), 400
        payload = request.get_json(silent=True) or {}
        group_name = str(payload.get("name") or "").strip()[:120]
        if not group_name:
            return jsonify({"error": "请输入井组名称"}), 400
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with database.connect() as conn:
            existing = conn.execute(
                "SELECT well_keys_json,created_at FROM production_clustering_well_groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            if not existing:
                return jsonify({"error": "井组不存在或已被删除"}), 404
            duplicate = conn.execute(
                "SELECT group_id FROM production_clustering_well_groups WHERE LOWER(group_name)=LOWER(?) AND group_id<>?",
                (group_name, group_id),
            ).fetchone()
            if duplicate:
                return jsonify({"error": f"井组名称“{group_name}”已存在"}), 409
            conn.execute(
                "UPDATE production_clustering_well_groups SET group_name=?,updated_at=? WHERE group_id=?",
                (group_name, timestamp, group_id),
            )
            conn.commit()
        try:
            well_keys = json.loads(existing["well_keys_json"] or "[]")
        except (TypeError, ValueError):
            well_keys = []
        return jsonify({
            "saved": True,
            "group": {"id": group_id, "name": group_name, "well_keys": well_keys, "updated_at": timestamp},
        })

    @app.post("/api/production-clustering/curves")
    def api_production_clustering_curves():
        """Return compact production curves for a selected well or result class."""
        payload = request.get_json(silent=True) or {}
        requested = payload.get("well_keys") or []
        if not isinstance(requested, list):
            return jsonify({"error": "井清单格式不正确"}), 400
        normalized = [normalize_well_name(str(value)) for value in requested]
        well_keys = list(dict.fromkeys(value for value in normalized if value))[:60]
        if not well_keys:
            return jsonify({"curves": [], "truncated": False})
        placeholders = ",".join("?" for _ in well_keys)
        with database.connect() as conn:
            mode, correction = production_source(conn)
            monthly = production_monthly_rows(conn, mode, correction, well_keys)
            events = [dict(row) for row in conn.execute(
                f"SELECT * FROM production_events WHERE well_key IN ({placeholders}) ORDER BY well_key,event_date,id",
                well_keys,
            )]
        monthly_by_key: dict[str, list[dict]] = defaultdict(list)
        event_by_key: dict[str, list[dict]] = defaultdict(list)
        for row in monthly:
            monthly_by_key[row["well_key"]].append(row)
        for row in events:
            event_by_key[row["well_key"]].append(row)
        curves = []
        for key in well_keys:
            series = production_tools.build_production_series(monthly_by_key.get(key, []))
            stride = max(1, math.ceil(len(series) / 80))
            sampled = series[::stride]
            if series and sampled[-1] is not series[-1]:
                sampled.append(series[-1])
            curves.append({
                "well_key": key,
                "well_name": next((row.get("well_name") for row in monthly_by_key.get(key, []) if row.get("well_name")), key),
                "record_count": len(series),
                "series": [{
                    "date": row.get("date"), "month_index": row.get("month_index"),
                    "oil_rate": row.get("oil_rate"), "liquid_rate": row.get("liquid_rate"),
                    "water_rate": row.get("water_rate"), "water_cut": row.get("water_cut"),
                } for row in sampled],
                "events": [{"date": row.get("event_date"), "type": row.get("event_type"), "status": row.get("status")} for row in event_by_key.get(key, [])],
            })
        return jsonify({"curves": curves, "truncated": len(normalized) > len(well_keys), "limit": 60})

    @app.post("/api/ofm/reanalyze")
    def api_ofm_reanalyze():
        payload = request.get_json(silent=True) or {}
        source_id = payload.get("source_id")
        try:
            source_id = int(source_id)
        except (TypeError, ValueError):
            return jsonify({"error": "请选择需要重新分析的 MDB 数据源"}), 400
        with database.connect() as conn:
            source = conn.execute(
                """SELECT os.file_path,s.batch,s.version FROM ofm_sources os
                   JOIN sources s ON s.id=os.source_id WHERE os.id=?""", (source_id,)
            ).fetchone()
        if not source:
            return jsonify({"error": "指定的 MDB 挂载记录不存在"}), 404
        path = Path(source["file_path"]).expanduser().resolve()
        if not path.is_file():
            return jsonify({"error": f"MDB 原文件当前不可访问：{path}"}), 400
        options = {
            "data_type": "production",
            "batch": source["batch"] or "OFM MDB 重分析",
            "version": source["version"] or "Access read-only",
            "crs": None,
        }
        process_directory = workspace_context["path"] / "process" if workspace_context["path"] else DATA_DIR / "process"
        return jsonify(job_manager.submit([path], database.path, options, process_directory)), 202

    @app.get("/api/ofm/table")
    def api_ofm_table():
        source_id = request.args.get("source_id", type=int)
        table_name = request.args.get("table", "").strip()
        if not source_id or not table_name:
            return jsonify({"error": "缺少 OFM 数据源或表名"}), 400
        with database.connect() as conn:
            row = conn.execute(
                """SELECT os.file_path FROM ofm_sources os
                   JOIN ofm_table_catalog tc ON tc.ofm_source_id=os.id
                   WHERE os.id=? AND tc.table_name=?""",
                (source_id, table_name),
            ).fetchone()
        if not row:
            return jsonify({"error": "OFM 表不存在或不属于该数据源"}), 404
        try:
            return jsonify(ofm_tools.read_table(
                row["file_path"], table_name,
                request.args.get("limit", default=200, type=int),
                request.args.get("offset", default=0, type=int),
            ))
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/ofm/table.csv")
    def api_ofm_table_csv():
        source_id = request.args.get("source_id", type=int)
        table_name = request.args.get("table", "").strip()
        with database.connect() as conn:
            row = conn.execute(
                """SELECT os.file_path FROM ofm_sources os
                   JOIN ofm_table_catalog tc ON tc.ofm_source_id=os.id
                   WHERE os.id=? AND tc.table_name=?""",
                (source_id, table_name),
            ).fetchone() if source_id and table_name else None
        if not row:
            return jsonify({"error": "OFM 表不存在或不属于该数据源"}), 404
        try:
            content = ofm_tools.table_csv_bytes(row["file_path"], table_name)
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return send_file(io.BytesIO(content), mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=f"{table_name}.csv")

    @app.get("/api/production/export/<kind>.csv")
    def api_production_export(kind: str):
        labels = {
            "coordinates": "井位坐标", "perforations": "射孔层段", "summary": "井基本动态汇总",
            "history": "各井生产历史", "injection": "各井注入历史", "events": "井状态与干预事件",
            "deviations": "OFM井轨迹", "markers": "OFM井上标志与分层", "keywords": "OFM关键字列表",
        }
        if kind not in labels:
            return jsonify({"error": "未知的生产动态导出类型"}), 404
        with database.connect() as conn:
            headers, rows = _production_export_sets(conn)[kind]
        return send_file(
            io.BytesIO(_csv_bytes(headers, rows)), mimetype="text/csv; charset=utf-8",
            as_attachment=True, download_name=f"{labels[kind]}.csv",
        )

    @app.get("/api/production/export/deviations-dev.zip")
    def api_production_export_deviations_dev():
        with database.connect() as conn:
            content, well_count = _ofm_deviation_dev_zip(conn)
        if not well_count:
            return jsonify({"error": "当前 MDB 没有同时具备井口 XY 与 MD/TVD/XDELT/YDELT 的轨迹记录"}), 404
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            io.BytesIO(content), mimetype="application/zip", as_attachment=True,
            download_name=f"OFM_Petrel标准_DEV_{well_count}井_{stamp}.zip",
        )

    @app.get("/api/production/export-package.zip")
    def api_production_export_package():
        content, _ = _build_production_export_package(database)
        return send_file(io.BytesIO(content), mimetype="application/zip", as_attachment=True, download_name="OFM生产动态完整拆分.zip")

    @app.post("/api/production/export-to-folder")
    def api_production_export_to_folder():
        payload = request.get_json(silent=True) or {}
        raw_folder = str(payload.get("folder") or "").strip()
        if not raw_folder:
            return jsonify({"error": "请先指定导出文件夹"}), 400
        folder = Path(raw_folder).expanduser().resolve()
        if not folder.is_dir():
            return jsonify({"error": f"导出文件夹不存在：{folder}"}), 400
        content, manifest_rows = _build_production_export_package(database)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = folder / f"OFM生产动态完整拆分_{stamp}.zip"
        counter = 2
        while target.exists():
            target = folder / f"OFM生产动态完整拆分_{stamp}_{counter}.zip"
            counter += 1
        target.write_bytes(content)
        return jsonify({
            "ok": True, "path": str(target), "bytes": len(content),
            "manifest_rows": manifest_rows,
        })

    @app.get("/api/model-inventory")
    def api_model_inventory():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        project_root = str(Path(snapshot["project"]["root"]).resolve())
        with database.connect() as conn:
            items = [dict(row) for row in conn.execute(
                """SELECT id,filename,relative_path,file_path,extension,source_folder,bytes,modified_at,representative
                   FROM project_catalog_items WHERE project_root=? ORDER BY relative_path""",
                (project_root,),
            )]
        result = model_tools.model_inventory(items)
        result["project_root"] = project_root
        result["catalog_files"] = len(items)
        return jsonify(result)

    @app.get("/api/global-filter")
    def api_global_filter():
        snapshot = snapshot_or_none()
        with database.connect() as conn:
            rules = filter_tools.load_filter(conn)
            if not snapshot:
                return jsonify({"filter": rules, "meta": {"active": False, "matched": 0, "total": 0}, "options": {"curves": [], "polygons": [], "horizons": []}})
            wells = project_wells(conn, snapshot)
            _, meta = filter_tools.evaluate_filter(conn, snapshot, wells, curve_profile(snapshot), rules)
            curves = [row["mnemonic"] for row in curve_tools.curve_statistics(curve_profile(snapshot)) if not curve_tools.is_time_depth_mnemonic(row["mnemonic"])]
            polygons = [{"id": row["id"], "name": row["name"]} for row in filter_tools.project_polygons(conn, snapshot)]
            root = str(Path(snapshot["project"]["root"]).resolve())
            horizons = filter_tools.horizon_options(snapshot)
            for horizon in horizons:
                horizon["ready"] = bool(conn.execute("SELECT 1 FROM horizon_well_hits WHERE project_root=? AND horizon_key=? LIMIT 1", (root, horizon["id"])).fetchone())
            return jsonify({"filter": rules, "meta": meta, "options": {"curves": curves, "polygons": polygons, "horizons": horizons}})

    @app.put("/api/global-filter")
    def api_save_global_filter():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        with database.connect() as conn:
            rules = filter_tools.save_filter(conn, payload)
            if not snapshot:
                return jsonify({"filter": rules, "meta": {"active": False, "matched": 0, "total": 0}})
            wells = project_wells(conn, snapshot)
            _, meta = filter_tools.evaluate_filter(conn, snapshot, wells, curve_profile(snapshot), rules)
            return jsonify({"filter": rules, "meta": meta})

    @app.delete("/api/global-filter")
    def api_clear_global_filter():
        with database.connect() as conn:
            rules = filter_tools.save_filter(conn, {"active": False, "conditions": []})
        return jsonify({"filter": rules, "ok": True})

    @app.get("/api/curve-filter")
    def api_curve_filter():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({
                "filter": distribution_tools.default_curve_filter(),
                "options": {"curves": [], "types": [], "wells": [], "horizons": []},
                "well_filter": {"active": False, "matched": 0, "total": 0},
            })
        with database.connect() as conn:
            wells = project_wells(conn, snapshot)
            filtered_wells, well_meta = project_wells_with_filter(conn, snapshot)
            allowed = {row["project_key"] for row in filtered_wells}
            workbench = curve_tools.curve_workbench(conn, snapshot, curve_profile(snapshot), allowed)
            horizons = filter_tools.horizon_options(snapshot)
            root = str(Path(snapshot["project"]["root"]).resolve())
            for horizon in horizons:
                horizon["ready"] = bool(conn.execute(
                    "SELECT 1 FROM horizon_well_hits WHERE project_root=? AND horizon_key=? LIMIT 1",
                    (root, horizon["id"]),
                ).fetchone())
            options = distribution_tools.curve_filter_options(workbench, filtered_wells, horizons)
            return jsonify({
                "filter": distribution_tools.load_curve_filter(conn),
                "options": options,
                "well_filter": well_meta,
                "all_wells": len(wells),
            })

    @app.put("/api/curve-filter")
    def api_save_curve_filter():
        payload = request.get_json(silent=True) or {}
        with database.connect() as conn:
            rules = distribution_tools.save_curve_filter(conn, payload)
        return jsonify({"filter": rules, "ok": True})

    @app.delete("/api/curve-filter")
    def api_clear_curve_filter():
        with database.connect() as conn:
            rules = distribution_tools.save_curve_filter(conn, {"active": False})
        return jsonify({"filter": rules, "ok": True})

    @app.post("/api/curve-distribution")
    def api_curve_distribution():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        try:
            payload = request.get_json(silent=True) or {}
            with database.connect() as conn:
                filtered_wells, _ = project_wells_with_filter(conn, snapshot)
                allowed = {row["project_key"] for row in filtered_wells}
                profile = curve_profile(snapshot)
                workbench = curve_tools.curve_workbench(conn, snapshot, profile, allowed)
                result = distribution_tools.curve_distribution(
                    conn, snapshot, profile, workbench, allowed, payload,
                )
            return jsonify(result)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/relationship-search")
    def api_relationship_search():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            with database.connect() as conn:
                # 关联检索用于回答“工区里是否存在”，因此不受当前井点筛选限制。
                wells = project_wells(conn, snapshot)
                result = relationship_tools.relationship_search(
                    conn, snapshot, wells, str(payload.get("query") or ""), str(payload.get("mode") or "single"),
                )
            return jsonify(result)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/well-reconstruction/export")
    def api_well_reconstruction_export():
        """Build a compact, auditable four-category handoff for one well.

        LAS is copied directly from the user's local source; Well Head, DEV
        and Well Top are transparent CSV reconstructions.  Nothing is written
        back to the original project directory.
        """
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or request.form.to_dict() or {}
        query = str(payload.get("well") or "").strip()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        if not query:
            return jsonify({"error": "请输入需要重构导出的井名"}), 400
        target = None
        try:
            with database.connect() as conn:
                wells = project_wells(conn, snapshot)
                relation = relationship_tools.relationship_search(conn, snapshot, wells, query, "single")
                selected = (relation.get("well") or {}).get("selected")
                if not selected:
                    raise ValueError("未能唯一匹配该井；请使用完整井名或先在检索窗确认候选")
                raw_well = next((row for row in wells if row["project_key"] == selected["project_key"]), None)
                if not raw_well:
                    raise ValueError("没有找到该井的原始资料索引")
                evidence = selected.get("evidence_files") or []
                tops = (relation.get("layer") or {}).get("available_tops") or []
                target = archive_target("well_reconstruction_")
                export_name = reconstruction_export_name(selected["name"])
                manifest = {
                    "well_name": selected["name"], "well_key": selected["project_key"],
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "principle": "LAS 直接复制原始文件；Well Head、DEV、Well Top 由本地索引/原始文件只读重构。",
                    "included": [], "missing": [],
                }
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    head_sources = [row for row in evidence if row.get("data_type") == "well_head"]
                    head_rows = [{
                        "well_name": selected["name"], "well_key": selected["project_key"], "uwi": raw_well.get("uwi"),
                        "x": raw_well.get("x"), "y": raw_well.get("y"), "crs": raw_well.get("crs"),
                        "kb_elevation": raw_well.get("kb_elevation"), "total_depth_md": raw_well.get("total_depth"),
                        "source_file": row.get("filename"), "source_path": row.get("file_path"),
                    } for row in head_sources] or [{
                        "well_name": selected["name"], "well_key": selected["project_key"], "uwi": raw_well.get("uwi"),
                        "x": raw_well.get("x"), "y": raw_well.get("y"), "crs": raw_well.get("crs"),
                        "kb_elevation": raw_well.get("kb_elevation"), "total_depth_md": raw_well.get("total_depth"),
                        "source_file": None, "source_path": None,
                    }]
                    archive.writestr("WellHead.csv", _csv_bytes(list(head_rows[0]), head_rows))
                    manifest["included"].append("WellHead.csv")

                    dev_path = next((Path(path) for path in raw_well.get("_dev_paths", []) if Path(path).is_file()), None)
                    if dev_path:
                        stations = parse_dev_stations(dev_path)
                        if stations:
                            dev_rows = [{"well_name": selected["name"], "md": row.get("md"), "x": row.get("x"), "y": row.get("y"), "z": row.get("z"), "tvd": row.get("tvd"), "source_file": dev_path.name, "source_path": str(dev_path)} for row in stations]
                            archive.writestr("WellDEV.csv", _csv_bytes(list(dev_rows[0]), dev_rows))
                            manifest["included"].append("WellDEV.csv")
                        else:
                            manifest["missing"].append("WellDEV：文件存在但没有可解析的有效测点")
                    else:
                        manifest["missing"].append("WellDEV：未关联在线 DEV 文件")

                    las_path = next((Path(path) for path in raw_well.get("_las_paths", []) if Path(path).is_file()), None)
                    if las_path:
                        archive.write(las_path, f"WellLAS/{las_path.name}")
                        manifest["included"].append(f"WellLAS/{las_path.name}")
                    else:
                        manifest["missing"].append("WellLAS：未关联在线 LAS 文件")

                    top_rows = [{"well_name": selected["name"], "layer_name": row.get("surface"), "depth_md": row.get("md"), "depth_tvd": row.get("tvd"), "source_file": row.get("source_file"), "relative_path": row.get("relative_path"), "version": row.get("version"), "year": row.get("year")} for row in tops]
                    if top_rows:
                        archive.writestr("WellTop.csv", _csv_bytes(list(top_rows[0]), top_rows))
                        manifest["included"].append("WellTop.csv")
                    else:
                        manifest["missing"].append("WellTop：没有解析到该井的分层记录")
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            return archive_response(target, f"{export_name}_井资料重构.zip")
        except (ValueError, OSError, sqlite3.Error) as exc:
            if target:
                target.unlink(missing_ok=True)
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/horizon-coverage")
    def api_horizon_coverage():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            with database.connect() as conn:
                wells = project_wells(conn, snapshot)
                result = filter_tools.compute_surface_horizon_hits(conn, snapshot, wells, str(payload["horizon_key"]))
            return jsonify(result)
        except (KeyError, ValueError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/horizon-workbench")
    def api_horizon_workbench():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        try:
            with database.connect() as conn:
                wells = project_wells(conn, snapshot)
                return jsonify(horizon_tools.horizon_workbench(conn, snapshot, wells))
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/horizon-name-groups")
    def api_create_horizon_name_group():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            with database.connect() as conn:
                result = horizon_tools.create_horizon_name_group(
                    conn, str(Path(snapshot["project"]["root"]).resolve()), str(payload.get("name") or "")
                )
            return jsonify(result), 201
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/horizon-name-groups/<canonical_key>/aliases")
    def api_assign_horizon_name_aliases(canonical_key: str):
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        payload = request.get_json(silent=True) or {}
        aliases = payload.get("aliases") or []
        if not isinstance(aliases, list) or not aliases:
            return jsonify({"error": "请拖入至少一个已识别层位名称"}), 400
        try:
            with database.connect() as conn:
                result = horizon_tools.assign_horizon_name_aliases(
                    conn, str(Path(snapshot["project"]["root"]).resolve()), canonical_key, aliases
                )
            return jsonify(result)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/horizon-name-groups/aliases/<alias_key>")
    def api_remove_horizon_name_alias(alias_key: str):
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        try:
            with database.connect() as conn:
                horizon_tools.remove_horizon_name_alias(
                    conn, str(Path(snapshot["project"]["root"]).resolve()), alias_key
                )
            return jsonify({"ok": True})
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/horizon-name-groups/<canonical_key>")
    def api_remove_horizon_name_group(canonical_key: str):
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        try:
            with database.connect() as conn:
                horizon_tools.remove_horizon_name_group(
                    conn, str(Path(snapshot["project"]["root"]).resolve()), canonical_key
                )
            return jsonify({"ok": True})
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/horizon-workbench/detail")
    def api_horizon_workbench_detail():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        kind, key = request.args.get("kind", ""), request.args.get("key", "")
        if not kind or not key:
            return jsonify({"error": "请选择 Well Top 层位或构造面版本"}), 400
        try:
            with database.connect() as conn:
                wells = project_wells(conn, snapshot)
                return jsonify(horizon_tools.horizon_detail(conn, snapshot, wells, kind, key))
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/horizon-workbench/trajectories")
    def api_horizon_workbench_trajectories():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        payload = request.get_json(silent=True) or {}
        selections = payload.get("selections") or []
        if not isinstance(selections, list):
            return jsonify({"error": "轨迹选择格式不正确"}), 400
        try:
            with database.connect() as conn:
                wells = project_wells(conn, snapshot)
            return jsonify(horizon_tools.selected_trajectories(wells, selections))
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/horizon-workbench/compute")
    def api_horizon_workbench_compute():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            item_id = int(payload["item_id"])
            with database.connect() as conn:
                wells = project_wells(conn, snapshot)
                return jsonify(horizon_tools.compute_catalog_surface_hits(conn, snapshot, wells, item_id))
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/reserves")
    def api_reserves():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        try:
            with database.connect() as conn:
                return jsonify({
                    "surfaces": reserve_tools.surface_inventory(conn, snapshot),
                    "groups": reserve_tools.list_groups(conn),
                    "parameters": reserve_tools.PARAMETERS,
                    "method": "构造面确定参考网格；属性面最近节点重采样；常数只在有效网格内参与计算。",
                })
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/reserves/surface-quality")
    def api_reserve_surface_quality():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            with database.connect() as conn:
                grid = reserve_tools.resolve_surface(conn, snapshot, payload.get("surface_id"))
                wells, filter_meta = project_wells_with_filter(conn, snapshot)
            return jsonify({
                "quality": surface_qc_tools.surface_quality(grid),
                "wells": surface_qc_tools.well_surface_samples(grid, wells),
                "well_filter": filter_meta,
            })
        except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/reserves/surface-compare")
    def api_reserve_surface_compare():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            with database.connect() as conn:
                result = surface_qc_tools.compare_surfaces(conn, snapshot, payload.get("surface_ids"))
            return jsonify(result)
        except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/reserves/surfaces/import")
    def api_import_reserve_surfaces():
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "请选择至少一个 ZMAP 属性面"}), 400
        target_dir = process_directory() / "reserve_surfaces"
        target_dir.mkdir(parents=True, exist_ok=True)
        imported, errors = [], []
        for index, uploaded in enumerate(files):
            original = Path(uploaded.filename or f"surface_{index + 1}.zmap").name
            suffix = Path(original).suffix.lower()
            if suffix not in reserve_tools.SURFACE_SUFFIXES:
                errors.append({"filename": original, "error": "仅支持 ZMAP、FSASCI、规则 XYZ 等文本平面"})
                continue
            safe = secure_filename(original) or f"surface{suffix}"
            destination = target_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{index}_{safe}"
            uploaded.save(destination)
            try:
                with database.connect() as conn:
                    imported.append(reserve_tools.import_surface(conn, destination, original))
            except (ValueError, OSError, sqlite3.Error) as exc:
                destination.unlink(missing_ok=True)
                errors.append({"filename": original, "error": str(exc)})
        status = 201 if imported else 400
        return jsonify({"surfaces": imported, "errors": errors}), status

    @app.post("/api/reserves/groups")
    def api_create_reserve_group():
        try:
            with database.connect() as conn:
                return jsonify(reserve_tools.save_group(conn, request.get_json(silent=True) or {})), 201
        except (ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.put("/api/reserves/groups/<group_id>")
    def api_update_reserve_group(group_id: str):
        try:
            with database.connect() as conn:
                return jsonify(reserve_tools.save_group(conn, request.get_json(silent=True) or {}, group_id))
        except (ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/reserves/groups/<group_id>")
    def api_delete_reserve_group(group_id: str):
        try:
            with database.connect() as conn:
                reserve_tools.delete_group(conn, group_id)
            return jsonify({"ok": True})
        except sqlite3.Error as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/reserves/calculate")
    def api_calculate_reserves():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        try:
            with database.connect() as conn:
                return jsonify(reserve_tools.calculate(conn, snapshot, request.get_json(silent=True) or {}))
        except (ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/sources")
    def api_sources():
        with database.connect() as conn:
            rows = analytics.sources(conn)
            snapshot = snapshot_or_none()
            if not rows and snapshot:
                scanned_at = snapshot["project"].get("scanned_at")
                rows = [{"id": f"category:{row['key']}", "filename": row["label"], "file_path": snapshot["project"]["root"], "data_type": row["key"], "batch": "完整目录索引", "version": "代表性解析", "imported_at": scanned_at, "record_count": row["files"], "status": "ready", "warning": None} for row in snapshot.get("categories", []) if row["files"]]
            return jsonify(rows)

    @app.get("/api/identity-suggestions")
    def api_identity_suggestions():
        threshold = request.args.get("threshold", default=0.72, type=float)
        with database.connect() as conn:
            imported = analytics.summary(conn)["wells"]
            snapshot = snapshot_or_none()
            if imported or not snapshot:
                return jsonify(analytics.identity_suggestions(conn, threshold))
            return jsonify(project_identity_suggestions(conn, snapshot))

    @app.post("/api/wells/merge")
    def api_merge_wells():
        payload = request.get_json(silent=True) or {}
        try:
            with database.connect() as conn:
                keep_ref, merge_ref = str(payload["keep_id"]), str(payload["merge_id"])
                if keep_ref.startswith("project:") and merge_ref.startswith("project:"):
                    snapshot = snapshot_or_none()
                    keep_key, merge_key = keep_ref.split(":", 1)[1], merge_ref.split(":", 1)[1]
                    save_well_alias(conn, snapshot, merge_key, keep_key, payload.get("canonical_name") or keep_key)
                    return jsonify({"ok": True, "keep_id": keep_ref, "mode": "alias"})
                keep_id, merge_id = int(keep_ref), int(merge_ref)
                importer.merge_wells(conn, keep_id, merge_id, payload.get("canonical_name"))
                conn.commit()
            return jsonify({"ok": True, "keep_id": keep_id, "mode": "merge"})
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/project-wells/compare-trajectories")
    def api_compare_project_trajectories():
        payload = request.get_json(silent=True) or {}
        try:
            snapshot = snapshot_or_none()
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            left = str(payload["left_id"]).split(":", 1)[-1]
            right = str(payload["right_id"]).split(":", 1)[-1]
            with database.connect() as conn:
                return jsonify(compare_project_trajectories(conn, snapshot, left, right))
        except (KeyError, ValueError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/identity-suggestions/strong-trajectories")
    def api_strong_trajectory_suggestions():
        """Add an explicit DEV-equality check to the current suggestion list."""
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            # The identity suggestion API itself caps the visible candidate
            # list.  Keep this ceiling above that list so the strong check
            # covers every candidate the operator can see, rather than a
            # silent first-page subset.
            limit = min(400, max(1, int(payload.get("limit") or 150)))
            with database.connect() as conn:
                candidates = project_identity_suggestions(conn, snapshot, limit=limit)
                results = []
                for candidate in candidates:
                    if not candidate.get("can_compare_trajectory"):
                        continue
                    try:
                        result = compare_project_trajectories(
                            conn, snapshot,
                            str(candidate["left"]["id"]).split(":", 1)[-1],
                            str(candidate["right"]["id"]).split(":", 1)[-1],
                        )
                    except (ValueError, OSError):
                        continue
                    results.append({
                        "left_id": candidate["left"]["id"], "right_id": candidate["right"]["id"],
                        "left_name": candidate["left"]["canonical_name"], "right_name": candidate["right"]["canonical_name"],
                        "score": candidate["score"], "reasons": candidate["reasons"], **result,
                        "strong_match": result["conclusion"] == "轨迹完全一致",
                    })
            return jsonify({"rows": results, "checked": len(results), "limit": limit,
                            "method": "先逐站比较 MD/X/Y/Z/TVD（数值容差 1e-6）；仅全部测点一致才标记为强轨迹匹配，并同时给出共同 MD 段 101 点距离统计。"})
        except (TypeError, ValueError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/catalog")
    def api_catalog():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        root = str(Path(snapshot["project"]["root"]).resolve())
        with database.connect() as conn:
            return jsonify(catalog_payload(
                conn, root, request.args.get("category"), request.args.get("q", ""),
                request.args.get("group_id", type=int), request.args.get("page", 1, type=int),
                request.args.get("per_page", 100, type=int),
            ))

    @app.post("/api/catalog/items/<int:item_id>/reveal")
    def api_reveal_catalog_item(item_id: int):
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        project_root = Path(snapshot["project"]["root"]).resolve()
        with database.connect() as conn:
            row = conn.execute(
                """SELECT i.file_path,
                          EXISTS(SELECT 1 FROM sources s
                                 WHERE lower(s.file_path)=lower(i.file_path) AND s.status='ready') imported
                     FROM project_catalog_items i WHERE i.id=? AND i.project_root=?""",
                (item_id, str(project_root)),
            ).fetchone()
        if not row:
            return jsonify({"error": "数据对象不存在"}), 404
        target = Path(row["file_path"]).resolve()
        try:
            target.relative_to(project_root)
        except ValueError:
            if not row["imported"]:
                return jsonify({"error": "拒绝访问未登记的工区外部路径"}), 403
        if not target.exists():
            return jsonify({"error": "原始文件当前不可访问，可能是磁盘未连接或文件已移动"}), 404
        if os.name != "nt":
            return jsonify({"error": "当前系统不支持资源管理器定位"}), 501
        payload = request.get_json(silent=True) or {}
        mode = "folder" if payload.get("mode") == "folder" else "select"
        try:
            launch = reveal_in_windows_explorer(target, mode)
        except OSError as exc:
            return jsonify({"error": f"无法打开 Windows 资源管理器：{exc}"}), 500
        return jsonify({
            "ok": True,
            "action": mode,
            "folder": str(target.parent),
            "filename": target.name,
            **launch,
        })

    @app.post("/api/catalog/groups")
    def api_catalog_groups():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                return jsonify(create_group(conn, root, str(payload["category"]), str(payload["name"]), payload.get("parent_id")))
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify({"error": "同级文件夹已存在" if isinstance(exc, sqlite3.IntegrityError) else str(exc)}), 400

    @app.patch("/api/catalog/groups/<int:group_id>")
    def api_update_catalog_group(group_id: int):
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                return jsonify(update_catalog_group(conn, root, group_id, str(payload.get("name") or "")))
        except (ValueError, sqlite3.IntegrityError) as exc:
            return jsonify({"error": "同级文件夹已存在" if isinstance(exc, sqlite3.IntegrityError) else str(exc)}), 400

    @app.delete("/api/catalog/groups/<int:group_id>")
    def api_delete_catalog_group(group_id: int):
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                delete_catalog_group(conn, root, group_id)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/catalog/items/group")
    def api_catalog_assign_group():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                count = assign_items_to_group(conn, root, int(payload["group_id"]), [int(value) for value in payload.get("item_ids", [])])
            return jsonify({"ok": True, "assigned": count})
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/catalog/items/group")
    def api_catalog_unassign_group():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                count = unassign_items_from_group(conn, root, [int(value) for value in payload.get("item_ids", [])])
            return jsonify({"ok": True, "unassigned": count})
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/well-groups")
    def api_well_groups():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        root = str(Path(snapshot["project"]["root"]).resolve())
        with database.connect() as conn:
            groups = list_well_groups(conn, root)
            wells = project_wells(conn, snapshot)
        return jsonify({
            "groups": groups,
            "wells": [{"well_key": row["project_key"], "well_name": row["canonical_name"], "source_types": row["source_types"], "x": row.get("x"), "y": row.get("y")} for row in wells],
            "method": "井组只保存统一井键的成员关系；不会自动合并井名、改写原始文件或移动目录。",
        })

    @app.post("/api/well-groups")
    def api_create_well_group():
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                group = create_well_group(conn, root, str(payload.get("name") or ""), payload.get("description"))
            return jsonify(group), 201
        except (ValueError, sqlite3.IntegrityError) as exc:
            return jsonify({"error": "井组名称已存在" if isinstance(exc, sqlite3.IntegrityError) else str(exc)}), 400

    @app.patch("/api/well-groups/<int:group_id>")
    def api_update_well_group(group_id: int):
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                return jsonify(update_well_group(conn, root, group_id, str(payload.get("name") or ""), payload.get("description")))
        except (ValueError, sqlite3.IntegrityError) as exc:
            return jsonify({"error": "井组名称已存在" if isinstance(exc, sqlite3.IntegrityError) else str(exc)}), 400

    @app.delete("/api/well-groups/<int:group_id>")
    def api_delete_well_group(group_id: int):
        snapshot = snapshot_or_none()
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                delete_well_group(conn, root, group_id)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.put("/api/well-groups/<int:group_id>/members")
    def api_replace_well_group_members(group_id: int):
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                keys = {row["project_key"] for row in project_wells(conn, snapshot)}
                count = replace_well_group_members(conn, root, group_id, payload.get("well_keys") or [], keys)
            return jsonify({"ok": True, "members": count})
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/well-groups/condition-preview")
    def api_well_group_condition_preview():
        """Preview a delivery well group without persisting a hidden rule.

        Rules deliberately resolve to an explicit list of unified well keys.
        That makes later directory delivery reproducible even if an original
        export folder changes after the group has been approved.
        """
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            root = str(Path(snapshot["project"]["root"]).resolve())
            prefixes = [normalize_well_name(item) for item in re.split(r"[,，;；\n]+", str(payload.get("prefixes") or "")) if normalize_well_name(item)]
            curve_names = {str(item).strip().upper() for item in re.split(r"[,，;；\n]+", str(payload.get("curves") or "")) if str(item).strip()}
            horizon_names = {str(item).strip().upper() for item in re.split(r"[,，;；\n]+", str(payload.get("horizons") or "")) if str(item).strip()}
            coordinate_mode = str(payload.get("coordinate") or "")
            evidence_type = str(payload.get("evidence") or "")
            production_mode = str(payload.get("production") or "")
            production_from = str(payload.get("production_from") or "").strip()
            production_to = str(payload.get("production_to") or "").strip()
            minimum_months = max(0, int(payload.get("production_months") or 0))
            logic = "or" if str(payload.get("logic") or "").lower() == "or" else "and"
            curve_mode = "all" if str(payload.get("curve_mode") or "").lower() == "all" else "any"
            horizon_mode = "all" if str(payload.get("horizon_mode") or "").lower() == "all" else "any"
            with database.connect() as conn:
                rows, filter_meta = project_wells_with_filter(conn, snapshot)
                aliases = {row["alias_key"]: row["canonical_key"] for row in conn.execute(
                    "SELECT alias_key,canonical_key FROM horizon_name_aliases WHERE project_root=?", (root,)
                )}
                canonical_names = {row["canonical_key"]: row["canonical_name"] for row in conn.execute(
                    "SELECT canonical_key,canonical_name FROM horizon_name_groups WHERE project_root=?", (root,)
                )}
                def layer_key(value: str) -> str:
                    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", Path(str(value or "")).stem.upper())
                requested_horizons = {aliases.get(layer_key(name), layer_key(name)) for name in horizon_names}
                top_by_well: defaultdict[str, set[str]] = defaultdict(set)
                if requested_horizons:
                    # This is intentionally delayed until a formation condition
                    # is used; normal opening of the wells view does not parse
                    # Well Top files.
                    for top in horizon_tools._top_rows(conn, root)[1]:
                        raw = layer_key(top.get("surface") or top.get("layer") or top.get("name") or "")
                        top_by_well[top["well_key"]].add(aliases.get(raw, raw))
                production_by_name: dict[str, dict] = {}
                if production_mode or production_from or production_to or minimum_months:
                    production_rows = conn.execute(
                        """SELECT well_name,COUNT(*) records,MIN(production_month) first_month,
                                  MAX(production_month) last_month
                           FROM production_monthly GROUP BY well_name"""
                    )
                    production_by_name = {normalize_well_name(row["well_name"]): dict(row) for row in production_rows}
            matched = []
            active_rules = []
            if prefixes: active_rules.append("井名前缀")
            if curve_names: active_rules.append("测井曲线")
            if requested_horizons: active_rules.append("钻遇层位")
            if evidence_type: active_rules.append("资料硬证据")
            if coordinate_mode: active_rules.append("坐标状态")
            if production_mode or production_from or production_to or minimum_months: active_rules.append("生产历史")
            for row in rows:
                checks: list[bool] = []
                if prefixes:
                    checks.append(any(row["project_key"].startswith(prefix) for prefix in prefixes))
                if curve_names:
                    have = {str(name).upper() for name in row.get("curve_names") or []}
                    checks.append(curve_names.issubset(have) if curve_mode == "all" else bool(curve_names & have))
                if requested_horizons:
                    have = top_by_well.get(row["project_key"], set())
                    checks.append(requested_horizons.issubset(have) if horizon_mode == "all" else bool(requested_horizons & have))
                if evidence_type:
                    checks.append(evidence_type in set(row.get("source_types") or []))
                if coordinate_mode == "has": checks.append(row.get("x") is not None and row.get("y") is not None)
                if coordinate_mode == "missing": checks.append(row.get("x") is None or row.get("y") is None)
                if production_mode or production_from or production_to or minimum_months:
                    records = []
                    for source in row.get("_sources", []):
                        if source.get("data_type") == "production":
                            record = production_by_name.get(normalize_well_name(source.get("raw_name") or ""))
                            if record: records.append(record)
                    # Source aliases can be absent in a monthly table.  The
                    # resolved well key is therefore also tried as a fallback.
                    if not records and row.get("project_key") in production_by_name:
                        records.append(production_by_name[row["project_key"]])
                    exists = bool(records)
                    first = min((item.get("first_month") or "") for item in records) if records else ""
                    last = max((item.get("last_month") or "") for item in records) if records else ""
                    months = sum(int(item.get("records") or 0) for item in records)
                    valid_range = (not production_from or last >= production_from) and (not production_to or first <= production_to) and months >= minimum_months
                    checks.append((exists and valid_range) if production_mode != "missing" else not exists)
                keep = (any(checks) if logic == "or" else all(checks)) if checks else True
                if keep:
                    matched.append({"well_key": row["project_key"], "well_name": row["canonical_name"],
                                    "source_types": row.get("source_types") or [], "x": row.get("x"), "y": row.get("y")})
            return jsonify({
                "wells": matched, "count": len(matched), "total": len(rows), "logic": logic,
                "active_rules": active_rules,
                "method": "筛选依据为已索引井名、曲线、Well Top 与 OFM 月度记录；预览结果可再人工增删后保存为井组。",
                "filter": filter_meta,
                "horizon_labels": [canonical_names.get(key, key) for key in requested_horizons],
            })
        except (TypeError, ValueError, OSError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/well-groups/merge-preview")
    def api_well_group_merge_preview():
        """Reserve a deliberate group-combination workflow without merging wells."""
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        try:
            if not snapshot:
                raise ValueError("尚未载入项目快照")
            requested = {int(value) for value in payload.get("group_ids") or []}
            root = str(Path(snapshot["project"]["root"]).resolve())
            with database.connect() as conn:
                groups = [row for row in list_well_groups(conn, root) if row["id"] in requested]
            union = sorted({key for group in groups for key in group.get("well_keys", [])})
            return jsonify({
                "groups": groups, "union_well_keys": union, "union_count": len(union),
                "note": "这是合并预览，不会修改井身份或分组。确认后的“新建联合井组”将在后续版本由人工命名并显式保存。",
            })
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/workspace/directory-export")
    def api_workspace_directory_export():
        """Copy a user-selected delivery scope into an explicit, new directory."""
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        folder_text = str(payload.get("folder") or "").strip()
        name_text = str(payload.get("name") or "标准工区交付").strip()
        if not folder_text:
            return jsonify({"error": "请先指定交付目录的上级文件夹"}), 400
        parent = Path(folder_text).expanduser().resolve()
        if not parent.is_dir():
            return jsonify({"error": f"交付目录不存在：{parent}"}), 400
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", name_text).strip(" ._")
        if not safe_name:
            return jsonify({"error": "目标文件夹名称无效"}), 400
        target = (parent / safe_name).resolve()
        if target.exists():
            return jsonify({"error": f"目标文件夹已存在，为避免覆盖请更换名称：{target}"}), 400
        requested_categories = {str(value) for value in payload.get("categories") or []}
        default_categories = {"well_heads", "well_logs", "well_paths", "well_tops", "production"}
        categories = requested_categories or default_categories
        project_root = Path(snapshot["project"]["root"]).resolve()
        report_rows: list[dict] = []
        copied: list[dict] = []
        skipped: list[dict] = []
        try:
            with database.connect() as conn:
                all_wells = project_wells(conn, snapshot)
                group_id = payload.get("well_group_id")
                group = None
                if group_id not in (None, "", 0):
                    group = next((row for row in list_well_groups(conn, str(project_root)) if row["id"] == int(group_id)), None)
                    if not group:
                        raise ValueError("所选定制井组不存在")
                    selected_keys = set(group.get("well_keys") or [])
                    selected_wells = [row for row in all_wells if row["project_key"] in selected_keys]
                    if not selected_wells:
                        raise ValueError("所选井组当前没有可导出的统一井")
                else:
                    selected_wells = all_wells
                selected_keys = {row["project_key"] for row in selected_wells}
                target.mkdir(parents=True)

                def copy_one(source: str | Path, folder_name: str, prefix: str = "") -> None:
                    source_path = Path(source)
                    if not source_path.is_file():
                        skipped.append({"类别": folder_name, "文件": str(source_path), "原因": "原文件离线或不存在"})
                        return
                    destination_folder = target / folder_name
                    destination_folder.mkdir(parents=True, exist_ok=True)
                    base = secure_filename(prefix + source_path.name) or source_path.name
                    destination = destination_folder / base
                    suffix = 2
                    while destination.exists():
                        destination = destination_folder / f"{Path(base).stem}_{suffix}{Path(base).suffix}"
                        suffix += 1
                    shutil.copy2(source_path, destination)
                    copied.append({"类别": folder_name, "源文件": str(source_path), "导出文件": str(destination.relative_to(target)), "字节": source_path.stat().st_size})

                if "well_heads" in categories:
                    rows = [{"well_name": row["canonical_name"], "well_key": row["project_key"], "uwi": row.get("uwi"), "x": row.get("x"), "y": row.get("y"), "crs": row.get("crs"), "kb_elevation": row.get("kb_elevation"), "total_depth_md": row.get("total_depth"), "source_types": "/".join(row["source_types"])} for row in selected_wells]
                    output = target / "Well head"
                    output.mkdir(exist_ok=True)
                    (output / "WellHead_SelectedWells.csv").write_bytes(_csv_bytes(list(rows[0]) if rows else ["well_name", "well_key"], rows))
                    copied.append({"类别": "Well head", "源文件": "统一井索引", "导出文件": "Well head/WellHead_SelectedWells.csv", "字节": (output / "WellHead_SelectedWells.csv").stat().st_size})
                if "well_logs" in categories:
                    seen: set[str] = set()
                    for well in selected_wells:
                        for path in well.get("_las_paths", []):
                            if path not in seen:
                                seen.add(path); copy_one(path, "Well LAS", f"{well['project_key']}_")
                if "well_paths" in categories:
                    seen = set()
                    for well in selected_wells:
                        for path in well.get("_dev_paths", []):
                            if path not in seen:
                                seen.add(path); copy_one(path, "Well DEV", f"{well['project_key']}_")
                if "well_tops" in categories:
                    top_rows: list[dict] = []
                    top_items = [dict(row) for row in conn.execute("SELECT filename,file_path,relative_path FROM project_catalog_items WHERE project_root=? AND category_key='well_tops'", (str(project_root),))]
                    for item in top_items:
                        try:
                            parsed = relationship_tools.parse_well_tops(item["file_path"])
                        except OSError:
                            skipped.append({"类别": "Well top", "文件": item["file_path"], "原因": "无法读取 Well Top 文件"})
                            continue
                        top_rows.extend({"well_name": row.get("well"), "well_key": row.get("well_key"), "layer_name": row.get("surface"), "depth_md": row.get("md"), "depth_tvd": row.get("tvd"), "source_file": item["filename"], "relative_path": item["relative_path"]} for row in parsed if row.get("well_key") in selected_keys)
                    output = target / "Well top"; output.mkdir(exist_ok=True)
                    (output / "WellTop_SelectedWells.csv").write_bytes(_csv_bytes(list(top_rows[0]) if top_rows else ["well_name", "well_key", "layer_name", "depth_md", "depth_tvd", "source_file"], top_rows))
                    copied.append({"类别": "Well top", "源文件": "按井筛选 Well Top", "导出文件": "Well top/WellTop_SelectedWells.csv", "字节": (output / "WellTop_SelectedWells.csv").stat().st_size})
                if "production" in categories:
                    output = target / "Well production"; output.mkdir(exist_ok=True)
                    events = [dict(row) for row in conn.execute("SELECT * FROM production_events")]
                    summaries = [row for row in _production_dashboard_summaries(conn, events) if row["well_key"] in selected_keys]
                    (output / "WellProduction_SelectedWells.csv").write_bytes(_csv_bytes(list(summaries[0]) if summaries else ["well_key", "well_name", "production_months"], summaries))
                    copied.append({"类别": "Well production", "源文件": "OFM 映射汇总", "导出文件": "Well production/WellProduction_SelectedWells.csv", "字节": (output / "WellProduction_SelectedWells.csv").stat().st_size})
                    mdb_sources = [dict(row) for row in conn.execute("SELECT filename,file_path FROM sources WHERE data_type='production' AND lower(file_path) LIKE '%.mdb'")]
                    for source in mdb_sources:
                        copy_one(source["file_path"], "Well production", "OFM_source_")

                extra_folders = {"seismic_3d": "Seismic 3D", "seismic_2d": "Seismic 2D", "polygons": "Polygon", "horizons": "Horizons", "faults": "Faults", "core": "Core", "interpretations": "Interpretations", "checkshots": "Checkshot", "other": "Other"}
                extra_keys = set(extra_folders) & categories
                if extra_keys:
                    placeholders = ",".join("?" for _ in extra_keys)
                    items = [dict(row) for row in conn.execute(f"SELECT category_key,file_path FROM project_catalog_items WHERE project_root=? AND category_key IN ({placeholders})", (str(project_root), *sorted(extra_keys)))]
                    for item in items:
                        copy_one(item["file_path"], extra_folders[item["category_key"]])

                production_keys = {row["well_key"] for row in summaries} if "production" in categories else set()
                for well in selected_wells:
                    types = set(well["source_types"])
                    report_rows.append({"well_name": well["canonical_name"], "well_key": well["project_key"], "Well head": "有" if "well_head" in types else "缺失", "Well LAS": "有" if "las" in types else "缺失", "Well DEV": "有" if "deviation" in types else "缺失", "Well Top": "有" if "well_top" in types else "缺失", "Well production": "有" if well["project_key"] in production_keys or "production" in types else "缺失", "source_types": "/".join(well["source_types"])})
                (target / "导出井清单与缺失项.csv").write_bytes(_csv_bytes(list(report_rows[0]) if report_rows else ["well_name", "well_key"], report_rows))
                (target / "导出文件清单.csv").write_bytes(_csv_bytes(["类别", "源文件", "导出文件", "字节"], copied))
                (target / "未复制文件清单.csv").write_bytes(_csv_bytes(["类别", "文件", "原因"], skipped))
                summary = {"目标目录": str(target), "井组": group["name"] if group else "全部统一井", "统一井数": len(selected_wells), "勾选类别": sorted(categories), "导出文件数": len(copied), "未复制文件数": len(skipped), "说明": "Well production 中的 MDB 是原 OFM 全工区只读副本；同目录的 WellProduction_SelectedWells.csv 才是按当前井组筛选的标准汇总。"}
                (target / "导出统计信息.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                (target / "README.txt").write_text("地数镜标准工区目录交付\n\n" + "本目录由资料数据库与定制井组生成。原始 LAS / DEV / MDB 保持只读复制；Well Head 与 Well Top 为按井组整理的 CSV。详见 导出统计信息.json、导出井清单与缺失项.csv 和 导出文件清单.csv。\n", encoding="utf-8")
            return jsonify({"ok": True, "path": str(target), "well_count": len(selected_wells), "copied": len(copied), "skipped": len(skipped), "summary": summary})
        except (OSError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/tree-card")
    def api_tree_card():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        category = request.args.get("category") or "other"
        item_id = request.args.get("item_id", type=int)
        try:
            with database.connect() as conn:
                return jsonify(insight_tools.tree_card_payload(conn, snapshot, curve_profile(snapshot), category, item_id))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/api/preflight/folder")
    def api_preflight_folder():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(insight_tools.preflight_folder(str(payload["path"])))
        except (KeyError, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/directory-translations/run")
    def api_run_directory_translation():
        """Create in-app filename aliases only; never rename source files."""
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            with database.connect() as conn:
                return jsonify(translation_tools.translate_folder(
                    conn,
                    str(Path(snapshot["project"]["root"]).resolve()),
                    str(payload["folder"]),
                    str(payload.get("custom_glossary") or ""),
                ))
        except (KeyError, OSError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/directory-translations")
    def api_directory_translations():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "尚未载入项目快照"}), 404
        folder = str(request.args.get("folder") or "").strip()
        if not folder:
            return jsonify({"error": "请先指定已翻译的目录"}), 400
        try:
            with database.connect() as conn:
                return jsonify(translation_tools.translation_page(
                    conn,
                    str(Path(snapshot["project"]["root"]).resolve()),
                    folder,
                    request.args.get("q", ""),
                    request.args.get("page", 1, type=int),
                    request.args.get("per_page", 160, type=int),
                ))
        except (OSError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/local-folder-picker")
    def api_local_folder_picker():
        if os.name != "nt":
            return jsonify({"error": "本机文件夹选择器当前仅支持 Windows"}), 501
        payload = request.get_json(silent=True) or {}
        initial = str(payload.get("initial") or "").strip()
        try:
            picker_env = os.environ.copy()
            picker_env["GEOINVENTORY_PICKER_INITIAL"] = initial
            # Use the modern Windows Common Item Dialog in folder-picking mode;
            # this is the Explorer-style dialog rather than BrowseForFolder.
            picker_script = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
[Flags] public enum FolderPickerOptions : uint { PickFolders=0x20, ForceFileSystem=0x40, NoChangeDirectory=0x08, PathMustExist=0x800 }
public enum ShellDisplayName : uint { FileSystemPath=0x80058000 }
[ComImport, Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")] public class FileOpenDialogCom { }
[ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IShellItem {
 void BindToHandler(IntPtr pbc,[MarshalAs(UnmanagedType.LPStruct)] Guid bhid,[MarshalAs(UnmanagedType.LPStruct)] Guid riid,out IntPtr ppv);
 void GetParent(out IShellItem item); void GetDisplayName(ShellDisplayName name,out IntPtr value);
 void GetAttributes(uint mask,out uint attributes); void Compare(IShellItem item,uint hint,out int order);
}
[ComImport, Guid("42F85136-DB7E-439C-85F1-E4075D135FC8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IFileDialog {
 [PreserveSig] int Show(IntPtr parent); void SetFileTypes(uint count,IntPtr filters); void SetFileTypeIndex(uint index);
 void GetFileTypeIndex(out uint index); void Advise(IntPtr events,out uint cookie); void Unadvise(uint cookie);
 void SetOptions(FolderPickerOptions options); void GetOptions(out FolderPickerOptions options);
 void SetDefaultFolder(IShellItem item); void SetFolder(IShellItem item); void GetFolder(out IShellItem item);
 void GetCurrentSelection(out IShellItem item); void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
 void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string name); void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
 void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text); void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
 void GetResult(out IShellItem item); void AddPlace(IShellItem item,uint alignment);
 void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string extension); void Close(int error);
 void SetClientGuid(ref Guid guid); void ClearClientData(); void SetFilter(IntPtr filter);
}
public static class NativeFolderPicker {
 [DllImport("shell32.dll",CharSet=CharSet.Unicode,PreserveSig=false)]
 static extern void SHCreateItemFromParsingName(string path,IntPtr context,[MarshalAs(UnmanagedType.LPStruct)] Guid riid,out IShellItem item);
 public static string Pick(string initialPath) {
  IFileDialog dialog=(IFileDialog)new FileOpenDialogCom(); IShellItem initial=null,result=null;
  try {
   FolderPickerOptions options; dialog.GetOptions(out options);
   dialog.SetOptions(options|FolderPickerOptions.PickFolders|FolderPickerOptions.ForceFileSystem|FolderPickerOptions.NoChangeDirectory|FolderPickerOptions.PathMustExist);
   dialog.SetTitle("选择本地文件夹"); dialog.SetOkButtonLabel("选择此文件夹");
   if(!String.IsNullOrWhiteSpace(initialPath)&&Directory.Exists(initialPath)){SHCreateItemFromParsingName(initialPath,IntPtr.Zero,typeof(IShellItem).GUID,out initial);dialog.SetFolder(initial);}
   int hr=dialog.Show(IntPtr.Zero); if(hr==unchecked((int)0x800704C7)) return null; Marshal.ThrowExceptionForHR(hr);
   dialog.GetResult(out result); IntPtr pointer; result.GetDisplayName(ShellDisplayName.FileSystemPath,out pointer);
   try{return Marshal.PtrToStringUni(pointer);}finally{Marshal.FreeCoTaskMem(pointer);}
  } finally {
   if(result!=null)Marshal.FinalReleaseComObject(result); if(initial!=null)Marshal.FinalReleaseComObject(initial); Marshal.FinalReleaseComObject(dialog);
  }
 }
}
'@
$selected = [NativeFolderPicker]::Pick($env:GEOINVENTORY_PICKER_INITIAL)
if ($selected) { [Console]::Write($selected) }
"""
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", picker_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=picker_env,
                timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise OSError(completed.stderr.strip() or f"PowerShell 返回 {completed.returncode}")
            selected = completed.stdout.strip()
            return jsonify({"path": selected or None, "cancelled": not bool(selected)})
        except Exception as exc:
            return jsonify({"error": f"无法打开本机文件夹选择器：{exc}"}), 500

    @app.post("/api/local-file-picker")
    def api_local_file_picker():
        if os.name != "nt":
            return jsonify({"error": "本机文件选择器当前仅支持 Windows"}), 501
        payload = request.get_json(silent=True) or {}
        initial = str(payload.get("initial") or "").strip()
        file_kind = str(payload.get("kind") or "excel").strip().lower()
        if file_kind != "excel":
            return jsonify({"error": "当前仅提供 Excel 文件选择"}), 400
        try:
            picker_env = os.environ.copy()
            picker_env["GEOINVENTORY_PICKER_INITIAL"] = initial
            picker_script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '选择 Petrel 射孔导出的 Excel 文件'
$dialog.Filter = 'Excel 工作簿 (*.xlsx;*.xlsm)|*.xlsx;*.xlsm|所有文件 (*.*)|*.*'
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
if ($env:GEOINVENTORY_PICKER_INITIAL) {
    $initial = $env:GEOINVENTORY_PICKER_INITIAL
    if (Test-Path -LiteralPath $initial -PathType Leaf) {
        $dialog.InitialDirectory = Split-Path -Parent $initial
        $dialog.FileName = Split-Path -Leaf $initial
    } elseif (Test-Path -LiteralPath $initial -PathType Container) {
        $dialog.InitialDirectory = $initial
    }
}
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Size = New-Object System.Drawing.Size(1, 1)
try {
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($dialog.FileName)
    }
} finally {
    $dialog.Dispose()
    $owner.Dispose()
}
"""
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", picker_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=picker_env,
                timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise OSError(completed.stderr.strip() or f"PowerShell 返回 {completed.returncode}")
            selected = completed.stdout.strip()
            return jsonify({"path": selected or None, "cancelled": not bool(selected)})
        except Exception as exc:
            return jsonify({"error": f"无法打开本机文件选择器：{exc}"}), 500

    @app.post("/api/format-conversion/perforation/preview")
    def api_perforation_conversion_preview():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(conversion_tools.preview_petrel_perforations(payload.get("path") or ""))
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/format-conversion/perforation/upload")
    def api_perforation_conversion_upload():
        """Keep browser-selected Excel files in the local working cache only.

        A browser cannot disclose a selected file's absolute source path.  The
        short-lived copy lets the existing read-only parser work while keeping
        the user-selected source workbook untouched.
        """
        files = [file for file in request.files.getlist("files") if file and file.filename]
        if not files:
            return jsonify({"error": "请选择至少一个 Excel 文件"}), 400
        if len(files) > 500:
            return jsonify({"error": "一次最多加入 500 个 Excel 文件"}), 400

        cache_dir = active_upload_dir() / "format_conversion"
        cache_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        items = []
        for index, uploaded in enumerate(files, 1):
            original_name = str(uploaded.filename or "").strip()
            suffix = Path(original_name).suffix.lower()
            if suffix not in {".xlsx", ".xlsm"}:
                return jsonify({"error": f"{original_name or '所选文件'} 不是支持的 .xlsx / .xlsm 文件"}), 400
            safe_name = secure_filename(Path(original_name).name) or f"perforation_{index}{suffix}"
            target = cache_dir / f"{stamp}_{index:03d}_{safe_name}"
            uploaded.save(target)
            items.append({
                "path": str(target),
                "filename": original_name,
                "bytes": target.stat().st_size,
            })
        return jsonify({"items": items})

    @app.post("/api/format-conversion/perforation/export")
    def api_perforation_conversion_export():
        payload = request.get_json(silent=True) or {}
        output = None
        try:
            source = Path(str(payload.get("path") or "")).expanduser()
            descriptor, raw_path = tempfile.mkstemp(
                prefix="perforation_standard_", suffix=".xlsx", dir=str(process_directory())
            )
            os.close(descriptor)
            output = Path(raw_path)
            summary = conversion_tools.write_petrel_perforation_workbook(source, output)
            stem = secure_filename(source.stem) or "Petrel_perforations"
            response = send_file(
                output,
                as_attachment=True,
                download_name=f"{stem}_standard_perforations.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            response.headers["X-GeoInventory-Records"] = str(summary["records"])
            response.headers["X-GeoInventory-Wells"] = str(summary["wells"])
            response.call_on_close(lambda: output.unlink(missing_ok=True))
            return response
        except (OSError, RuntimeError, ValueError) as exc:
            if output:
                output.unlink(missing_ok=True)
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/format-conversion/perforation/export-batch")
    def api_perforation_conversion_export_batch():
        payload = request.get_json(silent=True) or {}
        raw_paths = payload.get("paths") or []
        if not isinstance(raw_paths, list):
            return jsonify({"error": "待转换文件格式不正确"}), 400
        paths = [str(path).strip() for path in raw_paths if str(path).strip()]
        if not paths:
            return jsonify({"error": "请先勾选至少一个待转换的 Excel 文件"}), 400
        if len(paths) > 500:
            return jsonify({"error": "一次最多转换 500 个 Excel 文件"}), 400
        output_dir = Path(str(payload.get("output_dir") or "").strip()).expanduser()
        if not output_dir.is_dir():
            return jsonify({"error": "输出目录不存在或无法访问，请重新选择有效的本机文件夹"}), 400

        converted: list[dict] = []
        failed: list[dict] = []
        used_names: set[str] = set()
        for raw_path in paths:
            source = Path(raw_path).expanduser()
            base = secure_filename(source.stem) or "Petrel_perforations"
            filename = f"{base}_standard_perforations.xlsx"
            counter = 2
            while filename.lower() in used_names or (output_dir / filename).exists():
                filename = f"{base}_standard_perforations_{counter}.xlsx"
                counter += 1
            used_names.add(filename.lower())
            destination = output_dir / filename
            try:
                summary = conversion_tools.write_petrel_perforation_workbook(source, destination)
                converted.append({
                    "source_file": source.name,
                    "output_file": filename,
                    "wells": summary["wells"],
                    "records": summary["records"],
                    "status": "已转换",
                    "error": "",
                })
            except (OSError, RuntimeError, ValueError) as exc:
                failed.append({
                    "source_file": source.name or raw_path,
                    "output_file": "",
                    "wells": "",
                    "records": "",
                    "status": "失败",
                    "error": str(exc),
                })

        manifest_name = "射孔数据转换清单.csv"
        manifest_path = output_dir / manifest_name
        manifest_counter = 2
        while manifest_path.exists():
            manifest_path = output_dir / f"射孔数据转换清单_{manifest_counter}.csv"
            manifest_counter += 1
        manifest_path.write_bytes(_csv_bytes(
            ["source_file", "output_file", "wells", "records", "status", "error"],
            [*converted, *failed],
        ))
        return jsonify({
            "output_dir": str(output_dir),
            "converted": converted,
            "failed": failed,
            "manifest": str(manifest_path),
        })

    @app.post("/api/import-jobs")
    def api_start_upload_job():
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "请选择文件"}), 400
        options = {
            "data_type": request.form.get("data_type", "auto"),
            "batch": request.form.get("batch"),
            "version": request.form.get("version"),
            "crs": request.form.get("crs"),
        }
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        paths = []
        for index, uploaded in enumerate(files):
            safe_name = secure_filename(uploaded.filename or "") or f"upload_{index}"
            target = active_upload_dir() / f"{stamp}_{index}_{safe_name}"
            uploaded.save(target)
            paths.append(target)
        process_directory = workspace_context["path"] / "process" if workspace_context["path"] else DATA_DIR / "process"
        return jsonify(job_manager.submit(
            paths, database.path, options, process_directory,
            completion_callback=reconcile_imported_sources,
        )), 202

    @app.post("/api/import-jobs/path")
    def api_start_path_job():
        payload = request.get_json(silent=True) or {}
        requested_paths = payload.get("paths") or ([payload["path"]] if payload.get("path") else [])
        if not requested_paths:
            return jsonify({"error": "请输入本机文件路径"}), 400
        paths: list[str] = []
        seen: set[str] = set()
        for raw in requested_paths:
            target = Path(str(raw)).expanduser().resolve()
            if target.is_file():
                candidates = [target]
            elif target.is_dir():
                candidates = [
                    path for path in target.rglob("*")
                    if path.is_file() and not any(part.lower().endswith(".nvt") for part in path.relative_to(target).parts)
                    and insight_tools.predict_file_type(path)["importable"]
                ]
            else:
                return jsonify({"error": f"文件或文件夹不存在：{target}"}), 400
            for path in candidates:
                key = str(path).casefold()
                if key not in seen:
                    seen.add(key)
                    paths.append(str(path))
        if not paths:
            return jsonify({"error": "指定目录内没有可导入文件"}), 400
        if len(paths) > 100_000:
            return jsonify({"error": "可导入文件超过 100,000 个，请缩小目录范围或先使用快速预统计筛选"}), 400
        options = {key: payload.get(key) for key in ("data_type", "batch", "version", "crs")}
        options["data_type"] = options.get("data_type") or "auto"
        process_directory = workspace_context["path"] / "process" if workspace_context["path"] else DATA_DIR / "process"
        try:
            return jsonify(job_manager.submit(
                paths, database.path, options, process_directory,
                completion_callback=reconcile_imported_sources,
            )), 202
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/import-jobs/<job_id>")
    def api_import_job(job_id: str):
        try:
            return jsonify(job_manager.status(job_id))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.get("/api/import-jobs")
    def api_import_jobs():
        return jsonify(job_manager.recent())

    @app.post("/api/import")
    def api_import():
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "请选择文件"}), 400
        options = {
            "data_type": request.form.get("data_type", "auto"),
            "batch": request.form.get("batch"),
            "version": request.form.get("version"),
            "crs": request.form.get("crs"),
        }
        results, errors = [], []
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        for index, uploaded in enumerate(files):
            safe_name = secure_filename(uploaded.filename or "") or f"upload_{index}"
            target = active_upload_dir() / f"{stamp}_{index}_{safe_name}"
            uploaded.save(target)
            try:
                result = importer.import_path(target, **options)
                results.append(result.__dict__)
            except Exception as exc:
                errors.append({"filename": uploaded.filename, "error": str(exc)})
        if results:
            try:
                reconcile_imported_sources(results)
            except Exception as exc:
                errors.append({"filename": "工区资料索引", "error": str(exc)})
        status = 200 if results else 400
        return jsonify({"results": results, "errors": errors}), status

    @app.post("/api/import-path")
    def api_import_path():
        payload = request.get_json(silent=True) or {}
        raw_paths = payload.get("paths") or ([payload["path"]] if payload.get("path") else [])
        if not raw_paths:
            return jsonify({"error": "请输入本机文件路径"}), 400
        results, errors = [], []
        for raw_path in raw_paths:
            try:
                result = importer.import_path(
                    raw_path,
                    data_type=payload.get("data_type", "auto"),
                    batch=payload.get("batch"),
                    version=payload.get("version"),
                    crs=payload.get("crs"),
                )
                results.append(result.__dict__)
            except Exception as exc:
                errors.append({"filename": str(raw_path), "error": str(exc)})
        if results:
            try:
                reconcile_imported_sources(results)
            except Exception as exc:
                errors.append({"filename": "工区资料索引", "error": str(exc)})
        return jsonify({"results": results, "errors": errors}), (200 if results else 400)

    @app.get("/api/export/wells.csv")
    def export_wells():
        polygon_id = request.args.get("polygon_id")
        bbox = request.args.get("bbox")
        with database.connect() as conn:
            if bbox:
                try:
                    x_min, y_min, x_max, y_max = [float(value) for value in bbox.split(",")]
                except (TypeError, ValueError):
                    return jsonify({"error": "bbox 应为 xmin,ymin,xmax,ymax"}), 400
                rows = [row for row in combined_wells(conn) if row.get("x") is not None and x_min <= row["x"] <= x_max and y_min <= row["y"] <= y_max]
            elif polygon_id and polygon_id.startswith("project:"):
                snapshot = snapshot_or_none()
                rows = combined_wells(conn)
                polygon = next((row for row in filter_tools.project_polygons(conn, snapshot) if row["id"] == polygon_id), None) if snapshot else None
                rows = [row for row in rows if polygon and row.get("x") is not None and row.get("y") is not None and filter_tools.point_in_project_polygon(row["x"], row["y"], polygon)]
            elif polygon_id:
                rows = analytics.wells(conn, int(polygon_id))
            else:
                rows = combined_wells(conn)
        stream = io.StringIO()
        headers = ["canonical_name", "uwi", "x", "y", "crs", "kb_elevation", "total_depth", "confidence_score", "preferred_type", "source_count", "source_types", "curve_count", "log_start_md", "log_stop_md"]
        writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row = {**row, "source_types": ";".join(row["source_types"])}
            writer.writerow(row)
        return Response("\ufeff" + stream.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=wells.csv"})

    @app.get("/api/data-export/summary")
    def api_data_export_summary():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "请先扫描或打开一个工区，再导出原始数据"}), 404
        with database.connect() as conn:
            sync_imported_sources_to_catalog(conn, snapshot["project"]["root"])
            wells, filter_meta = export_scope_project_wells(conn, snapshot)
            profile = curve_profile(snapshot)
            complete = curve_tools.curve_profile_is_current(conn, snapshot, profile)
            types = export_curve_type_inventory(conn, snapshot, profile, {row["project_key"] for row in wells})
            dev_path_counts = [len({str(Path(path).resolve()) for path in row.get("_dev_paths", [])}) for row in wells]
            catalog_las = conn.execute(
                "SELECT COUNT(*) count FROM project_catalog_items WHERE project_root=? AND category_key='well_logs'",
                (str(Path(snapshot["project"]["root"]).resolve()),),
            ).fetchone()["count"]
        return jsonify({
            "types": types,
            "profile_complete": complete,
            "profile_source": profile.get("source") or "代表性 LAS 画像",
            "profile_las_files": profile.get("las_files", len(profile.get("wells", []))),
            "catalog_las_files": catalog_las,
            "well_count": len(wells),
            "trajectory": {
                "candidate_wells": sum(1 for count in dev_path_counts if count),
                "multiple_source_wells": sum(1 for count in dev_path_counts if count > 1),
                "source_files": sum(dev_path_counts),
            },
            "filter": filter_meta,
        })

    @app.post("/api/data-export/prepare-las")
    def api_data_export_prepare_las():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "请先扫描或打开一个工区"}), 404
        with database.connect() as conn:
            profile, built = complete_las_profile_for_export(conn, snapshot)
        return jsonify({
            "ok": True,
            "built": built,
            "las_files": profile.get("las_files", 0),
            "sample_wells": profile.get("sample_wells", 0),
            "errors": profile.get("errors", []),
        })

    @app.get("/api/data-export/well-las")
    def api_data_export_well_las_search():
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "请先扫描或打开一个工区"}), 404
        query = str(request.args.get("q") or "").strip()
        if not query:
            return jsonify({"query": "", "wells": [], "profile_complete": False})
        with database.connect() as conn:
            profile, _ = complete_las_profile_for_export(conn, snapshot)
        query_key = normalize_well_name(query)
        groups: dict[str, dict[str, Any]] = {}
        for row in profile.get("wells", []):
            well_key = str(row.get("well_key") or "")
            well_name = str(row.get("well_name") or well_key)
            filename = str(row.get("filename") or "")
            haystack = f"{well_name} {well_key} {filename}".upper()
            compact = normalize_well_name(haystack)
            if query.upper() not in haystack and (not query_key or query_key not in compact):
                continue
            group = groups.setdefault(well_key, {
                "well_key": well_key, "well_name": well_name, "files": [], "curves": {},
                "exact": bool(query_key and query_key == normalize_well_name(well_key)),
            })
            file_curves = []
            for curve in row.get("curves", []):
                mnemonic = str(curve.get("mnemonic") or "").upper().strip()
                if not mnemonic:
                    continue
                item = {
                    "mnemonic": mnemonic, "unit": curve.get("unit"),
                    "description": curve.get("description"), "filename": filename,
                }
                file_curves.append(item)
                aggregate = group["curves"].setdefault(mnemonic, {
                    "mnemonic": mnemonic, "units": set(), "descriptions": set(), "files": set(),
                })
                if curve.get("unit"):
                    aggregate["units"].add(str(curve["unit"]))
                if curve.get("description"):
                    aggregate["descriptions"].add(str(curve["description"]))
                aggregate["files"].add(filename)
            group["files"].append({
                "filename": filename, "file_path": row.get("file_path"),
                "start": row.get("start"), "stop": row.get("stop"), "step": row.get("step"),
                "depth_unit": row.get("depth_unit"), "curves": file_curves,
            })
        result = []
        for group in groups.values():
            group["curves"] = [
                {"mnemonic": item["mnemonic"], "units": sorted(item["units"]),
                 "descriptions": sorted(item["descriptions"]), "file_count": len(item["files"]),
                 "filenames": sorted(item["files"])}
                for item in group["curves"].values()
            ]
            group["curves"].sort(key=lambda item: item["mnemonic"])
            group["files"].sort(key=lambda item: item["filename"].lower())
            group["curve_count"] = len(group["curves"])
            group["file_count"] = len(group["files"])
            result.append(group)
        result.sort(key=lambda item: (not item["exact"], item["well_name"].upper()))
        return jsonify({
            "query": query, "wells": result[:30], "match_count": len(result),
            "profile_complete": profile.get("schema_version") == 2 and (profile.get("scope") or profile.get("source")) == "all_las_headers",
        })

    @app.post("/api/data-export/well-las")
    def api_data_export_well_las():
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        if not snapshot:
            return jsonify({"error": "请先扫描或打开一个工区"}), 404
        well_key = normalize_well_name(payload.get("well_key"))
        mnemonics = {str(value).upper().strip() for value in payload.get("mnemonics", []) if str(value).strip()}
        if not well_key:
            return jsonify({"error": "请选择一口井"}), 400
        if not mnemonics:
            return jsonify({"error": "请至少勾选一条曲线；DEPTH 会自动附加"}), 400
        if len(mnemonics) > 200:
            return jsonify({"error": "单次最多导出 200 条曲线"}), 400
        step_raw = payload.get("resample_step")
        try:
            resample_step = float(step_raw) if step_raw not in (None, "") else None
            if resample_step is not None and (not math.isfinite(resample_step) or resample_step <= 0):
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "重采样间隔必须为空或大于 0"}), 400
        archive = None
        try:
            with database.connect() as conn:
                profile, profile_built = complete_las_profile_for_export(conn, snapshot)
                sources = []
                project_root = str(Path(snapshot["project"]["root"]).resolve())
                for row in profile.get("wells", []):
                    if normalize_well_name(row.get("well_key")) != well_key:
                        continue
                    available = {str(curve.get("mnemonic") or "").upper().strip() for curve in row.get("curves", [])}
                    selected = sorted(mnemonics & available)
                    if not selected:
                        continue
                    path = Path(str(row.get("file_path") or ""))
                    if not path.is_file():
                        fallback = conn.execute(
                            """SELECT file_path FROM project_catalog_items
                               WHERE project_root=? AND category_key='well_logs' AND filename=? LIMIT 1""",
                            (project_root, row.get("filename")),
                        ).fetchone()
                        path = Path(str(fallback["file_path"])) if fallback else path
                    if path.is_file():
                        sources.append({"path": path, "selected": selected, "well_name": row.get("well_name") or well_key})
                if not sources:
                    raise ValueError("该井没有包含所选曲线的可访问 LAS 文件")
                archive = archive_target("well_curve_export_")
                manifest = []
                used_names: dict[str, int] = {}
                with tempfile.TemporaryDirectory(prefix="well_las_", dir=str(process_directory())) as raw_temp:
                    temporary = Path(raw_temp)
                    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
                        for index, source in enumerate(sorted(sources, key=lambda item: str(item["path"]).lower()), 1):
                            base = f"{source['well_name']}_{source['path'].stem}_selected"
                            output_name = _las_curve_filename(base, used_names, index)
                            output_path = temporary / output_name
                            meta = las_export_tools.build_selected_las(
                                source["path"], source["selected"], output_path, resample_step=resample_step,
                            )
                            bundle.write(output_path, f"LAS/{output_name}")
                            manifest.append({
                                "well_name": source["well_name"], "well_key": well_key,
                                "source_file": source["path"].name, "output_file": output_name,
                                "depth_mnemonic": meta["depth_mnemonic"], "depth_unit": meta["depth_unit"],
                                "selected_mnemonics": " | ".join(meta["mnemonics"]),
                                "start": meta["start"], "stop": meta["stop"], "step": meta["step"],
                                "sample_rows": meta["sample_rows"], "resampled": "yes" if meta["resampled"] else "no",
                            })
                        bundle.writestr("LAS/导出清单.csv", _csv_bytes(
                            ["well_name", "well_key", "source_file", "output_file", "depth_mnemonic", "depth_unit", "selected_mnemonics", "start", "stop", "step", "sample_rows", "resampled"],
                            manifest,
                        ))
                        bundle.writestr("README.txt", (
                            "地数镜 单井选曲线标准 LAS 导出\n\n"
                            "每份输出均自动包含源文件的 DEPTH/MD 道，并仅保留人工勾选的曲线。\n"
                            "曲线分布在不同源 LAS 时分别输出，不擅自跨文件拼接。\n"
                            f"重采样：{f'{resample_step:g}（源深度单位）' if resample_step is not None else '保持各源文件原始采样'}。\n"
                            f"首次建立完整 LAS 头段画像：{'是' if profile_built else '否'}。\n"
                        ))
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return archive_response(archive, f"{well_key}_选曲线标准LAS_{stamp}.zip")
        except (OSError, ValueError, sqlite3.Error) as exc:
            if archive:
                archive.unlink(missing_ok=True)
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/data-export/las")
    def api_data_export_las():
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        selected_types = {str(value) for value in payload.get("type_keys", []) if str(value).strip()}
        requested_keys = payload.get("well_keys") or []
        if not snapshot:
            return jsonify({"error": "请先扫描或打开一个工区"}), 404
        if not selected_types:
            return jsonify({"error": "请至少勾选一个测井曲线类型"}), 400
        archive = None
        try:
            with database.connect() as conn:
                wells, filter_meta = export_scope_project_wells(conn, snapshot, requested_keys)
                allowed_wells = {row["project_key"] for row in wells}
                profile, profile_built = complete_las_profile_for_export(conn, snapshot)
                type_items = export_curve_type_inventory(conn, snapshot, profile, allowed_wells)
                type_map = {row["type_key"]: row for row in type_items}
                unknown = selected_types - set(type_map)
                if unknown:
                    raise ValueError(f"选中的曲线类型当前没有可导出的 LAS：{', '.join(sorted(unknown))}")
                mnemonic_types = {}
                for type_key in selected_types:
                    for mnemonic in type_map[type_key]["mnemonics"]:
                        mnemonic_types.setdefault(mnemonic.upper(), set()).add(type_key)
                matches = {}
                for well in profile.get("wells", []):
                    well_key = well.get("well_key")
                    if not well_key or well_key not in allowed_wells:
                        continue
                    matched_mnemonics = {
                        str(curve.get("mnemonic") or "").upper()
                        for curve in well.get("curves", [])
                        if str(curve.get("mnemonic") or "").upper() in mnemonic_types
                    }
                    path = Path(str(well.get("file_path") or ""))
                    if not matched_mnemonics or not path.is_file():
                        continue
                    identity = str(path.resolve())
                    item = matches.setdefault(identity, {
                        "path": path, "well_keys": set(), "well_names": set(), "mnemonics": set(), "type_keys": set(),
                    })
                    item["well_keys"].add(well_key)
                    item["well_names"].add(str(well.get("well_name") or well_key))
                    item["mnemonics"].update(matched_mnemonics)
                    item["type_keys"].update(type_key for mnemonic in matched_mnemonics for type_key in mnemonic_types[mnemonic])
                if not matches:
                    raise ValueError("当前井点范围和曲线类型下没有可访问的 LAS 文件")
                project_root = snapshot["project"]["root"]
                archive = archive_target("las_export_")
                manifest = []
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
                    for index, item in enumerate(sorted(matches.values(), key=lambda row: str(row["path"]).lower()), 1):
                        source = item["path"]
                        fallback = f"LAS_{index:04d}{source.suffix or '.las'}"
                        bundle.write(source, archive_name("LAS", source, project_root, fallback))
                        manifest.append({
                            "well_name": " | ".join(sorted(item["well_names"])),
                            "well_key": " | ".join(sorted(item["well_keys"])),
                            "curve_types": " | ".join(sorted(item["type_keys"])),
                            "matched_mnemonics": " | ".join(sorted(item["mnemonics"])),
                            "source_file": source.name,
                            "source_path": str(source),
                        })
                    bundle.writestr("LAS/导出清单.csv", _csv_bytes(
                        ["well_name", "well_key", "curve_types", "matched_mnemonics", "source_file", "source_path"], manifest
                    ))
                    bundle.writestr("README.txt", (
                        "地数镜 LAS 导出\n\n"
                        "筛选口径：LAS 头段包含任一选中曲线类型的 mnemonic，即完整保留该 LAS 原文件。\n"
                        "本软件不截取曲线、不重采样、不改写原始 LAS。\n"
                        f"井点范围：{len(allowed_wells)} 口统一井；全局井点筛选：{'已激活' if filter_meta.get('active') else '未激活'}。\n"
                        f"本次打包：{len(matches)} 份 LAS；首次建立完整头段清单：{'是' if profile_built else '否'}。\n"
                    ))
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return archive_response(archive, f"LAS_曲线类型筛选_{stamp}.zip")
        except (OSError, ValueError, sqlite3.Error) as exc:
            if archive:
                archive.unlink(missing_ok=True)
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/data-export/trajectories")
    def api_data_export_trajectories():
        snapshot = snapshot_or_none()
        payload = request.get_json(silent=True) or {}
        requested_keys = payload.get("well_keys") or []
        if not snapshot:
            return jsonify({"error": "请先扫描或打开一个工区"}), 404
        archive = None
        try:
            with database.connect() as conn:
                wells, filter_meta = export_scope_project_wells(conn, snapshot, requested_keys)
                groups = {}
                for row in wells:
                    if not row.get("_dev_paths"):
                        continue
                    group_key = normalize_well_name(row.get("canonical_name")) or row["project_key"]
                    group = groups.setdefault(group_key, {
                        "well_name": row["canonical_name"], "well_keys": set(), "paths": set(),
                    })
                    group["well_keys"].add(row["project_key"])
                    group["paths"].update(str(Path(path)) for path in row["_dev_paths"])
                if not groups:
                    raise ValueError("当前井点范围内没有关联的 DEV 轨迹文件")
                archive = archive_target("dev_export_")
                project_root = snapshot["project"]["root"]
                manifest, exported, used_names = [], 0, set()
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
                    for index, group in enumerate(sorted(groups.values(), key=lambda row: row["well_name"].upper()), 1):
                        valid, invalid = [], []
                        for raw_path in sorted(group["paths"], key=str.lower):
                            path = Path(raw_path)
                            try:
                                stations = parse_dev_stations(path) if path.is_file() else []
                            except (OSError, ValueError):
                                stations = []
                            if len(stations) >= 2:
                                valid.append((path, len(stations)))
                            else:
                                invalid.append(str(path))
                        selected = sorted(valid, key=lambda item: (-item[1], str(item[0]).lower()))[0] if valid else None
                        record = {
                            "well_name": group["well_name"],
                            "unified_well_keys": " | ".join(sorted(group["well_keys"])),
                            "selected_file": selected[0].name if selected else "",
                            "selected_source_path": str(selected[0]) if selected else "",
                            "selected_station_count": selected[1] if selected else 0,
                            "valid_file_count": len(valid),
                            "empty_or_invalid_file_count": len(invalid),
                            "skipped_valid_duplicates": " | ".join(str(path) for path, _ in valid if not selected or path != selected[0]),
                            "skipped_empty_or_invalid": " | ".join(invalid),
                        }
                        manifest.append(record)
                        if not selected:
                            continue
                        source, _ = selected
                        base = secure_filename(group["well_name"]) or f"well_{index:04d}"
                        suffix = source.suffix or ".dev"
                        candidate = f"DEV/{base}{suffix}"
                        duplicate_index = 2
                        while candidate.lower() in used_names:
                            candidate = f"DEV/{base}_{duplicate_index}{suffix}"
                            duplicate_index += 1
                        used_names.add(candidate.lower())
                        bundle.write(source, candidate)
                        exported += 1
                    if not exported:
                        raise ValueError("已找到 DEV 文件，但全部为空或无法识别有效测点")
                    bundle.writestr("DEV/井轨迹导出清单.csv", _csv_bytes([
                        "well_name", "unified_well_keys", "selected_file", "selected_source_path", "selected_station_count",
                        "valid_file_count", "empty_or_invalid_file_count", "skipped_valid_duplicates", "skipped_empty_or_invalid",
                    ], manifest))
                    bundle.writestr("README.txt", (
                        "地数镜 DEV 轨迹导出\n\n"
                        "去重口径：以统一井名（含已确认别名）归集 DEV 文件。\n"
                        "空文件或有效测点少于 2 个的文件不导出；多份有效 DEV 仅选有效测点最多的一份。\n"
                        "若有效测点数相同，按源路径字母顺序稳定选择。完整判定过程见 DEV/井轨迹导出清单.csv。\n"
                        f"井点范围：{len(wells)} 口统一井；全局井点筛选：{'已激活' if filter_meta.get('active') else '未激活'}。\n"
                    ))
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return archive_response(archive, f"DEV_统一井去重导出_{stamp}.zip")
        except (OSError, ValueError, sqlite3.Error) as exc:
            if archive:
                archive.unlink(missing_ok=True)
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/workspace")
    def api_workspace():
        info = workspace_manager.describe(workspace_context["path"])
        if startup_workspace_info and startup_workspace_info.get("migrated"):
            info.update({"migrated": True, "previous_version": startup_workspace_info.get("previous_version")})
        info.update({"supported_format_version": WORKSPACE_FORMAT_VERSION, "current_app_version": APP_VERSION})
        if not info["active"]:
            snapshot = snapshot_or_none()
            if snapshot:
                root = Path(snapshot["project"]["root"])
                info["suggested_path"] = str(root.with_name(root.name + ".nvt"))
        return jsonify(info)

    @app.get("/api/projects")
    def api_projects():
        rows = workspace_manager.history()
        projects = []
        for row in rows:
            summary = row.get("project_summary") or {}
            # Old workspaces receive this compact summary the next time they
            # are opened.  Only then fall back once to their old snapshot.
            snapshot = None
            if not summary and row.get("exists"):
                snapshot = load_snapshot(Path(row["path"]) / "project_snapshot.json")
                project = (snapshot or {}).get("project") or {}
                categories = (snapshot or {}).get("categories") or []
                summary = {
                    "source_name": project.get("name"), "scanned_at": project.get("scanned_at"),
                    "total_files": int(project.get("total_files") or 0), "total_bytes": int(project.get("total_bytes") or 0),
                    "total_gb": project.get("total_gb"), "representative_count": int(project.get("representative_count") or 0),
                    "category_count": sum(1 for item in categories if item.get("files")),
                }
            projects.append({
                **row,
                "project_title": row.get("project_title") or summary.get("source_name") or row.get("name"),
                "source_name": summary.get("source_name") or row.get("name"),
                "scanned_at": summary.get("scanned_at"),
                "total_files": int(summary.get("total_files") or 0),
                "total_bytes": int(summary.get("total_bytes") or 0),
                "total_gb": summary.get("total_gb"),
                "representative_count": int(summary.get("representative_count") or 0),
                "category_count": int(summary.get("category_count") or 0),
                "version_current": row.get("format_version") == WORKSPACE_FORMAT_VERSION and row.get("app_version") == APP_VERSION,
            })
        if not projects:
            snapshot = snapshot_or_none()
            project = (snapshot or {}).get("project") or {}
            if project:
                projects.append({
                    "path": None, "name": project.get("name"),
                    "project_title": f"{project.get('name', '临时项目')}资料清查",
                    "source_root": project.get("root"), "source_name": project.get("name"),
                    "scanned_at": project.get("scanned_at"), "total_files": int(project.get("total_files") or 0),
                    "total_bytes": int(project.get("total_bytes") or 0), "total_gb": project.get("total_gb"),
                    "representative_count": int(project.get("representative_count") or 0),
                    "category_count": sum(1 for item in (snapshot or {}).get("categories", []) if item.get("files")),
                    "format_version": WORKSPACE_FORMAT_VERSION, "app_version": APP_VERSION,
                    "is_active": True, "exists": True, "version_current": True, "temporary": True,
                })
        return jsonify({
            "projects": projects,
            "active_path": str(workspace_context["path"]) if workspace_context["path"] else None,
            "app": {"name": "地数镜", "english_name": "GeoInventory", "version": APP_VERSION,
                    "workspace_format": WORKSPACE_FORMAT_VERSION, "local_only": True},
        })

    @app.post("/api/workspace/save")
    def api_workspace_save():
        payload = request.get_json(silent=True) or {}
        snapshot = snapshot_or_none()
        if not snapshot:
            return jsonify({"error": "请先扫描项目目录，再保存分析工区"}), 400
        try:
            info = workspace_manager.create_or_save(
                snapshot["project"]["root"], database.path, workspace_context["snapshot_path"], payload.get("path"), process_directory()
            )
            switch_workspace(info["path"])
            return jsonify(info)
        except (OSError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/workspace/open")
    def api_workspace_open():
        payload = request.get_json(silent=True) or {}
        try:
            info = workspace_manager.open(payload.get("path") or "")
            switch_workspace(info["path"])
            snapshot = snapshot_or_none()
            return jsonify({**info, "project": snapshot.get("project") if snapshot else None})
        except (OSError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/workspace/relocate-paths")
    def api_workspace_relocate_paths():
        if not workspace_context["path"]:
            return jsonify({"error": "请先打开需要更改路径的 .nvt 工区"}), 400
        payload = request.get_json(silent=True) or {}
        try:
            result = relocate_paths(
                workspace_context["path"], payload.get("old_prefix") or "",
                payload.get("new_prefix") or "", apply=payload.get("apply") is True,
            )
            return jsonify(result)
        except (OSError, ValueError, sqlite3.Error) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"error": "上传文件超过 2 GB；大型 SEG-Y 请使用“本机路径导入”"}), 413

    return app


def _port_is_open(host: str = SERVER_HOST, port: int = SERVER_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _open_local_browser() -> None:
    try:
        webbrowser.open(f"http://{SERVER_HOST}:{SERVER_PORT}/", new=1)
    except OSError:
        pass


def _run_diagnostics() -> int:
    checks: dict[str, object] = {
        "app_version": APP_VERSION,
        "software_identity": public_identity(APP_VERSION),
        "frozen": IS_FROZEN,
        "resource_dir": str(RESOURCE_DIR),
        "data_dir": str(DATA_DIR),
        "python": sys.version.split()[0],
        "listen": f"http://{SERVER_HOST}:{SERVER_PORT}/",
    }
    ok = True
    for module_name in ("flask", "openpyxl", "pyodbc"):
        try:
            module = __import__(module_name)
            if module_name == "flask":
                checks[module_name] = package_version("Flask")
            else:
                checks[module_name] = getattr(module, "__version__", getattr(module, "version", "available"))
        except Exception as exc:  # diagnostics must report optional runtime failures
            checks[module_name] = f"ERROR: {exc}"
            ok = False
    if not str(checks.get("pyodbc", "")).startswith("ERROR"):
        try:
            import pyodbc

            checks["odbc_drivers"] = list(pyodbc.drivers())
        except Exception as exc:
            checks["odbc_drivers"] = f"ERROR: {exc}"
            ok = False
    checks["folder_picker_backend"] = "Windows PowerShell / System.Windows.Forms"
    print(json.dumps(checks, ensure_ascii=True, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--diagnostics" in sys.argv:
        raise SystemExit(_run_diagnostics())
    if IS_FROZEN and _port_is_open():
        _open_local_browser()
        print("地数镜已经在运行，已打开现有页面。")
        raise SystemExit(0)
    pid_file = DATA_DIR / "GeoInventory.pid"
    if IS_FROZEN:
        pid_file.write_text(str(os.getpid()), encoding="ascii")

        def _remove_pid_file() -> None:
            try:
                if pid_file.is_file() and pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                    pid_file.unlink()
            except OSError:
                pass

        atexit.register(_remove_pid_file)
        if os.environ.get("GEOINVENTORY_NO_BROWSER") != "1":
            threading.Timer(1.2, _open_local_browser).start()
        print("地数镜 GeoInventory 已启动。关闭本窗口或按 Ctrl+C 可停止服务。")
        print(f"访问地址：http://{SERVER_HOST}:{SERVER_PORT}/")
        print(f"本机运行数据：{DATA_DIR}")
    create_app().run(host=SERVER_HOST, port=SERVER_PORT, debug=False, use_reloader=False, threaded=True)
