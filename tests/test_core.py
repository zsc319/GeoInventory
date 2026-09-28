from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import pytest

from app import create_app
from geo_inventory import production_clustering as clustering_module
from geo_inventory.db import Database
from geo_inventory.geometry import point_in_polygon, polygon_area
from geo_inventory.curve_analysis import is_time_depth_mnemonic
from geo_inventory.importers import ImportService, parse_las, parse_las_curve_samples, parse_las_header, scan_segy
from geo_inventory.project_scan import parse_petrel_surface, parse_petrel_well_head, save_snapshot
from geo_inventory.production_clustering import build_feature_dataset
from geo_inventory.production_correction import build_audit, rebuild_monthly
from geo_inventory.seismic_inventory import build_inventory, canonical_volume_name, quick_file_metadata
from geo_inventory.trajectory import minimum_curvature
from geo_inventory.well_identity import classify_well_identity


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def test_software_identity_is_embedded(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "identity.sqlite")})
    payload = app.test_client().get("/api/software-identity").get_json()
    assert payload["author"] == "Zhu Sicheng"
    assert payload["author_zh"] == "朱思成"
    assert payload["email"] == "zhusc.syky@sinopec.com"
    assert payload["repository"] == "https://github.com/zsc319/GeoInventory"
    assert "SINOPEC" in payload["trademark"]


def test_minimum_curvature_vertical_and_tvdss():
    rows = minimum_curvature([
        {"md": 0, "inclination": 0, "azimuth": 0},
        {"md": 1000, "inclination": 0, "azimuth": 0},
    ], kb_elevation=100)
    assert rows[-1]["tvd"] == pytest.approx(1000)
    assert rows[-1]["northing"] == pytest.approx(0)
    assert rows[-1]["tvdss"] == pytest.approx(900)
    assert rows[-1]["z_msl"] == pytest.approx(-900)


def test_production_correction_rebuilds_ebano_style_missing_cumulative():
    rows = [
        {"id": 1, "source_id": 7, "well_key": "EBANO2", "well_name": "Ebano-2", "production_month": "1927-04-01T00:00:00", "days_on": None, "oil_rate": 269.01371, "monthly_oil": None, "cumulative_oil": None, "metadata_json": '{"raw":{"GAS":538.02742}}'},
        {"id": 2, "source_id": 7, "well_key": "EBANO2", "well_name": "Ebano-2", "production_month": "1927-05-01T00:00:00", "days_on": None, "oil_rate": 5646.74173, "monthly_oil": None, "cumulative_oil": 0, "metadata_json": '{"raw":{"GAS":11293.48346}}'},
        {"id": 3, "source_id": 7, "well_key": "EBANO2", "well_name": "Ebano-2", "production_month": "1927-06-01T00:00:00", "days_on": None, "oil_rate": 4585.76811, "monthly_oil": None, "cumulative_oil": 0, "metadata_json": '{"raw":{"GAS":9171.53622}}'},
    ]

    corrected, summary = rebuild_monthly(rows)

    assert [row["days_on"] for row in corrected] == [30, 31, 30]
    assert corrected[-1]["cumulative_oil"] == pytest.approx(269.01371 * 30 + 5646.74173 * 31 + 4585.76811 * 30)
    assert corrected[0]["gas_rate"] == pytest.approx(538.02742)
    assert summary["inferred_days_count"] == 3
    audit = build_audit(rows, corrected, [], [], [])
    by_field = {row["field_key"]: row for row in audit}
    assert by_field["cumulative_oil"]["changed"] == 1
    assert by_field["uptime_ratio"]["changed"] == 1
    assert by_field["initial_gor"]["corrected_value"] == pytest.approx(2.0)


