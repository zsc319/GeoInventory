from pathlib import Path

from geo_inventory.db import Database
from geo_inventory.project_catalog import classify_file, sync_project_catalog
from geo_inventory.time_depth import build_time_depth_inventory, is_time_depth_file, parse_petrel_checkshot


def _snapshot(root: Path) -> dict:
    return {
        "project": {"root": str(root), "total_files": 4},
        "representatives": [],
        "wellheads": {"wells": []},
    }


def test_time_depth_detection_covers_independent_files_without_seismic_false_positives(tmp_path):
    root = tmp_path / "project"
    checkshots = root / "Checkshots"
    tdr = root / "TDR"
    seismic = root / "Seismic processing"
    checkshots.mkdir(parents=True)
    tdr.mkdir()
    seismic.mkdir()
    checkshot = checkshots / "ALPHA-01_TZ.las"
    standalone = tdr / "BRAVO-02_TDR.csv"
    seismic_twt = seismic / "LINE-100_TWT.segy"
    sidecar = checkshots / "ALPHA-01_TZ.las.xml"
    for path in (checkshot, standalone, seismic_twt, sidecar):
        path.write_text("test", encoding="utf-8")

    assert is_time_depth_file(root, checkshot)
    assert is_time_depth_file(root, standalone)
    assert not is_time_depth_file(root, seismic_twt)
    assert not is_time_depth_file(root, sidecar)
    assert classify_file(root, standalone) == "checkshots"
    assert classify_file(root, seismic_twt) == "seismic_3d"


def test_petrel_checkshot_is_parsed_and_searchable_by_well(tmp_path):
    root = tmp_path / "project"
    folder = root / "Checkshots"
    folder.mkdir(parents=True)
    path = folder / "ALPHA-01_TZ.las"
    path.write_text(
        "# Petrel checkshots format\nVERSION 1\nBEGIN HEADER\nX\nY\nZ\nTWT picked\nMD\nWell\n"
        "Average velocity\nInterval velocity\nEND HEADER\n"
        '1 2 -100 150.5 100 "ALPHA-01" 1500 1600\n'
        '1 2 -200 290.5 200 "ALPHA-01" 1550 1650\n',
        encoding="utf-8",
    )
    parsed = parse_petrel_checkshot(path)
    assert parsed["wells"] == ["ALPHA-01"]
    assert parsed["point_count"] == 2
    assert parsed["md_min"] == 100
    assert parsed["time_max"] == 290.5

    database = Database(tmp_path / "inventory.sqlite")
    sync_project_catalog(database, _snapshot(root))
    with database.connect() as conn:
        result = build_time_depth_inventory(conn, str(root), {"wells": []}, query="ALPHA01")
    assert result["match_count"] == 1
    assert result["files"][0]["well_name"] == "ALPHA-01"
    assert result["files"][0]["catalog_id"]
    assert result["summary"]["independent_files"] == 1


def test_las_time_curve_is_added_to_well_search(tmp_path):
    root = tmp_path / "project"
    logs = root / "Well logs"
    logs.mkdir(parents=True)
    path = logs / "CHARLIE-03.las"
    path.write_text("~Version\n", encoding="utf-8")
    database = Database(tmp_path / "inventory.sqlite")
    sync_project_catalog(database, _snapshot(root))
    profile = {
        "wells": [{
            "well_key": "CHARLIE03", "well_name": "CHARLIE-03", "filename": path.name,
            "file_path": str(path), "start": 100.0, "stop": 2000.0,
            "curves": [{"mnemonic": "ONE-WAYTIME1"}],
        }]
    }
    with database.connect() as conn:
        result = build_time_depth_inventory(conn, str(root), profile, query="CHARLIE-03")
    assert result["match_count"] == 1
    assert result["files"][0]["source_kind"] == "las_curve"
    assert "owt" in result["files"][0]["evidence"]
