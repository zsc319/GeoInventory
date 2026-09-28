from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import create_app
from geo_inventory.db import Database
from geo_inventory.path_relocation import relocate_paths


def make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "Moved.nvt"
    workspace.mkdir()
    (workspace / "process").mkdir()
    target = tmp_path / "NewData"
    target.mkdir()
    (target / "source.las").write_text("LAS", encoding="utf-8")
    (workspace / "workspace.json").write_text(json.dumps({
        "format_version": 5, "name": "Moved", "source_root": "E:\\OldData",
    }), encoding="utf-8")
    (workspace / "project_snapshot.json").write_text(json.dumps({
        "project": {"root": "E:\\OldData", "total_files": 1},
        "representatives": [{"path": "E:\\OldData\\source.las"}],
        "unrelated": "E:\\OldData2\\keep.las",
    }), encoding="utf-8")
    (workspace / "process" / "curve_profile.json").write_text(json.dumps({
        "source": "E:\\OldData\\source.las",
        "external": "E:\\OtherProject\\keep.las",
    }), encoding="utf-8")
    database = workspace / "inventory.sqlite"
    Database(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO sources(filename,file_path,data_type,imported_at,status) VALUES(?,?,?,?,?)",
                     ("source.las", "E:\\OldData\\source.las", "las", "2026-01-01", "ready"))
        conn.execute("""INSERT INTO project_catalog_items
            (project_root,file_path,relative_path,filename,extension,category_key,source_folder)
            VALUES(?,?,?,?,?,?,?)""",
            ("E:\\OldData", "E:\\OldData\\source.las", "source.las", "source.las", ".las", "well_logs", "root"))
        conn.execute("INSERT INTO project_catalog_groups(project_root,category_key,name,created_at) VALUES(?,?,?,?)",
                     ("E:\\OldData", "well_logs", "组", "2026-01-01"))
        conn.execute("INSERT INTO workspace_settings(setting_key,value_json,updated_at) VALUES(?,?,?)",
                     ("paths", json.dumps({"files": ["E:\\OldData\\source.las", "E:\\OtherProject\\keep.las"]}), "2026-01-01"))
    return workspace, target


def test_workspace_path_relocation_updates_references_and_preserves_other_paths(tmp_path):
    workspace, target = make_workspace(tmp_path)
    preview = relocate_paths(workspace, "E:\\OldData", str(target))
    assert preview["applied"] is False
    assert preview["sample_found"] == 1
    assert preview["database_count"] >= 4
    assert json.loads((workspace / "workspace.json").read_text())["source_root"] == "E:\\OldData"

    result = relocate_paths(workspace, "E:\\OldData", str(target), apply=True)
    assert result["applied"] is True
    assert json.loads((workspace / "workspace.json").read_text())["source_root"] == str(target)
    snapshot = json.loads((workspace / "project_snapshot.json").read_text())
    assert snapshot["project"]["root"] == str(target)
    assert snapshot["representatives"][0]["path"] == str(target / "source.las")
    assert snapshot["unrelated"] == "E:\\OldData2\\keep.las"
    process = json.loads((workspace / "process" / "curve_profile.json").read_text())
    assert process["source"] == str(target / "source.las")
    assert process["external"] == "E:\\OtherProject\\keep.las"
    with sqlite3.connect(workspace / "inventory.sqlite") as conn:
        assert conn.execute("SELECT file_path FROM sources").fetchone()[0] == str(target / "source.las")
        assert conn.execute("SELECT project_root FROM project_catalog_groups").fetchone()[0] == str(target)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        settings = json.loads(conn.execute("SELECT value_json FROM workspace_settings").fetchone()[0])
        assert settings["files"] == [str(target / "source.las"), "E:\\OtherProject\\keep.las"]


def test_workspace_path_relocation_rejects_wrong_directory_without_writing(tmp_path):
    workspace, target = make_workspace(tmp_path)
    empty = tmp_path / "EmptyData"
    empty.mkdir()
    with pytest.raises(ValueError, match="抽查"):
        relocate_paths(workspace, "E:\\OldData", str(empty), apply=True)
    with pytest.raises(ValueError, match="不存在"):
        relocate_paths(workspace, "E:\\OldData", str(target / "missing"), apply=True)
    assert json.loads((workspace / "workspace.json").read_text())["source_root"] == "E:\\OldData"


def test_workspace_path_relocation_rolls_back_on_catalog_collision(tmp_path):
    workspace, target = make_workspace(tmp_path)
    with sqlite3.connect(workspace / "inventory.sqlite") as conn:
        conn.execute("""INSERT INTO project_catalog_items
            (project_root,file_path,relative_path,filename,extension,category_key,source_folder)
            VALUES(?,?,?,?,?,?,?)""",
            (str(target), str(target / "source.las"), "source.las", "source.las", ".las", "well_logs", "root"))
    with pytest.raises(sqlite3.IntegrityError):
        relocate_paths(workspace, "E:\\OldData", str(target), apply=True)
    assert json.loads((workspace / "workspace.json").read_text())["source_root"] == "E:\\OldData"
    with sqlite3.connect(workspace / "inventory.sqlite") as conn:
        assert conn.execute("SELECT file_path FROM sources").fetchone()[0] == "E:\\OldData\\source.las"


def test_workspace_path_relocation_api_and_button(tmp_path):
    workspace, target = make_workspace(tmp_path)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "api.sqlite")})
    client = app.test_client()
    assert client.post("/api/workspace/relocate-paths", json={"old_prefix": "E:\\", "new_prefix": "F:\\"}).status_code == 400
    assert client.post("/api/workspace/open", json={"path": str(workspace)}).status_code == 200
    response = client.post("/api/workspace/relocate-paths", json={
        "old_prefix": "E:\\OldData", "new_prefix": str(target),
    })
    assert response.status_code == 200
    assert response.get_json()["applied"] is False
    page = client.get("/").get_data(as_text=True)
    assert 'id="workspace-change-path"' in page
    assert page.index('id="workspace-change-path"') < page.index('id="workspace-directory-export"')
