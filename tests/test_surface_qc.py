from __future__ import annotations

import numpy as np
import pytest

from app import create_app
from geo_inventory.db import Database
from geo_inventory.project_scan import save_snapshot
from geo_inventory.reserves import Grid
from geo_inventory.surface_qc import compare_surfaces, surface_quality, well_surface_samples


def _grid(values, name="test"):
    return Grid(name, name, np.asarray(values, dtype=float), 0, 3, 0, 3, True, None, "test")


def _save_surface(conn, path):
    return f"reserve:{conn.execute('INSERT INTO reserve_surfaces(surface_name,file_path,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?)', (path.name, str(path), '{}', '2026-01-01', '2026-01-01')).lastrowid}"


def _write_zmap(path, values):
    path.write_text("\n".join([
        "@GRID FILE, GRID, 5", "15, -999.25, , 7, 1", "4, 4, 0, 3, 0, 3",
        "0, 0, 0", "@", " ".join(str(value) for value in values),
    ]), encoding="utf-8")


def test_surface_quality_and_well_sampling_handle_missing_edges():
    grid = _grid([[1, 2, 3, 4], [5, np.nan, 7, 8], [9, 10, 11, 12], [13, 14, 15, 100]])
    quality = surface_quality(grid)
    assert quality["valid_cells"] == 15
    assert quality["missing_percent"] == pytest.approx(6.25)
    assert quality["iqr_outliers"] == 1
    assert len(quality["missing_map"]) == 4

    wells = [
        {"project_key": "a", "canonical_name": "A", "x": 0, "y": 0},
        {"project_key": "b", "canonical_name": "B", "x": 1, "y": 1},
        {"project_key": "c", "canonical_name": "C", "x": 4, "y": 2},
        {"project_key": "d", "canonical_name": "D", "x": None, "y": None},
    ]
    result = well_surface_samples(grid, wells)
    assert result["counts"] == {"ok": 1, "no_coordinates": 1, "outside": 1, "missing": 1}
    assert result["rows"][0]["value"] == 1
    assert result["rows"][0]["distance"] == 0
    assert result["rows"][1]["status"] == "missing"

    north_first = Grid("north", "north", grid.values, 0, 3, 0, 3, False, None, "test")
    assert surface_quality(north_first)["missing_map"][1][1] == 1


def test_compare_surfaces_aligns_by_xy_and_excludes_missing(tmp_path):
    first = tmp_path / "first.zmap"
    second = tmp_path / "second.zmap"
    values = np.arange(1, 17, dtype=float)
    _write_zmap(first, values)
    _write_zmap(second, 2 * values + 1)
    database = Database(tmp_path / "qc.sqlite")
    with database.connect() as conn:
        ids = [_save_surface(conn, path) for path in (first, second)]
        result = compare_surfaces(conn, {"project": {"root": str(tmp_path)}}, ids)
        assert result["common_valid_cells"] == 16
        assert result["correlation"][0][1] == pytest.approx(1)
        assert result["strong_pairs"][0]["first"] == "first"
        assert result["pca"]["explained_variance_percent"][0] == pytest.approx(100)
        with pytest.raises(ValueError, match="重复"):
            compare_surfaces(conn, {"project": {"root": str(tmp_path)}}, [ids[0], ids[0]])

    distant = tmp_path / "distant.zmap"
    distant.write_text("\n".join([
        "@GRID FILE, GRID, 5", "15, -999.25, , 7, 1", "4, 4, 100, 103, 100, 103",
        "0, 0, 0", "@", " ".join(str(value) for value in values),
    ]), encoding="utf-8")
    with database.connect() as conn:
        distant_id = _save_surface(conn, distant)
        with pytest.raises(ValueError, match="共同有效网格点"):
            compare_surfaces(conn, {"project": {"root": str(tmp_path)}}, [ids[0], distant_id])


def test_surface_quality_api_returns_results_and_validates_request(tmp_path):
    snapshot_path = tmp_path / "project_snapshot.json"
    save_snapshot({
        "project": {"root": str(tmp_path), "name": "Test", "scanned_at": "2026-01-01", "total_files": 0, "total_bytes": 0},
        "categories": [], "representatives": [],
    }, snapshot_path)
    surface = tmp_path / "surface.zmap"
    _write_zmap(surface, np.arange(1, 17))
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "api.sqlite"), "PROJECT_SNAPSHOT": str(snapshot_path)})
    with app.extensions["geo_database"].connect() as conn:
        surface_id = _save_surface(conn, surface)
        second = tmp_path / "second.zmap"
        _write_zmap(second, np.arange(1, 17) * 2)
        second_id = _save_surface(conn, second)
    client = app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert b"surface-qc-run" in page.data
    assert client.get("/static/surface-qc.js").status_code == 200
    response = client.post("/api/reserves/surface-quality", json={"surface_id": surface_id})
    assert response.status_code == 200
    assert response.get_json()["quality"]["valid_cells"] == 16
    assert response.get_json()["wells"]["total_wells"] == 0
    comparison = client.post("/api/reserves/surface-compare", json={"surface_ids": [surface_id, second_id]})
    assert comparison.status_code == 200
    assert comparison.get_json()["correlation"][0][1] == pytest.approx(1)
    assert client.post("/api/reserves/surface-compare", json={"surface_ids": [surface_id]}).status_code == 400
