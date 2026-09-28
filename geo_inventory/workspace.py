from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database


WORKSPACE_FORMAT_VERSION = 6
APP_VERSION = "0.7.0"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkspaceManager:
    """Manage an analysis workspace stored in a sibling ``*.nvt`` directory."""

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)
        self.history_path = self.state_path.with_name("workspace_history.json")

    def active_path(self) -> Path | None:
        if not self.state_path.is_file():
            return None
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8")).get("workspace_path")
            path = Path(value).resolve() if value else None
            return path if path and path.is_dir() and path.suffix.lower() == ".nvt" else None
        except (OSError, ValueError, TypeError):
            return None

    def describe(self, workspace_path: str | Path | None) -> dict[str, Any]:
        if not workspace_path:
            return {"active": False, "format_version": WORKSPACE_FORMAT_VERSION, "app_version": APP_VERSION}
        path = Path(workspace_path).resolve()
        manifest = self._read_manifest(path)
        return {
            "active": True,
            "path": str(path),
            "name": manifest.get("name") or path.stem,
            "project_title": manifest.get("project_title") or self._default_project_title(manifest.get("source_root"), manifest.get("name") or path.stem),
            "source_root": manifest.get("source_root"),
            "format_version": manifest.get("format_version", 1),
            "app_version": manifest.get("app_version"),
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "last_migration": manifest.get("last_migration"),
            "project_summary": manifest.get("project_summary") or {},
            "database_path": str(path / "inventory.sqlite"),
            "snapshot_path": str(path / "project_snapshot.json"),
        }

    def history(self) -> list[dict[str, Any]]:
        active = self.active_path()
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8")) if self.history_path.is_file() else {"workspaces": []}
        except (OSError, ValueError, TypeError):
            payload = {"workspaces": []}
        entries = payload.get("workspaces") if isinstance(payload, dict) else []
        result = []
        seen: set[str] = set()
        for entry in entries if isinstance(entries, list) else []:
            path_text = str((entry or {}).get("path") or "")
            if not path_text:
                continue
            path = Path(path_text).resolve()
            normalized = str(path).lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            exists = path.is_dir() and path.suffix.lower() == ".nvt"
            try:
                info = self.describe(path) if exists else {
                    "active": False, "path": str(path), "name": path.stem,
                    "project_title": path.stem, "format_version": None, "app_version": None,
                }
            except (OSError, ValueError, TypeError):
                info = {"active": False, "path": str(path), "name": path.stem, "project_title": path.stem}
            result.append({
                **info, "exists": exists, "is_active": bool(active and path == active),
                "last_opened_at": entry.get("last_opened_at"), "open_count": int(entry.get("open_count") or 0),
            })
        if active and str(active).lower() not in seen:
            result.insert(0, {**self.describe(active), "exists": True, "is_active": True, "last_opened_at": None, "open_count": 1})
        return sorted(result, key=lambda row: (bool(row.get("is_active")), row.get("last_opened_at") or row.get("updated_at") or ""), reverse=True)

    def create_or_save(
        self,
        source_root: str | Path,
        source_database: str | Path,
        source_snapshot: str | Path,
        target_path: str | Path | None = None,
        source_process: str | Path | None = None,
    ) -> dict[str, Any]:
        root = Path(source_root).resolve()
        target = Path(target_path).resolve() if target_path else root.with_name(root.name + ".nvt")
        if target.suffix.lower() != ".nvt":
            target = target.with_name(target.name + ".nvt")
        target.mkdir(parents=True, exist_ok=True)
        (target / "process").mkdir(exist_ok=True)
        (target / "uploads").mkdir(exist_ok=True)

        source_db = Path(source_database).resolve()
        target_db = (target / "inventory.sqlite").resolve()
        if source_db != target_db:
            self._sqlite_copy(source_db, target_db)
        else:
            with Database(target_db).connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

        source_snapshot_path = Path(source_snapshot).resolve()
        target_snapshot = (target / "project_snapshot.json").resolve()
        if source_snapshot_path.is_file() and source_snapshot_path != target_snapshot:
            shutil.copy2(source_snapshot_path, target_snapshot)
        elif not target_snapshot.is_file():
            target_snapshot.write_text("{}", encoding="utf-8")

        if source_process:
            source_process_path = Path(source_process).resolve()
            target_process = (target / "process").resolve()
            if source_process_path.is_dir() and source_process_path != target_process:
                shutil.copytree(source_process_path, target_process, dirs_exist_ok=True)
                # Reserve ZMAP files are workspace process assets rather than
                # external source data.  After the process directory is copied,
                # repoint the copied database so reopening the .nvt does not
                # depend on the previous temporary workspace location.
                with Database(target_db).connect() as conn:
                    for row in conn.execute("SELECT surface_id,file_path FROM reserve_surfaces"):
                        try:
                            relative = Path(row["file_path"]).resolve().relative_to(source_process_path)
                        except (OSError, ValueError):
                            continue
                        relocated = target_process / relative
                        if relocated.is_file():
                            conn.execute("UPDATE reserve_surfaces SET file_path=?,updated_at=? WHERE surface_id=?", (str(relocated), utcnow(), row["surface_id"]))

        old = self._read_manifest(target)
        now = utcnow()
        manifest = {
            **old,
            "format": "GeoInventory workspace",
            "format_version": WORKSPACE_FORMAT_VERSION,
            "app_version": APP_VERSION,
            "name": target.stem,
            "project_title": old.get("project_title") or self._default_project_title(root, target.stem),
            "source_root": str(root),
            "created_at": old.get("created_at") or now,
            "updated_at": now,
            "database": "inventory.sqlite",
            "snapshot": "project_snapshot.json",
            "process_directory": "process",
            "project_summary": self._snapshot_summary(target_snapshot),
        }
        self._write_json(target / "workspace.json", manifest)
        process_path = target / "process" / "state.json"
        if not process_path.exists():
            self._write_json(process_path, {"created_at": now, "jobs": [], "curve_profile": None, "horizon_profiles": {}})
        self._set_active(target)
        return {**self.describe(target), "created": not bool(old), "migrated": False}

    def open(self, workspace_path: str | Path) -> dict[str, Any]:
        path = Path(workspace_path).resolve()
        if path.suffix.lower() != ".nvt" or not path.is_dir():
            raise ValueError("请选择一个以 .nvt 结尾的工区文件夹")
        manifest_path = path / "workspace.json"
        if not manifest_path.is_file():
            raise ValueError("该目录缺少 workspace.json，不是有效的 GeoInventory 工区")
        manifest = self._read_manifest(path)
        old_version = int(manifest.get("format_version") or 1)
        migrated = old_version < WORKSPACE_FORMAT_VERSION or manifest.get("app_version") != APP_VERSION
        manifest_changed = False
        if not manifest.get("project_title"):
            manifest["project_title"] = self._default_project_title(manifest.get("source_root"), manifest.get("name") or path.stem)
            manifest_changed = True
        # The project hub needs just these few scalars.  Persisting them in
        # the manifest avoids reopening a potentially large directory snapshot
        # for every historical project card.
        if not manifest.get("project_summary"):
            manifest["project_summary"] = self._snapshot_summary(path / "project_snapshot.json")
            manifest_changed = True
        # Database.initialize contains idempotent schema migrations. The user
        # requested in-place upgrades, so no automatic backup is created here.
        Database(path / "inventory.sqlite").initialize()
        if migrated:
            now = utcnow()
            manifest.update({
                "format_version": WORKSPACE_FORMAT_VERSION,
                "app_version": APP_VERSION,
                "updated_at": now,
                "last_migration": {"from": old_version, "to": WORKSPACE_FORMAT_VERSION, "at": now, "backup_created": False},
            })
            manifest_changed = True
        if manifest_changed:
            self._write_json(manifest_path, manifest)
        self._set_active(path)
        return {**self.describe(path), "created": False, "migrated": migrated, "previous_version": old_version}

    def _set_active(self, path: Path) -> None:
        now = utcnow()
        self._write_json(self.state_path, {"workspace_path": str(path), "updated_at": now})
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8")) if self.history_path.is_file() else {"workspaces": []}
        except (OSError, ValueError, TypeError):
            payload = {"workspaces": []}
        entries = payload.get("workspaces") if isinstance(payload, dict) else []
        entries = entries if isinstance(entries, list) else []
        normalized = str(path.resolve()).lower()
        previous = next((row for row in entries if str(row.get("path") or "").lower() == normalized), {})
        updated = {
            "path": str(path.resolve()), "last_opened_at": now,
            "open_count": int(previous.get("open_count") or 0) + 1,
        }
        remaining = [row for row in entries if str(row.get("path") or "").lower() != normalized]
        self._write_json(self.history_path, {"workspaces": [updated, *remaining][:30], "updated_at": now})

    @staticmethod
    def _default_project_title(source_root: str | Path | None, fallback: str) -> str:
        text = str(source_root or fallback)
        name = Path(text).name or fallback
        name = name.removesuffix(".nvt")
        return name if name.endswith("资料清查") else f"{name}资料清查"

    @staticmethod
    def _snapshot_summary(snapshot_path: Path) -> dict[str, Any]:
        """Return only hub-card fields, never the catalog file list itself."""
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.is_file() else {}
        except (OSError, ValueError, TypeError):
            snapshot = {}
        project = snapshot.get("project") if isinstance(snapshot, dict) else {}
        categories = snapshot.get("categories") if isinstance(snapshot, dict) else []
        project = project if isinstance(project, dict) else {}
        categories = categories if isinstance(categories, list) else []
        return {
            "source_name": project.get("name"),
            "scanned_at": project.get("scanned_at"),
            "total_files": int(project.get("total_files") or 0),
            "total_bytes": int(project.get("total_bytes") or 0),
            "total_gb": project.get("total_gb"),
            "representative_count": int(project.get("representative_count") or 0),
            "category_count": sum(1 for item in categories if isinstance(item, dict) and item.get("files")),
        }

    @staticmethod
    def _sqlite_copy(source: Path, target: Path) -> None:
        if not source.is_file():
            Database(target).initialize()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(source)
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        Database(target).initialize()

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        manifest_path = path / "workspace.json"
        if not manifest_path.is_file():
            return {}
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
