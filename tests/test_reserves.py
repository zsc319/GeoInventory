from __future__ import annotations

import pytest

from app import create_app
from geo_inventory.db import Database
from geo_inventory.project_scan import save_snapshot
from geo_inventory.reserves import calculate, list_groups, read_grid, save_group


def _write_zmap(path, values):
    path.write_text(
        "\n".join([
            "! unit test grid",
            "@GRID FILE, GRID, 5",
            "15, -999.25, , 7, 1",
            "3, 3, 0, 2, 0, 2",
            "0, 0, 0",
            "@",
            " ".join(str(value) for value in values),
        ]),
        encoding="utf-8",
    )


def test_zmap_grid_and_volumetric_constants(tmp_path):
    surface = tmp_path / "structure.zmap"
    _write_zmap(surface, [1000] * 9)
    grid = read_grid(surface)
    assert grid.values.shape == (3, 3)
    assert grid.x_min == 0
    assert grid.y_max == 2

    database = Database(tmp_path / "inventory.sqlite")
    with database.connect() as conn:
        surface_id = conn.execute(
            "INSERT INTO reserve_surfaces(surface_name,file_path,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?)",
            (surface.name, str(surface), "{}", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        ).lastrowid
        result = calculate(conn, {"project": {"root": str(tmp_path)}}, {
            "configuration": {
                "structure_surface_id": f"reserve:{surface_id}",
                "parameters": {
                    "netpay": {"mode": "constant", "value": 10},
                    "geofactor": {"mode": "constant", "value": 100, "unit_mode": "percent"},
                    "phie": {"mode": "constant", "value": 20, "unit_mode": "percent"},
                    "so": {"mode": "constant", "value": 50, "unit_mode": "percent"},
                    "bo": {"mode": "constant", "value": 1},
                    "rs": {"mode": "constant", "value": 2},
                    "rec": {"mode": "constant", "value": 25, "unit_mode": "percent"},
                },
            }
        })

    # The 3x3 node quadrature integrates to the exact 2 m x 2 m map area.
    assert result["summary"]["area_km2"] == pytest.approx(4e-6)
    assert result["summary"]["stoiip_1e4_m3"] == pytest.approx(4 / 1e4)
    assert result["summary"]["recoverable_1e4_m3"] == pytest.approx(1 / 1e4)
    assert result["summary"]["solution_gas_1e8_m3"] == pytest.approx(8 / 1e8)
    assert result["layers"]["stoiip_density"]["rows"] == 3


def test_reserve_parameter_groups_are_workspace_objects(tmp_path):
    database = Database(tmp_path / "inventory.sqlite")
    with database.connect() as conn:
        saved = save_group(conn, {
            "name": "KSF 低方案",
            "configuration": {"structure_surface_id": "reserve:1", "parameters": {}},
            "result_summary": {"stoiip_1e4_m3": 123.4},
        })
    with database.connect() as conn:
        groups = list_groups(conn)
    assert groups[0]["id"] == saved["id"]
    assert groups[0]["name"] == "KSF 低方案"
    assert groups[0]["result_summary"]["stoiip_1e4_m3"] == pytest.approx(123.4)


def test_reserve_workbench_api_lists_workspace_groups(tmp_path):
    snapshot_path = tmp_path / "project_snapshot.json"
    save_snapshot({
        "project": {"root": str(tmp_path), "name": "Test", "scanned_at": "2026-01-01T00:00:00", "total_files": 0, "total_bytes": 0},
        "categories": [], "representatives": [],
    }, snapshot_path)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "api.sqlite"), "PROJECT_SNAPSHOT": str(snapshot_path)})
    with app.extensions["geo_database"].connect() as conn:
        save_group(conn, {"name": "方案 A", "configuration": {}, "result_summary": {}})
    response = app.test_client().get("/api/reserves")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["groups"][0]["name"] == "方案 A"
    assert "phie" in payload["parameters"]
