import os
from pathlib import Path

import pytest

import app as app_module
from app import create_app

from geo_inventory.db import Database
from geo_inventory.curve_analysis import build_curve_profile, curve_profile_is_current
from geo_inventory.importers import ImportService
from geo_inventory.inventory_insights import overview_composition
from geo_inventory.project_catalog import (
    assign_items_to_group,
    catalog_payload,
    compare_project_trajectories,
    create_group,
    create_well_group,
    delete_well_group,
    list_well_groups,
    project_identity_suggestions,
    petrel_well_top_names,
    project_wells,
    replace_well_group_members,
    save_well_alias,
    sync_imported_sources_to_catalog,
    sync_project_catalog,
    update_well_group,
)
from geo_inventory.project_scan import save_snapshot


DEV_TEMPLATE = """# WELL TRACE FROM PETREL
# WELL NAME: {name}
# WELL HEAD X-COORDINATE: {x} (m)
# WELL HEAD Y-COORDINATE: {y} (m)
      MD            X            Y            Z           TVD
 0.0 {x} {y} 0.0 0.0
 100.0 {x2} {y2} -99.0 99.0
"""


def make_snapshot(root: Path) -> dict:
    files = list(root.rglob("*"))
    files = [path for path in files if path.is_file()]
    return {
        "project": {"root": str(root), "total_files": len(files), "scanned_at": "2026-09-01T00:00:00Z"},
        "representatives": [{"path": str(files[0])}],
        "wellheads": {
            "path": str(root / "wellhead"), "crs": "EPSG:32614",
            "wells": [
                {"name": "ALPHA-01", "uwi": "ALPHA-01", "x": 500000.0, "y": 2400000.0, "kb": 10.0, "td_md": 100.0},
                {"name": "LPH-01", "uwi": "LPH-01", "x": 500000.0, "y": 2400000.0, "kb": 10.0, "td_md": 100.0},
            ],
        },
        "representative_analysis": {"las_samples": [], "dev_samples": [], "curve_sample_coverage": []},
    }


def test_catalog_groups_project_wells_and_trajectory_comparison(tmp_path):
    root = tmp_path / "project"
    (root / "welllogs").mkdir(parents=True)
    (root / "wellpath").mkdir()
    (root / "Sísmica 2D_SINOPEC").mkdir()
    (root / "welllogs" / "ALPHA-01.las").write_text("~Version\n", encoding="utf-8")
    (root / "Sísmica 2D_SINOPEC" / "LINE-01.sgy").write_bytes(b"x")
    for name, x2 in (("ALPHA-01", 500010.0), ("LPH-01", 500010.05)):
        (root / "wellpath" / f"{name}.dev").write_text(
            DEV_TEMPLATE.format(name=name, x=500000.0, y=2400000.0, x2=x2, y2=2400005.0), encoding="utf-8"
        )
    snapshot = make_snapshot(root)
    database = Database(tmp_path / "catalog.sqlite")
    stats = sync_project_catalog(database, snapshot)
    assert stats["items"] == 4

    with database.connect() as conn:
        catalog = catalog_payload(conn, str(root.resolve()), category="seismic_2d")
        assert catalog["total"] == 1
        fuzzy = catalog_payload(conn, str(root.resolve()), query="LPH01")
        assert {row["filename"] for row in fuzzy["items"]} >= {"ALPHA-01.las", "LPH-01.dev"}
        assert all(row["match_score"] >= .72 for row in fuzzy["items"])
        group = create_group(conn, str(root.resolve()), "seismic_2d", "优选线")
        assert assign_items_to_group(conn, str(root.resolve()), group["id"], [catalog["items"][0]["id"]]) == 1
        grouped = catalog_payload(conn, str(root.resolve()), category="seismic_2d", group_id=group["id"])
        assert grouped["items"][0]["group_name"] == "优选线"

        wells = project_wells(conn, snapshot)
        assert {row["canonical_name"] for row in wells} >= {"ALPHA-01", "LPH-01"}
        suggestions = project_identity_suggestions(conn, snapshot)
        match = next(row for row in suggestions if {row["left"]["canonical_name"], row["right"]["canonical_name"]} == {"ALPHA-01", "LPH-01"})
        assert match["can_compare_trajectory"] is True
        comparison = compare_project_trajectories(conn, snapshot, match["left"]["project_key"], match["right"]["project_key"])
        assert comparison["max_3d_distance"] < 0.1

        save_well_alias(conn, snapshot, "LPH01", "ALPHA01", "ALPHA-01")
        merged = project_wells(conn, snapshot)
        assert len([row for row in merged if row["project_key"] == "ALPHA01"]) == 1


