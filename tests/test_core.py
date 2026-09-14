from __future__ import annotations

import struct
from pathlib import Path

import pytest

from app import create_app
from geo_inventory.db import Database
from geo_inventory.geometry import point_in_polygon, polygon_area
from geo_inventory.curve_analysis import is_time_depth_mnemonic
from geo_inventory.importers import ImportService, parse_las, parse_las_curve_samples, parse_las_header, scan_segy
from geo_inventory.project_scan import parse_petrel_surface, parse_petrel_well_head
from geo_inventory.production_clustering import build_feature_dataset
from geo_inventory.seismic_inventory import build_inventory, canonical_volume_name, quick_file_metadata
from geo_inventory.trajectory import minimum_curvature
from geo_inventory.well_identity import classify_well_identity


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def test_minimum_curvature_vertical_and_tvdss():
    rows = minimum_curvature([
        {"md": 0, "inclination": 0, "azimuth": 0},
        {"md": 1000, "inclination": 0, "azimuth": 0},
    ], kb_elevation=100)
    assert rows[-1]["tvd"] == pytest.approx(1000)
    assert rows[-1]["northing"] == pytest.approx(0)
    assert rows[-1]["tvdss"] == pytest.approx(900)
    assert rows[-1]["z_msl"] == pytest.approx(-900)


def test_polygon_boundary_is_inside():
    polygon = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]
    assert point_in_polygon(5, 5, polygon)
    assert point_in_polygon(0, 5, polygon)
    assert not point_in_polygon(11, 5, polygon)
    assert polygon_area(polygon) == 100


def test_las_parser():
    parsed = parse_las(SAMPLES / "WELL-001.las")
    assert parsed["well"]["WELL"]["value"] == "WELL-001"
    assert parsed["sample_rows"] == 5
    assert len(parsed["curves"]) == 4
    assert parsed["curves"][3]["sample_count"] == 4


def test_las_header_inventory_skips_values_and_separates_time_depth():
    parsed = parse_las_header(SAMPLES / "WELL-001.las")
    assert parsed["well"]["WELL"]["value"] == "WELL-001"
    assert parsed["sample_rows"] is None
    assert all(curve["sample_count"] is None for curve in parsed["curves"])
    assert is_time_depth_mnemonic("TWT(VELOCITYMODEL-NEW)")
    assert is_time_depth_mnemonic("GENERALTIME1")
    assert not is_time_depth_mnemonic("GR")


def test_las_targeted_curve_stream_only_returns_requested_samples():
    parsed = parse_las_curve_samples(SAMPLES / "WELL-001.las", {"GR"}, sample_limit=128)
    assert [row["mnemonic"] for row in parsed["curves"]] == ["GR"]
    assert len(parsed["curves"][0]["samples"]) == 5
    assert parsed["curves"][0]["samples"][0] == {"md": 1000.0, "value": 65.2}


def test_well_identity_uses_hard_evidence_not_arbitrary_medium_score():
    las_only = classify_well_identity(["las"])
    assert las_only["existence_confirmed"] is True
    assert las_only["identity_status"] == "单源硬证据"
    auxiliary = classify_well_identity(["well_top", "checkshot"])
    assert auxiliary["existence_confirmed"] is False
    assert auxiliary["identity_status"] == "待核实身份"
    conflict = classify_well_identity(["well_head", "deviation"], coordinate_spread=12)
    assert conflict["existence_confirmed"] is True
    assert conflict["identity_status"] == "待核实身份"
    production_only = classify_well_identity(["production"])
    assert production_only["existence_confirmed"] is True
    assert production_only["identity_status"] == "单源硬证据"


