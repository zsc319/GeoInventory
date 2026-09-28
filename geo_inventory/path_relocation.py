"""Repoint absolute source references after a project drive or folder moves."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .workspace import utcnow


def _prefix(value: str) -> str:
    value = str(value or "").strip().strip('"').replace("/", "\\")
    if not value or not (re.match(r"^[A-Za-z]:\\", value) or value.startswith("\\\\")):
        raise ValueError("请输入完整的 Windows 绝对路径前缀，例如 E:\\ 或 F:\\项目")
    return value.rstrip("\\") + ("\\" if re.fullmatch(r"[A-Za-z]:\\?", value) else "")


def _repoint(value: str, old: str, new: str) -> str:
    normalized = value.replace("/", "\\")
    base = old.rstrip("\\")
    if normalized.casefold() == base.casefold():
        return new.rstrip("\\")
    if normalized[: len(base)].casefold() != base.casefold():
        return value
    tail = normalized[len(base) :]
    if not tail.startswith("\\"):
        return value
    return new.rstrip("\\") + tail


def _repoint_json(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return _repoint(value, old, new)
    if isinstance(value, list):
        return [_repoint_json(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _repoint_json(item, old, new) for key, item in value.items()}
    return value


def _json_files(workspace: Path) -> list[Path]:
    files = [workspace / "workspace.json", workspace / "project_snapshot.json"]
    process = workspace / "process"
    if process.is_dir():
        files.extend(sorted(process.rglob("*.json")))
    return [path for path in files if path.is_file()]


def _db_changes(conn: sqlite3.Connection, old: str, new: str) -> list[tuple[str, str, int, str]]:
    changes: list[tuple[str, str, int, str]] = []
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", table):
            continue
        for info in conn.execute(f'PRAGMA table_info("{table}")'):
            column = info[1]
            is_path = column == "project_root" or column.endswith("_path")
            is_json = column.endswith("_json")
            if not (is_path or is_json):
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", column):
                continue
            # The drive token also appears before JSON's escaped backslashes.
            needle = old[:2] if re.match(r"^[A-Za-z]:", old) or old.startswith("\\\\") else old
            for rowid, value in conn.execute(
                f'SELECT rowid,"{column}" FROM "{table}" WHERE instr(lower("{column}"),lower(?))>0',
                (needle,),
            ):
                if not isinstance(value, str):
                    continue
                if is_json:
                    try:
                        parsed = json.loads(value)
                    except (TypeError, ValueError):
                        continue
                    updated = _repoint_json(parsed, old, new)
                    if updated == parsed:
                        continue
                    replacement = json.dumps(updated, ensure_ascii=False)
                else:
                    replacement = _repoint(value, old, new)
                if replacement != value:
                    changes.append((table, column, rowid, replacement))
    return changes


def prepare_relocation(workspace_path: str | Path, old_prefix: str, new_prefix: str) -> dict[str, Any]:
    workspace = Path(workspace_path).resolve()
    if workspace.suffix.lower() != ".nvt" or not (workspace / "workspace.json").is_file():
        raise ValueError("请先打开有效的 .nvt 工区")
    old, new = _prefix(old_prefix), _prefix(new_prefix)
    if old.casefold() == new.casefold():
        raise ValueError("新旧路径相同，请填写变化后的路径")
    manifest = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    source_root = manifest.get("source_root") or ""
    target_root = _repoint(source_root, old, new)
    if target_root == source_root:
        raise ValueError("旧路径前缀未包含工区记录的原始资料目录")
    if not Path(target_root).is_dir():
        raise ValueError(f"新原始资料目录不存在：{target_root}")
    files = []
    for path in _json_files(workspace):
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        updated = _repoint_json(parsed, old, new)
        if updated != parsed:
            files.append((path, raw, json.dumps(updated, ensure_ascii=False, indent=2)))
    database = workspace / "inventory.sqlite"
    if not database.is_file():
        raise ValueError("工区数据库不存在，无法安全更改路径")
    with closing(sqlite3.connect(database)) as conn:
        changes = _db_changes(conn, old, new)
        candidates = [row[0] for row in conn.execute(
            "SELECT file_path FROM project_catalog_items WHERE project_root=? LIMIT 40", (source_root,)
        )]
        if not candidates:
            candidates = [row[0] for row in conn.execute("SELECT file_path FROM sources WHERE file_path IS NOT NULL LIMIT 40")]
    mapped = [_repoint(path, old, new) for path in candidates if _repoint(path, old, new) != path]
    found = sum(Path(path).is_file() for path in mapped)
    if mapped and found == 0:
        raise ValueError("新目录已找到，但抽查的资料文件均不存在；请核对新路径前缀及目录层级")
    if not files and not changes:
        raise ValueError("工区中没有匹配旧前缀的路径")
    return {
        "workspace": workspace, "old_prefix": old, "new_prefix": new,
        "source_root": source_root, "target_root": target_root,
        "files": files, "db_changes": changes,
        "file_count": len(files), "database_count": len(changes),
        "sample_checked": len(mapped), "sample_found": found,
    }


def relocate_paths(workspace_path: str | Path, old_prefix: str, new_prefix: str, *, apply: bool = False) -> dict[str, Any]:
    plan = prepare_relocation(workspace_path, old_prefix, new_prefix)
    result = {key: plan[key] for key in ("old_prefix", "new_prefix", "source_root", "target_root", "file_count", "database_count", "sample_checked", "sample_found")}
    result["applied"] = False
    if not apply:
        return result

    database = plan["workspace"] / "inventory.sqlite"
    staged = []
    replaced = []
    try:
        for path, original, updated in plan["files"]:
            temporary = path.with_name(path.name + ".relocate.tmp")
            temporary.write_text(updated, encoding="utf-8")
            staged.append((path, temporary, original))
        with closing(sqlite3.connect(database, timeout=30)) as conn:
            with conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("PRAGMA defer_foreign_keys=ON")
                for table, column, rowid, replacement in plan["db_changes"]:
                    conn.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (replacement, rowid))
                for path, temporary, original in staged:
                    temporary.replace(path)
                    replaced.append((path, original))
    except Exception:
        for path, original in reversed(replaced):
            path.write_text(original, encoding="utf-8")
        raise
    finally:
        for _, temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    result["applied"] = True
    result["updated_at"] = utcnow()
    return result