def test_overview_uses_well_and_file_denominators_separately(tmp_path):
    root = tmp_path / "project"
    (root / "welltops").mkdir(parents=True)
    (root / "checkshots").mkdir()
    top = root / "welltops" / "Welltops WT2023"
    top.write_text(
        "# Petrel well tops\nVERSION 2\nBEGIN HEADER\nX\nY\nMD\nSurface\nWell\nEND HEADER\n"
        "1 2 100 \"TOP-A\" \"ALPHA-01\"\n1 2 200 \"TOP-B\" \"BETA-02\"\n",
        encoding="utf-8",
    )
    (root / "welltops" / "Welltops WT2023.crsmeta.xml").write_text("<meta/>", encoding="utf-8")
    (root / "checkshots" / "ALPHA-01_logs.las").write_text("~Version\n", encoding="utf-8")
    (root / "checkshots" / "ALPHA-01_TZ_3D.las").write_text("~Version\n", encoding="utf-8")
    snapshot = make_snapshot(root)
    snapshot["representatives"] = [{"path": str(top)}]
    snapshot["categories"] = [
        {"key": "well_heads", "files": 1}, {"key": "checkshots", "files": 2},
        {"key": "well_tops", "files": 2},
    ]
    database = Database(tmp_path / "scope.sqlite")
    sync_project_catalog(database, snapshot)
    assert set(petrel_well_top_names(top)) == {"ALPHA-01", "BETA-02"}

    with database.connect() as conn:
        wells = project_wells(conn, snapshot)
        result = overview_composition(conn, snapshot, wells, {"active": False})
    rows = {row["data_type"]: row for row in result["well_source_coverage"]["rows"]}
    assert result["well_source_coverage"]["denominator"] == 3
    assert rows["well_heads"]["well_count"] == 2
    assert rows["well_heads"]["missing_wells"] == 1
    assert rows["checkshots"]["well_count"] == 1
    assert rows["checkshots"]["extra_file_records"] == 1
    assert rows["well_tops"]["scope"] == "representative"
    assert rows["well_tops"]["primary_files"] == 1
    assert rows["well_tops"]["supporting_files"] == 1


def test_imported_external_las_is_projected_into_catalog_and_export_profile(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.txt").write_text("project", encoding="utf-8")
    external = tmp_path / "new-delivery" / "WELL-001.las"
    external.parent.mkdir()
    external.write_bytes((Path(__file__).resolve().parents[1] / "samples" / "WELL-001.las").read_bytes())
    snapshot = make_snapshot(root)
    database = Database(tmp_path / "imports.sqlite")
    sync_project_catalog(database, snapshot)
    with database.connect() as conn:
        stale_profile = build_curve_profile(conn, snapshot, tmp_path / "curve_profile.json")
    result = ImportService(database).import_path(external, data_type="auto")

    with database.connect() as conn:
        assert curve_profile_is_current(conn, snapshot, stale_profile) is False
        synced = sync_imported_sources_to_catalog(conn, root, [result.source_id])
        catalog = catalog_payload(conn, str(root.resolve()), category="well_logs", query="WELL-001")
        profile = build_curve_profile(conn, snapshot, tmp_path / "curve_profile.json")
        assert curve_profile_is_current(conn, snapshot, profile) is True

    assert synced == {"sources": 1, "items": 1, "missing": 0}
    assert catalog["total"] == 1
    assert catalog["items"][0]["file_path"] == str(external.resolve())
    assert catalog["items"][0]["relative_path"].startswith("导入资料")
    assert profile["las_files"] == 1
    assert profile["wells"][0]["well_name"] == "WELL-001"
    assert {row["mnemonic"] for row in profile["wells"][0]["curves"]} >= {"GR", "RHOB", "NPHI"}


def test_custom_well_groups_keep_an_explicit_delivery_scope(tmp_path):
    root = tmp_path / "project"
    (root / "welllogs").mkdir(parents=True)
    (root / "welllogs" / "ALPHA-01.las").write_text("~Version\n", encoding="utf-8")
    snapshot = make_snapshot(root)
    database = Database(tmp_path / "well-groups.sqlite")
    sync_project_catalog(database, snapshot)
    root_key = str(root.resolve())

    with database.connect() as conn:
        wells = project_wells(conn, snapshot)
        valid_keys = {row["project_key"] for row in wells}
        group = create_well_group(conn, root_key, "北部重点井", "首次交付范围")
        assert replace_well_group_members(conn, root_key, group["id"], list(valid_keys), valid_keys) == len(valid_keys)
        listed = list_well_groups(conn, root_key)
        assert listed[0]["member_count"] == len(valid_keys)
        assert set(listed[0]["well_keys"]) == valid_keys
        updated = update_well_group(conn, root_key, group["id"], "北部重点井 v2", "复核后范围")
        assert updated["name"] == "北部重点井 v2"
        delete_well_group(conn, root_key, group["id"])
        assert list_well_groups(conn, root_key) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows Explorer integration")
def test_catalog_reveal_can_open_folder_or_select_file(tmp_path, monkeypatch):
    root = tmp_path / "project"
    folder = root / "welllogs"
    folder.mkdir(parents=True)
    source = folder / "ALPHA-01.las"
    source.write_text("~Version\n", encoding="utf-8")
    snapshot = make_snapshot(root)
    snapshot_path = tmp_path / "project_snapshot.json"
    save_snapshot(snapshot, snapshot_path)

    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "reveal.sqlite"),
        "PROJECT_SNAPSHOT": str(snapshot_path),
    })
    client = app.test_client()
    item = client.get("/api/catalog").get_json()["items"][0]
    launches = []
    def fake_reveal(target, mode):
        launches.append((target, mode))
        return {"launcher": "Windows Shell", "shell_status": 42, "folder": str(target.parent), "selected": mode == "select"}
    monkeypatch.setattr(app_module, "reveal_in_windows_explorer", fake_reveal)

    opened = client.post(f"/api/catalog/items/{item['id']}/reveal", json={"mode": "folder"})
    assert opened.status_code == 200
    assert opened.get_json()["action"] == "folder"
    assert launches[-1] == (source.resolve(), "folder")
    assert opened.get_json()["launcher"] == "Windows Shell"

    selected = client.post(f"/api/catalog/items/{item['id']}/reveal")
    assert selected.status_code == 200
    assert selected.get_json()["action"] == "select"
    assert launches[-1] == (source.resolve(), "select")
    assert selected.get_json()["selected"] is True

    source.unlink()
    offline = client.post(f"/api/catalog/items/{item['id']}/reveal", json={"mode": "folder"})
    assert offline.status_code == 404
    assert "不可访问" in offline.get_json()["error"]