def test_production_overview_and_clustering_mount_latest_completed_correction(tmp_path):
    db_path = tmp_path / "correction.sqlite"
    corrected_mdb = tmp_path / "生产动态_校正.mdb"
    corrected_mdb.touch()
    database = Database(db_path)
    with database.connect() as conn:
        source_id = conn.execute(
            "INSERT INTO sources(filename,file_path,data_type,imported_at,status) VALUES(?,?,?,?,?)",
            ("original.mdb", str(tmp_path / "original.mdb"), "production", "2026-01-01T00:00:00", "ready"),
        ).lastrowid
        run_id = conn.execute(
            """INSERT INTO production_correction_runs(source_id,source_path,corrected_mdb_path,created_at,completed_at,status,well_count,record_count,corrected_well_count,corrected_value_count,inferred_days_count)
               VALUES(?,?,?,?,?,'complete',1,1,1,2,1)""",
            (source_id, str(tmp_path / "original.mdb"), str(corrected_mdb), "2026-01-01T00:00:00", "2026-01-01T00:01:00"),
        ).lastrowid
        conn.execute(
            """INSERT INTO production_corrected_monthly(run_id,source_id,well_key,well_name,production_month,days_on,oil_rate,monthly_oil,cumulative_oil,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (run_id, source_id, "EBANO2", "Ebano-2", "1927-04-01T00:00:00", 30, 269.01371, 8070.4113, 8070.4113, "{}"),
        )

    client = create_app({"TESTING": True, "DATABASE": str(db_path)}).test_client()
    overview = client.get("/api/production").get_json()

    assert overview["production_source"]["mode"] == "corrected"
    assert overview["well_summaries"][0]["cumulative_oil"] == pytest.approx(8070.4113)
    clustering = client.get("/api/production-clustering").get_json()
    assert clustering["data_source"]["mode"] == "corrected"
    assert clustering["data_source"]["selected"] is True
    assert clustering["data_source"]["verified"] is True
    assert clustering["data_source"]["filename"] == "生产动态_校正.mdb"
    assert clustering["data_source"]["path"] == str(corrected_mdb)
    assert clustering["data_source"]["run_id"] == run_id
    assert clustering["data_source"]["record_count"] == 1
    assert clustering["data_source"]["scope"] == "01 数据体检至 09 二维展示"
    assert clustering["wells"][0]["values"]["cumulative_oil"] == pytest.approx(8.0704113)


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
    assert summary["cumulative_liquid_kbbl"] == pytest.approx(3.275)
    assert summary["production_days"] == 30
    assert summary["average_oil_rate"] == pytest.approx(2300 / 30)
    assert summary["average_water_rate"] == pytest.approx(975 / 30)
    assert summary["average_liquid_rate"] == pytest.approx(3275 / 30)
    assert summary["latest_liquid_rate"] == 80
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
    assert inventory["performance"]["feature_cache"] == "rebuilt"
    cached_inventory = client.get("/api/production-clustering").get_json()
    assert cached_inventory["performance"]["feature_cache"] == "hit"
    assert inventory["readiness"]["training_candidates"] == 4
    assert any(row["key"] == "initial_gor" and not row["available"] for row in inventory["indicators"])
    assert [row["key"] for row in inventory["composition_schemes"]] == ["initial_3m_rate", "late_3m_rate", "cumulative"]
    assert inventory["wells"][0]["values"]["initial_3m_water_rate"] is not None
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
    assert som["performance"]["engine"] in {"NumPy 向量化计算", "Python 兼容计算"}
    assert som["performance"]["feature_cache"] == "hit"

    background = client.post("/api/production-clustering/training-jobs", json={
        "model": "kmeans", "cluster_count": 2, "max_iterations": 10,
        "indicators": selected, "training_wells": well_keys, "evaluation_wells": well_keys,
    })
    assert background.status_code == 202
    job = background.get_json()
    for _ in range(100):
        job = client.get(f"/api/production-clustering/training-jobs/{job['id']}").get_json()
        if job["status"] in {"complete", "failed"}:
            break
        time.sleep(0.02)
    assert job["status"] == "complete"
    assert job["percent"] == 100
    assert job["result"]["ready"] is True

    save_response = client.post("/api/production-clustering/save", json={
        "plan_id": "plan-test", "plan_name": "测试方案",
        "configuration": {
            "model": "som",
            "plan_state": {
                "training_wells": well_keys, "evaluation_wells": well_keys,
                "indicators": selected, "model": "som", "segmentation": "split",
                "modelSettings": {"som": {"cluster_count": 2}},
                "architectures": {"som": [{"type": "som", "nodes": 2}]},
            },
        },
        "result": som,
    })
    assert save_response.status_code == 200
    saved = save_response.get_json()
    assert saved["saved"] is True
    assert set(saved["files"]) == {"井产能分类结果.csv", "类别指标统计.csv", "训练摘要.json", "训练配置.json", "完整训练结果.json"}
    assert saved["plan"]["id"] == "plan-test"

    group_response = client.post("/api/production-clustering/well-groups", json={
        "id": "group-test", "name": "测试独立井组",
        "well_keys": [well_keys[0], well_keys[1], well_keys[0]],
    })
    assert group_response.status_code == 200
    assert group_response.get_json()["group"]["well_keys"] == well_keys[:2]
    rename_response = client.patch("/api/production-clustering/well-groups/group-test", json={
        "name": "重命名后的独立井组",
    })
    assert rename_response.status_code == 200
    assert rename_response.get_json()["group"]["name"] == "重命名后的独立井组"
    assert rename_response.get_json()["group"]["well_keys"] == well_keys[:2]

    # A fresh app instance represents closing and reopening the same .nvt.
    reopened = create_app({"TESTING": True, "DATABASE": str(db_path)}).test_client()
    restored = reopened.get("/api/production-clustering/state").get_json()
    assert restored["active_plan_id"] == "plan-test"
    assert len(restored["plans"]) == 1
    assert restored["plans"][0]["name"] == "测试方案"
    assert restored["plans"][0]["configuration"]["plan_state"]["model"] == "som"
    assert restored["plans"][0]["result"]["ready"] is True
    compact = reopened.get("/api/production-clustering/state?compact=1").get_json()
    assert compact["plans"][0]["has_result"] is True
    assert compact["plans"][0]["result"] == {}
    assert compact["plans"][0]["result_deferred"] is True
    assert compact["plans"][0]["result_summary"]["cluster_count"] == 2
    detail = reopened.get("/api/production-clustering/state/plans/plan-test").get_json()
    assert detail["plan"]["result"]["ready"] is True
    assert detail["plan"]["configuration"]["plan_state"]["model"] == "som"
    assert restored["well_groups"] == [{
        "id": "group-test", "name": "重命名后的独立井组", "well_keys": well_keys[:2],
        "created_at": restored["well_groups"][0]["created_at"],
        "updated_at": restored["well_groups"][0]["updated_at"],
    }]
    listed_groups = reopened.get("/api/production-clustering/well-groups").get_json()["well_groups"]
    assert listed_groups[0]["id"] == "group-test"
    assert listed_groups[0]["well_keys"] == well_keys[:2]


def test_production_clustering_indexes_legacy_saved_scheme(tmp_path):
    db_path = tmp_path / "inventory.sqlite"
    scheme = tmp_path / "生产聚类" / "旧方案"
    scheme.mkdir(parents=True)
    (scheme / "训练配置.json").write_text(json.dumps({
        "plan_name": "旧方案", "saved_at": "2026-09-15T08:48:06+08:00",
        "model": "som", "cluster_count": 2,
        "indicators": ["initial_3m_oil_rate"],
        "training_wells": ["WELL1"], "evaluation_wells": ["WELL1"],
    }, ensure_ascii=False), encoding="utf-8")
    (scheme / "训练摘要.json").write_text(json.dumps({
        "model": "SOM", "model_type": "som", "selected_indicators": ["initial_3m_oil_rate"],
        "cluster_count": 2, "training_count": 1, "training_sample_count": 1,
        "evaluation_count": 1, "silhouette": 0.5,
        "category_order": [
            {"category": "产能类型 1", "cluster": 1, "rank": 1, "level": "相对较好"},
            {"category": "产能类型 2", "cluster": 0, "rank": 2, "level": "相对偏弱"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (scheme / "井产能分类结果.csv").write_text(
        "井名,范围,产能类别,相对级次,开发层位,X,Y,initial_3m_oil_rate\n"
        "Well-1,训练井,产能类型 1,1,KAN,100,200,88.5\n",
        encoding="utf-8-sig",
    )
    (scheme / "类别指标统计.csv").write_text(
        "类别,相对级次,样本数,指标,单位,最小值,Q1,中值,Q3,最大值\n"
        "产能类型 1,1,1,初期3个月平均日产油,bbl/d,88.5,88.5,88.5,88.5,88.5\n",
        encoding="utf-8-sig",
    )

    client = create_app({"TESTING": True, "DATABASE": str(db_path)}).test_client()
    first = client.get("/api/production-clustering/state").get_json()
    assert first["migrated_plan_count"] == 1
    assert first["active_plan_id"] == "legacy-001"
    assert first["plans"][0]["name"] == "旧方案"
    assert first["plans"][0]["result"]["ready"] is True
    assert first["plans"][0]["result"]["results"][0]["well_key"] == "WELL1"
    assert first["plans"][0]["result"]["results"][0]["metrics"][0]["value"] == 88.5

    # The folder is indexed once; later module opens read the database record.
    second = client.get("/api/production-clustering/state").get_json()
    assert second["migrated_plan_count"] == 0
    assert len(second["plans"]) == 1


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
    assert well["segment_samples"][1]["values"]["initial_3m_water_rate"] == 20


def test_production_composition_uses_additive_windows_and_gas_equivalent():
    monthly = []
    cumulative_oil = cumulative_water = cumulative_gas = 0
    for month, gas_rate in enumerate((6000, 12000, 18000), 1):
        cumulative_oil += 100 * 30
        cumulative_water += 50 * 30
        cumulative_gas += gas_rate * 30
        monthly.append({
            "id": month, "well_key": "P1", "well_name": "P-1",
            "production_month": f"2026-{month:02d}", "days_on": 30,
            "oil_rate": 100, "water_rate": 50, "gas_rate": gas_rate,
            "monthly_oil": 3000, "monthly_water": 1500, "monthly_gas": gas_rate * 30,
            "cumulative_oil": cumulative_oil, "cumulative_water": cumulative_water,
            "cumulative_gas": cumulative_gas,
        })
    dataset = build_feature_dataset(monthly, [], [], [{"well_key": "P1", "well_name": "P-1"}])
    values = dataset["wells"][0]["values"]
    assert values["initial_3m_oil_rate"] == 100
    assert values["initial_3m_water_rate"] == 50
    assert values["initial_3m_gas_boe_rate"] == pytest.approx(2)
    assert values["cumulative_oil"] == pytest.approx(9)
    assert values["cumulative_water"] == pytest.approx(4.5)
    assert values["cumulative_gas_kboe"] == pytest.approx(0.18)
    component_keys = {
        component["metric_key"]
        for scheme in dataset["composition_schemes"]
        for component in scheme["components"]
    }
    assert "late_water_cut" not in component_keys
    assert "water_cut_growth" not in component_keys
    assert "oil_decline_pct" not in component_keys


def test_vectorized_som_preserves_online_update_results(monkeypatch):
    if clustering_module.np is None:
        pytest.skip("NumPy acceleration is unavailable")
    matrix = [[-1.2, 0.3, 0.8], [-0.7, 0.1, 0.5], [0.4, -0.2, -0.1], [1.1, -0.5, -0.8]]
    accelerated = clustering_module._som(matrix, 2, 12, 0.4, 1.2, "line")
    monkeypatch.setattr(clustering_module, "np", None)
    fallback = clustering_module._som(matrix, 2, 12, 0.4, 1.2, "line")
    assert accelerated[0] == fallback[0]
    for accelerated_row, fallback_row in zip(accelerated[1], fallback[1]):
        assert accelerated_row == pytest.approx(fallback_row, abs=1e-12)
    assert accelerated[2] == fallback[2]


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


def test_minimal_well_head_excel_enters_catalog_and_clustering_map(tmp_path):
    from openpyxl import Workbook

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "README.txt").write_text("project", encoding="utf-8")
    excel_path = tmp_path / "new-delivery" / "well-heads.xlsx"
    excel_path.parent.mkdir()
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Index", "Well Name", "surface X", "surface Y", "base X", "base Y"])
    sheet.append([1, "EB-2035DES", 574057, 2470705, 573930.56, 2470727])
    sheet.append([2, "EB-2079DES", 574667, 2471541, 574411.36, 2471563])
    workbook.save(excel_path)

    snapshot_path = tmp_path / "project_snapshot.json"
    save_snapshot({
        "project": {"root": str(project_root), "total_files": 1, "scanned_at": "2026-09-17T00:00:00Z"},
        "representatives": [],
        "wellheads": {"wells": [], "crs": "EPSG:32614"},
        "representative_analysis": {"las_samples": [], "dev_samples": []},
    }, snapshot_path)
    db_path = tmp_path / "well-head-excel.sqlite"
    client = create_app({
        "TESTING": True,
        "DATABASE": str(db_path),
        "PROJECT_SNAPSHOT": str(snapshot_path),
    }).test_client()
    imported = client.post("/api/import-path", json={
        "paths": [str(excel_path)], "data_type": "auto", "crs": "EPSG:32614",
    })
    assert imported.status_code == 200
    assert imported.get_json()["results"][0]["data_type"] == "well_head"
    assert imported.get_json()["results"][0]["records"] == 2

    database = Database(db_path)
    with database.connect() as conn:
        wells = [dict(row) for row in conn.execute("SELECT canonical_name,x,y,crs FROM wells ORDER BY canonical_name")]
        source = conn.execute("SELECT attributes_json FROM well_sources ORDER BY id LIMIT 1").fetchone()
    assert wells == [
        {"canonical_name": "EB-2035DES", "x": 574057.0, "y": 2470705.0, "crs": "EPSG:32614"},
        {"canonical_name": "EB-2079DES", "x": 574667.0, "y": 2471541.0, "crs": "EPSG:32614"},
    ]
    assert json.loads(source["attributes_json"])["base X"] == 573930.56
    catalog = client.get("/api/catalog?category=well_heads").get_json()
    assert catalog["total"] == 1
    assert catalog["items"][0]["filename"] == "well-heads.xlsx"

    clustering = client.get("/api/production-clustering").get_json()
    assert clustering["wells"] == []
    assert clustering["readiness"]["map_well_count"] == 2
    assert clustering["readiness"]["coordinate_wells"] == 2
    assert {(row["well_name"], row["x"], row["y"], row["has_production"]) for row in clustering["map_wells"]} == {
        ("EB-2035DES", 574057.0, 2470705.0, False),
        ("EB-2079DES", 574667.0, 2471541.0, False),
    }


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
