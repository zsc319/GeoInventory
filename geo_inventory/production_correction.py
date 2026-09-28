from __future__ import annotations

import calendar
import json
import math
import os
import shutil
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .db import Database
from . import ofm_mdb
from . import production_clustering


Progress = Callable[[int, str], None]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _latest(records: list[dict[str, Any]], field: str) -> Any:
    return next((row.get(field) for row in reversed(records) if row.get(field) is not None), None)


def _gas_measurement(records: list[dict[str, Any]]) -> tuple[float | None, bool]:
    for row in reversed(records):
        direct = _number(row.get("gas_rate"))
        if direct is not None:
            return direct, False
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            continue
        raw = metadata.get("raw") if isinstance(metadata, dict) else None
        if not isinstance(raw, dict):
            continue
        normalized = {str(key).upper().replace("_", ""): value for key, value in raw.items()}
        for alias in ("GAS", "QG", "GASRATE", "GASPRODUCTION"):
            value = _number(normalized.get(alias))
            if value is not None:
                basis = str(metadata.get("basis") or "")
                return value, "月量" in basis
    return None, False


def _month_days(value: Any) -> int:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return calendar.monthrange(parsed.year, parsed.month)[1]
    except ValueError:
        return 30


def _changed(left: Any, right: Any) -> bool:
    first, second = _number(left), _number(right)
    if first is None or second is None:
        return first is not second
    tolerance = max(1e-7, abs(first) * 1e-6, abs(second) * 1e-6)
    return abs(first - second) > tolerance