def test_production_perforation_import(tmp_path):
    path = tmp_path / "perforations.csv"
    path.write_text(
        "WELL,TOP_MD,BASE_MD,INTERVAL,STATUS,DATE\nWELL-001,1200,1225,SAND-A,OPEN,2026-01-01\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "production.sqlite"
    service = ImportService(Database(db_path))
    result = service.import_path(path, data_type="production")
    assert result.records == 1
    app = create_app({"TESTING": True, "DATABASE": str(db_path)})
    payload = app.test_client().get("/api/production").get_json()
    assert payload["perforation_count"] == 1
    assert payload["perforations"][0]["well_key"] == "WELL001"
    assert payload["perforations"][0]["top_md"] == 1200


def test_ofm_monthly_production_import_and_summary(tmp_path):
    path = tmp_path / "OFM_production_export.csv"
    path.write_text(
        "WELL,DATE,FIRST_PRODUCTION_DATE,DAYS_ON,LIQUID_RATE,OIL_RATE,WATER_CUT,CUM_OIL,CUM_WATER,BHP,STATUS\n"
        "E-100,2026-01,2026-01-15,20,120,90,25,1800,600,25.5,OPEN\n"
        "E-100,2026-02,2026-01-15,10,80,50,37.5,2300,975,22.0,SHUT\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "ofm.sqlite"
    service = ImportService(Database(db_path))
    result = service.import_path(path)
    assert result.data_type == "production"
    assert result.records == 2
    assert result.details["monthly_records"] == 2
    app = create_app({"TESTING": True, "DATABASE": str(db_path)})
    payload = app.test_client().get("/api/production?well=E100").get_json()
    summary = payload["well_summaries"][0]
    assert payload["monthly_record_count"] == 2
    assert payload["dynamic_wells"] == 1
    assert len(payload["series"]) == 2
    assert summary["onstream_date"] == "2026-01-15"
    assert summary["production_months"] == 2
    assert summary["initial_liquid_rate"] == 120
    assert summary["initial_oil_rate"] == 90
    assert summary["initial_water_cut"] == 25
    assert summary["cumulative_oil"] == 2300
    assert summary["cumulative_water"] == 975
    assert summary["pressure_change"] == pytest.approx(-3.5)
    assert summary["hard_evidence"] is True


def test_production_series_is_on_demand_and_supports_decline_fit(tmp_path):
    path = tmp_path / "production.csv"
    path.write_text(
        "WELL,DATE,DAYS_ON,OIL_RATE,WATER_RATE,WATER_CUT,CUM_OIL,CUM_WATER,BHP\n"
        "E-101,2026-01,31,120,10,7.7,3720,310,30\n"
        "E-101,2026-02,28,95,18,15.9,6380,814,28\n"
        "E-101,2026-03,31,76,26,25.5,8736,1620,26\n"
        "E-101,2026-04,30,61,35,36.5,10566,2670,24\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "series.sqlite"
    ImportService(Database(db_path)).import_path(path, data_type="production")
    client = create_app({"TESTING": True, "DATABASE": str(db_path)}).test_client()
    response = client.post("/api/production/series", json={
        "well": "E101", "fields": ["oil_rate", "cumulative_oil", "pressure"],
        "x_axis": "cumulative_oil", "fit_metric": "oil_rate",
    })
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["well_key"] == "E101"
    assert len(payload["series"]) == 4
    assert payload["series"][0]["cumulative_days"] == 31
    assert payload["fit"]["available"] is True
    assert 0 <= payload["fit"]["b"] <= 2


def test_production_clustering_inventory_training_and_grades(tmp_path):
    path = tmp_path / "cluster_production.csv"
    rows = ["WELL,DATE,DAYS_ON,OIL_RATE,WATER_RATE,WATER_CUT,CUM_OIL,CUM_WATER,BHP"]
    profiles = {
        "HIGH-1": [(180, 10), (170, 12), (160, 15), (150, 18)],
        "HIGH-2": [(165, 12), (158, 14), (151, 17), (145, 20)],
        "LOW-1": [(45, 35), (35, 42), (27, 50), (20, 58)],
        "LOW-2": [(38, 40), (31, 46), (24, 54), (17, 62)],
    }
    for well, values in profiles.items():
        cumulative_oil = cumulative_water = 0
        for month, (oil, water) in enumerate(values, 1):
            cumulative_oil += oil * 30
            cumulative_water += water * 30
            water_cut = water / (oil + water) * 100
            rows.append(f"{well},2026-{month:02d},30,{oil},{water},{water_cut},{cumulative_oil},{cumulative_water},{30-month}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    db_path = tmp_path / "cluster.sqlite"
    ImportService(Database(db_path)).import_path(path, data_type="production")
    client = create_app({"TESTING": True, "DATABASE": str(db_path)}).test_client()

    inventory_response = client.get("/api/production-clustering")
    assert inventory_response.status_code == 200
    inventory = inventory_response.get_json()
    assert inventory["readiness"]["well_count"] == 4
    assert inventory["readiness"]["training_candidates"] == 4
    assert any(row["key"] == "initial_gor" and not row["available"] for row in inventory["indicators"])
    well_keys = [row["well_key"] for row in inventory["wells"]]
    selected = [row["key"] for row in inventory["indicators"] if row["selected"]]

    trained_response = client.post("/api/production-clustering/train", json={
        "model": "kmeans", "cluster_count": 2, "indicators": selected,
        "training_wells": well_keys, "evaluation_wells": well_keys,
    })
    assert trained_response.status_code == 200
    trained = trained_response.get_json()
    assert trained["ready"] is True
    assert trained["cluster_count"] == 2
    assert trained["training_sample_count"] == 4
    assert trained["projection"]["method"] == "PCA"
    assert len(trained["results"]) == 4
    assert {row["category"] for row in trained["results"]} == {"产能类型 1", "产能类型 2"}
    assert {row["category_rank"] for row in trained["results"]} == {1, 2}
    assert all(row["metrics"] for row in trained["results"])
    curves = client.post("/api/production-clustering/curves", json={"well_keys": well_keys}).get_json()
    assert len(curves["curves"]) == 4
    assert all(row["series"] for row in curves["curves"])

    som_response = client.post("/api/production-clustering/train", json={
        "model": "som", "cluster_count": 2, "max_iterations": 20,
        "model_parameters": {"learning_rate": 0.4, "radius": 1.5, "topology": "line"},
        "indicators": selected, "training_wells": well_keys, "evaluation_wells": well_keys,
    })
    assert som_response.status_code == 200
    som = som_response.get_json()
    assert som["ready"] is True
    assert som["model_type"] == "som"
    assert "SOM" in som["model"]
    assert len(som["loss_history"]) == 20

    save_response = client.post("/api/production-clustering/save", json={
        "plan_name": "测试方案", "configuration": {"model": "som"}, "result": som,
    })
    assert save_response.status_code == 200
    saved = save_response.get_json()
    assert saved["saved"] is True
    assert set(saved["files"]) == {"井产能分类结果.csv", "类别指标统计.csv", "训练摘要.json", "训练配置.json"}


def test_production_clustering_builds_auditable_pre_post_samples():
    monthly = []
    for month in range(1, 9):
        monthly.append({
            "id": month, "well_key": "M1", "well_name": "M-1",
            "production_month": f"2026-{month:02d}", "days_on": 30,
            "oil_rate": 40 if month < 5 else 90, "water_rate": 20,
            "water_cut": 33.3 if month < 5 else 18.2,
            "monthly_oil": (40 if month < 5 else 90) * 30,
            "monthly_water": 600,
        })
    dataset = build_feature_dataset(
        monthly,
        [{"well_key": "M1", "well_name": "M-1", "event_date": "2026-05", "event_type": "压裂"}],
        [],
        [{"well_key": "M1", "well_name": "M-1", "x": 100, "y": 200}],
    )
    well = dataset["wells"][0]
    assert well["segment_eligible"] is True
    assert [row["segment"] for row in well["segment_samples"]] == ["措施前基线", "措施后响应"]
    assert all(row["record_count"] == 4 for row in well["segment_samples"])
    assert well["segment_samples"][1]["values"]["initial_3m_oil_rate"] == 90


def test_production_clustering_preflight_points_to_missing_dataset_step(tmp_path):
    db_path = tmp_path / "empty_cluster.sqlite"
    client = create_app({"TESTING": True, "DATABASE": str(db_path)}).test_client()
    response = client.post("/api/production-clustering/train", json={
        "model": "kmeans", "cluster_count": 3,
        "indicators": ["initial_3m_oil_rate", "peak_oil_rate"],
        "training_wells": [], "evaluation_wells": [],
    })
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ready"] is False
    assert payload["jump_to"] in {"indicators", "dataset"}


def test_end_to_end_import_and_api(tmp_path):
    db_path = tmp_path / "inventory.sqlite"
    database = Database(db_path)
    service = ImportService(database)
    service.import_path(SAMPLES / "well_head.csv", crs="EPSG:32648")
    service.import_path(SAMPLES / "well_deviation.csv", data_type="deviation", crs="EPSG:32648")
    service.import_path(SAMPLES / "WELL-001.las")
    service.import_path(SAMPLES / "interpretation_v1.csv", data_type="interpretation", version="v1")
    service.import_path(SAMPLES / "block.geojson", crs="EPSG:32648")

    app = create_app({"TESTING": True, "DATABASE": str(db_path)})
    client = app.test_client()
    summary = client.get("/api/summary").get_json()
    assert summary["wells"] == 3
    assert summary["curve_types"] == 3
    assert summary["interpretation_types"] == 4
    wells = client.get("/api/wells?polygon_id=1").get_json()
    assert {row["canonical_name"] for row in wells} == {"WELL-001", "W-002"}
    trajectory = client.get(f"/api/wells/{wells[0]['id']}/trajectory").get_json()
    assert trajectory
    assert trajectory[-1]["tvdss"] is not None


def make_segy(path: Path) -> None:
    text_header = ("C01 SYNTHETIC TEST".ljust(3200)).encode("ascii")
    binary = bytearray(400)
    struct.pack_into(">H", binary, 16, 2000)
    struct.pack_into(">H", binary, 20, 2)
    struct.pack_into(">H", binary, 24, 5)
    data = bytearray(text_header) + binary
    traces = [(100, 200, 10, 20), (200, 200, 11, 20), (100, 300, 10, 21), (200, 300, 11, 21)]
    for x, y, inline, crossline in traces:
        header = bytearray(240)
        struct.pack_into(">h", header, 70, 1)
        struct.pack_into(">ii", header, 180, x, y)
        struct.pack_into(">ii", header, 188, inline, crossline)
        struct.pack_into(">H", header, 114, 2)
        data.extend(header)
        data.extend(struct.pack(">ff", 1.0, 2.0))
    path.write_bytes(data)


def test_segy_scanner(tmp_path):
    path = tmp_path / "synthetic.sgy"
    make_segy(path)
    stats = scan_segy(path)
    assert stats["dimension"] == "3D"
    assert stats["trace_count"] == 4
    assert stats["x_min"] == 100
    assert stats["footprint_area"] == pytest.approx(10000)
    assert stats["delay_time_min_ms"] == 0
    assert stats["trace_end_time_max_ms"] == pytest.approx(2)


def test_seismic_quick_inventory_reads_headers_without_trace_scan(tmp_path):
    path = tmp_path / "Amplitude_PSTM.sgy"
    make_segy(path)
    item = quick_file_metadata(path, tmp_path)
    assert item["format"] == "SEG-Y"
    assert item["trace_count"] == 4
    assert item["trace_count_exact"] is True
    assert item["sample_count_max"] == 2
    assert item["sample_interval_label"] == "2 ms"
    assert item["sample_encoding"] == "IEEE 32-bit 浮点"
    assert item["attribute_type"] == "振幅"
    assert item["domain"] == "时间域"


def test_seismic_inventory_groups_copies_but_preserves_paths(tmp_path):
    first = tmp_path / "north" / "Cube_PSTM.sgy"
    second = tmp_path / "south" / "Cube_PSTM copy.sgy"
    first.parent.mkdir()
    second.parent.mkdir()
    make_segy(first)
    make_segy(second)
    (tmp_path / "Velocity.zgy").write_bytes(b"ZGY placeholder")

    inventory = build_inventory(tmp_path)
    assert inventory["file_count"] == 3
    assert inventory["sgy_count"] == 2
    assert inventory["zgy_count"] == 1
    pstm_group = next(group for group in inventory["groups"] if group["name"] == "Cube PSTM")
    assert pstm_group["path_count"] == 2
    assert {Path(item["relative_path"]).parent.name for item in pstm_group["paths"]} == {"north", "south"}
    assert canonical_volume_name(second) == "Cube PSTM"


def test_petrel_swapped_segy_layout(tmp_path):
    path = tmp_path / "petrel.sgy"
    text = "C01 SURVEY: TEST 3D".ljust(80) + "C02 RANGE OF INLINE: 1008-1009 RANGE OF XLINE: 2001-2002".ljust(80)
    text_header = text.ljust(3200).encode("cp500")
    binary = bytearray(400)
    struct.pack_into(">H", binary, 16, 2000)
    struct.pack_into(">H", binary, 20, 1)
    struct.pack_into(">H", binary, 24, 5)
    data = bytearray(text_header) + binary
    for inline, crossline in [(1008, 2001), (1008, 2002), (1009, 2001), (1009, 2002)]:
        header = bytearray(240)
        struct.pack_into(">h", header, 70, -100)
        struct.pack_into(">ii", header, 180, inline, crossline)
        struct.pack_into(">ii", header, 188, int((500000 + crossline * 15) * 100), int((2400000 + inline * 15) * 100))
        struct.pack_into(">H", header, 114, 1)
        data.extend(header)
        data.extend(struct.pack(">f", 1.0))
    path.write_bytes(data)
    stats = scan_segy(path)
    assert stats["byte_layout"] == "petrel_pep_swapped"
    assert stats["dimension"] == "3D"
    assert stats["grid_transform"]["inline_spacing"] == pytest.approx(15)
    assert stats["grid_transform"]["crossline_spacing"] == pytest.approx(15)


def test_2d_source_coordinates_are_not_grid_numbers(tmp_path):
    path = tmp_path / "line.segy"
    text_header = "C01 ESTUDIO 2D: TEST LINE".ljust(3200).encode("cp500")
    binary = bytearray(400)
    struct.pack_into(">H", binary, 16, 2000)
    struct.pack_into(">H", binary, 20, 1)
    struct.pack_into(">H", binary, 24, 5)
    data = bytearray(text_header) + binary
    for index in range(4):
        x, y = 550000 + index * 15, 2460000
        header = bytearray(240)
        struct.pack_into(">ii", header, 72, x, y)
        struct.pack_into(">ii", header, 188, x, y)
        struct.pack_into(">H", header, 114, 1)
        data.extend(header)
        data.extend(struct.pack(">f", 1.0))
    path.write_bytes(data)
    stats = scan_segy(path)
    assert stats["dimension"] == "2D"
    assert stats["byte_layout"] == "source_xy_2d"
    assert stats["inline_min"] is None


def test_petrel_wellhead_and_surface_headers(tmp_path):
    wellhead = tmp_path / "wellhead"
    headers = ["Name", "UWI", "Well symbol", "Surface X", "Surface Y", "Latitude", "Longitude", "Well datum value", "TD (MD)"]
    wellhead.write_text(
        "# Petrel well head\n# Coordinate reference system X, Y: EPSG:32614\nVERSION 1\nBEGIN HEADER\n"
        + "\n".join(headers)
        + "\nEND HEADER\n\"A-1\" \"UWI-1\" 3 500100 2460100 22°11'32.0\"N 98°27'4.0\"W 12.5 3000\n",
        encoding="utf-8",
    )
    parsed_head = parse_petrel_well_head(wellhead)
    assert parsed_head["count"] == 1
    assert parsed_head["wells"][0]["x"] == 500100

    surface = tmp_path / "surface.ptd"
    surface.write_text(
        "FSASCI 0 1 COMPUTED 0 0.1E+31\nFSLIMI 0 20 0 10 100 200\nFSNROW 2 3\nFSXINC 10 10\n->MSMODL\n1 2 0.1E+31\n4 5 6\n",
        encoding="utf-8",
    )
    parsed_surface = parse_petrel_surface(surface, True)
    assert parsed_surface["total_cells"] == 6
    assert parsed_surface["valid_cells"] == 5