def rebuild_monthly(rows: Iterable[dict[str, Any]], progress: Progress | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rebuild derived production values without mutating imported rows.

    Daily oil/water/gas rates are authoritative.  Missing producing days on a
    positive-rate record are explicitly inferred from that calendar month.
    Monthly volumes, water cut and cumulative volumes are then rebuilt in that
    order.  Pressure, status and raw metadata are carried through unchanged.
    """
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for source in rows:
        item = dict(source)
        well_key = str(item.get("well_key") or "").strip()
        month = str(item.get("production_month") or "").strip()
        if well_key and month:
            grouped[well_key][month].append(item)

    result: list[dict[str, Any]] = []
    inferred_days = 0
    low_level_changes = 0
    well_items = sorted(grouped.items())
    for well_index, (well_key, months) in enumerate(well_items, 1):
        cumulative_oil = cumulative_water = cumulative_gas = 0.0
        oil_started = water_started = gas_started = False
        for month in sorted(months):
            records = sorted(months[month], key=lambda row: int(row.get("id") or 0))
            base = records[-1]
            oil_rate = _number(_latest(records, "oil_rate"))
            water_rate = _number(_latest(records, "water_rate"))
            gas_value, gas_is_monthly = _gas_measurement(records)
            raw_days = _number(_latest(records, "days_on"))
            has_positive_rate = any((value or 0) > 0 for value in (oil_rate, water_rate, gas_value))
            inferred = raw_days is None and has_positive_rate
            days_on = float(_month_days(month)) if inferred else raw_days
            gas_rate = gas_value / days_on if gas_is_monthly and gas_value is not None and days_on not in (None, 0) else gas_value
            if inferred:
                inferred_days += 1

            monthly_oil = oil_rate * days_on if oil_rate is not None and days_on is not None else _number(_latest(records, "monthly_oil"))
            monthly_water = water_rate * days_on if water_rate is not None and days_on is not None else _number(_latest(records, "monthly_water"))
            monthly_gas = gas_rate * days_on if gas_rate is not None and days_on is not None else None
            if monthly_oil is not None:
                cumulative_oil += monthly_oil
                oil_started = True
            if monthly_water is not None:
                cumulative_water += monthly_water
                water_started = True
            if monthly_gas is not None:
                cumulative_gas += monthly_gas
                gas_started = True

            if oil_rate is not None and water_rate is not None:
                liquid_rate = oil_rate + water_rate
                water_cut = water_rate / liquid_rate * 100 if liquid_rate > 0 else 0.0
            else:
                liquid_rate = _number(_latest(records, "liquid_rate"))
                water_cut = _number(_latest(records, "water_cut"))
                if water_cut is not None and 0 <= water_cut <= 1:
                    water_cut *= 100

            correction_note = "开井天数缺失，按自然月天数推算" if inferred else "按基础日率与开井天数重算"
            metadata = {"correction": {"basis": "daily_rates_x_days", "days_inferred": inferred, "note": correction_note}}
            corrected = {
                "source_id": int(base.get("source_id") or 0), "well_key": well_key,
                "well_name": str(_latest(records, "well_name") or well_key), "production_month": month,
                "days_on": days_on, "liquid_rate": liquid_rate, "oil_rate": oil_rate,
                "water_rate": water_rate, "gas_rate": gas_rate, "water_cut": water_cut,
                "monthly_oil": monthly_oil, "monthly_water": monthly_water, "monthly_gas": monthly_gas,
                "cumulative_oil": cumulative_oil if oil_started else None,
                "cumulative_water": cumulative_water if water_started else None,
                "cumulative_gas": cumulative_gas if gas_started else None,
                "pressure": _number(_latest(records, "pressure")),
                "pressure_type": _latest(records, "pressure_type"), "status": _latest(records, "status"),
                "metadata_json": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            }
            for field in ("days_on", "liquid_rate", "water_cut", "monthly_oil", "monthly_water", "cumulative_oil", "cumulative_water"):
                if _changed(_latest(records, field), corrected[field]):
                    low_level_changes += 1
            result.append(corrected)
        if progress and (well_index == len(well_items) or well_index % 25 == 0):
            progress(18 + int(35 * well_index / max(1, len(well_items))), f"正在重建第 {well_index:,} / {len(well_items):,} 口井")
    return result, {"well_count": len(well_items), "record_count": len(result), "inferred_days_count": inferred_days, "low_level_changes": low_level_changes}


def build_audit(original_rows: list[dict[str, Any]], corrected_rows: list[dict[str, Any]], events: list[dict[str, Any]], intervals: list[dict[str, Any]], wells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    original = production_clustering.build_feature_dataset(original_rows, events, intervals, wells)
    corrected = production_clustering.build_feature_dataset(corrected_rows, events, intervals, wells)
    definitions = {row["key"]: row for row in corrected["indicators"]}
    original_by_key = {row["well_key"]: row for row in original["wells"]}
    audit = []
    for row in corrected["wells"]:
        before = original_by_key.get(row["well_key"], {}).get("values", {})
        after = row.get("values", {})
        for key, definition in definitions.items():
            if key in {"nearest_distance", "perforation_length", "formation_count", "event_count", "intervention_response"}:
                continue
            left, right = before.get(key), after.get(key)
            changed = _changed(left, right)
            reason = "由基础日率与生产天数重新统计" if changed else "原值与重算值一致，无需修正"
            if key == "uptime_ratio" and changed:
                reason = "缺失开井天数已按自然月天数补齐后重算"
            audit.append({
                "well_key": row["well_key"], "well_name": row["well_name"], "field_key": key,
                "field_label": definition["label"], "unit": definition.get("unit"),
                "original_value": _number(left), "corrected_value": _number(right),
                "changed": int(changed), "reason": reason,
            })
    return audit


def _drop_if_exists(cursor, table: str) -> None:
    names = {str(row.table_name).upper() for row in cursor.tables(tableType="TABLE")}
    if table.upper() in names:
        cursor.execute(f"DROP TABLE [{table}]")


def write_corrected_mdb(source_path: Path, target_path: Path, corrected_rows: list[dict[str, Any]], audit_rows: list[dict[str, Any]], progress: Progress | None = None) -> str:
    """Create an atomic, writable copy containing standardized correction tables."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_name(f".{target_path.stem}-{uuid.uuid4().hex}.tmp.mdb")
    shutil.copy2(source_path, temporary)
    connection = None
    try:
        import pyodbc

        driver = ofm_mdb.access_driver()
        connection = pyodbc.connect(f"DRIVER={{{driver}}};DBQ={temporary};", autocommit=False, timeout=30)
        cursor = connection.cursor()
        _drop_if_exists(cursor, "Production_Corrected")
        _drop_if_exists(cursor, "Production_Correction_Audit")
        cursor.execute("""CREATE TABLE [Production_Corrected] (
            [WellKey] TEXT(100), [WellName] TEXT(255), [ProductionDate] DATETIME,
            [DaysOn] DOUBLE, [OilRate] DOUBLE, [WaterRate] DOUBLE, [GasRate] DOUBLE,
            [LiquidRate] DOUBLE, [WaterCutPct] DOUBLE, [MonthlyOil] DOUBLE,
            [MonthlyWater] DOUBLE, [MonthlyGas] DOUBLE, [CumulativeOil] DOUBLE,
            [CumulativeWater] DOUBLE, [CumulativeGas] DOUBLE, [Pressure] DOUBLE,
            [PressureType] TEXT(100), [WellStatus] TEXT(255), [CorrectionNote] MEMO)""")
        cursor.execute("""CREATE TABLE [Production_Correction_Audit] (
            [WellKey] TEXT(100), [WellName] TEXT(255), [FieldKey] TEXT(100),
            [FieldLabel] TEXT(255), [Unit] TEXT(50), [OriginalValue] DOUBLE,
            [CorrectedValue] DOUBLE, [Changed] YESNO, [Reason] TEXT(255))""")
        insert_sql = """INSERT INTO [Production_Corrected] ([WellKey],[WellName],[ProductionDate],[DaysOn],[OilRate],[WaterRate],[GasRate],[LiquidRate],[WaterCutPct],[MonthlyOil],[MonthlyWater],[MonthlyGas],[CumulativeOil],[CumulativeWater],[CumulativeGas],[Pressure],[PressureType],[WellStatus],[CorrectionNote]) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        total = max(1, len(corrected_rows))
        for offset in range(0, len(corrected_rows), 1000):
            chunk = corrected_rows[offset:offset + 1000]
            cursor.executemany(insert_sql, [(
                row["well_key"], row["well_name"], datetime.fromisoformat(str(row["production_month"]).replace("Z", "+00:00")).replace(tzinfo=None),
                row["days_on"], row["oil_rate"], row["water_rate"], row["gas_rate"], row["liquid_rate"], row["water_cut"],
                row["monthly_oil"], row["monthly_water"], row["monthly_gas"], row["cumulative_oil"], row["cumulative_water"],
                row["cumulative_gas"], row["pressure"], row["pressure_type"], row["status"],
                json.loads(row["metadata_json"])["correction"]["note"],
            ) for row in chunk])
            if progress and (offset == 0 or offset + len(chunk) == len(corrected_rows) or offset % 10000 == 0):
                progress(67 + int(22 * (offset + len(chunk)) / total), f"正在写入校正 MDB：{offset + len(chunk):,} / {len(corrected_rows):,} 条")
        cursor.executemany("""INSERT INTO [Production_Correction_Audit] ([WellKey],[WellName],[FieldKey],[FieldLabel],[Unit],[OriginalValue],[CorrectedValue],[Changed],[Reason]) VALUES (?,?,?,?,?,?,?,?,?)""", [(
            row["well_key"], row["well_name"], row["field_key"], row["field_label"], row["unit"],
            row["original_value"], row["corrected_value"], bool(row["changed"]), row["reason"],
        ) for row in audit_rows])
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary, target_path)
        return driver
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
                connection.close()
            except Exception:
                pass
        temporary.unlink(missing_ok=True)
        raise


class ProductionCorrectionManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def submit(self, database_path: str | Path, workspace_path: str | Path) -> dict[str, Any]:
        with self._lock:
            active = next((dict(job) for job in self._jobs.values() if job.get("status") in {"queued", "running"}), None)
            if active:
                return active
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "status": "queued", "progress": 0, "message": "等待校正", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run, args=(job_id, Path(database_path), Path(workspace_path)), daemon=True, name=f"production-correction-{job_id[:8]}")
        thread.start()
        return dict(job)

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return dict(self._jobs[job_id])

    def _run(self, job_id: str, database_path: Path, workspace_path: Path) -> None:
        def progress(value: int, message: str) -> None:
            self._update(job_id, status="running", progress=max(0, min(99, value)), message=message)

        self._update(job_id, status="running", progress=3, message="正在检查原始 MDB 与基础字段")
        try:
            # Fail before scanning hundreds of thousands of rows when the
            # current runtime lacks pyodbc or a same-bitness Access driver.
            # access_driver() also converts ImportError into an actionable
            # Chinese diagnostic instead of exposing "No module named ...".
            ofm_mdb.access_driver()
            database = Database(database_path)
            with database.connect() as conn:
                source = conn.execute("""SELECT os.source_id,os.file_path FROM ofm_sources os
                    JOIN sources s ON s.id=os.source_id WHERE s.status='ready' ORDER BY os.id DESC LIMIT 1""").fetchone()
                if not source or not Path(source["file_path"]).is_file():
                    raise RuntimeError("没有可读取的原始 MDB，请先在生产动态中挂载 MDB")
                source_id, source_path = int(source["source_id"]), Path(source["file_path"])
                original_rows = [dict(row) for row in conn.execute("SELECT * FROM production_monthly WHERE source_id=? ORDER BY well_key,production_month,id", (source_id,))]
                events = [dict(row) for row in conn.execute("SELECT * FROM production_events WHERE source_id=? ORDER BY well_key,event_date,id", (source_id,))]
                intervals = [dict(row) for row in conn.execute("SELECT * FROM production_intervals WHERE source_id=? ORDER BY well_key,top_md,id", (source_id,))]
                wells = [dict(row) for row in conn.execute("SELECT * FROM ofm_wells WHERE source_id=? ORDER BY well_key,id", (source_id,))]
            if not original_rows:
                raise RuntimeError("原始 MDB 没有可校正的生产月度记录")
            progress(15, f"已读取 {len(original_rows):,} 条原始生产记录")
            corrected_rows, summary = rebuild_monthly(original_rows, progress)
            progress(56, "正在核对生产聚类指标的修正前后差异")
            audit_rows = build_audit(original_rows, corrected_rows, events, intervals, wells)
            corrected_wells = {row["well_key"] for row in audit_rows if row["changed"]}
            summary.update({"corrected_well_count": len(corrected_wells), "corrected_value_count": sum(row["changed"] for row in audit_rows), "audit_count": len(audit_rows)})
            target = workspace_path / "生产动态校正" / "生产动态_校正.mdb"
            progress(64, "正在复制原始 MDB；原文件保持只读")
            driver = write_corrected_mdb(source_path, target, corrected_rows, audit_rows, progress)
            progress(91, "正在写入工区索引并切换生产聚类数据源")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with database.connect() as conn:
                cursor = conn.execute("""INSERT INTO production_correction_runs(source_id,source_path,corrected_mdb_path,created_at,completed_at,status,well_count,record_count,corrected_well_count,corrected_value_count,inferred_days_count,summary_json)
                    VALUES(?,?,?,?,?,'complete',?,?,?,?,?,?)""", (source_id, str(source_path), str(target), now, now, summary["well_count"], summary["record_count"], summary["corrected_well_count"], summary["corrected_value_count"], summary["inferred_days_count"], json.dumps({**summary, "driver": driver}, ensure_ascii=False)))
                run_id = int(cursor.lastrowid)
                corrected_insert = """INSERT INTO production_corrected_monthly(run_id,source_id,well_key,well_name,production_month,days_on,liquid_rate,oil_rate,water_rate,gas_rate,water_cut,monthly_oil,monthly_water,monthly_gas,cumulative_oil,cumulative_water,cumulative_gas,pressure,pressure_type,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
                for offset in range(0, len(corrected_rows), 5000):
                    conn.executemany(corrected_insert, [
                        (run_id, row["source_id"], row["well_key"], row["well_name"], row["production_month"], row["days_on"], row["liquid_rate"], row["oil_rate"], row["water_rate"], row["gas_rate"], row["water_cut"], row["monthly_oil"], row["monthly_water"], row["monthly_gas"], row["cumulative_oil"], row["cumulative_water"], row["cumulative_gas"], row["pressure"], row["pressure_type"], row["status"], row["metadata_json"])
                        for row in corrected_rows[offset:offset + 5000]
                    ])
                conn.executemany("""INSERT INTO production_correction_audit(run_id,well_key,well_name,field_key,field_label,unit,original_value,corrected_value,changed,reason) VALUES(?,?,?,?,?,?,?,?,?,?)""", [(run_id, row["well_key"], row["well_name"], row["field_key"], row["field_label"], row["unit"], row["original_value"], row["corrected_value"], row["changed"], row["reason"]) for row in audit_rows])
                # One workspace has one active corrected MDB.  Remove older
                # internal snapshots only after the replacement snapshot and
                # its audit have been written in the same transaction.
                conn.execute("DELETE FROM production_correction_runs WHERE id<>?", (run_id,))
            self._update(job_id, status="complete", progress=100, message="校正完成；已自动挂载到生产聚类", run_id=run_id, result={**summary, "corrected_mdb_path": str(target)})
        except Exception as exc:
            self._update(job_id, status="failed", progress=100, message="校正失败；原始 MDB 与当前聚类数据源均未改变", error=str(exc))
