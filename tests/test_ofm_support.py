from __future__ import annotations

import io
import zipfile

from app import create_app
from geo_inventory.db import Database
from geo_inventory.importers import detect_type
from geo_inventory.inventory_insights import predict_file_type
from geo_inventory.ofm_mdb import column_explanation, table_explanation


def test_mdb_is_a_first_class_production_source(tmp_path):
    path = tmp_path / "ACE_OFM.mdb"
    path.touch()

    assert detect_type(path) == "production"
    prediction = predict_file_type(path)
    assert prediction["type_key"] == "ofm_mdb"
    assert prediction["importable"] is True


def test_database_initializes_ofm_catalog_and_mapping_tables(tmp_path):
    database = Database(tmp_path / "inventory.sqlite")
    with database.connect() as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"ofm_sources", "ofm_table_catalog", "ofm_wells", "ofm_injection_monthly"} <= names


def test_empty_production_exports_keep_stable_contract(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "inventory.sqlite")})
    client = app.test_client()

    payload = client.get("/api/production").get_json()
    assert payload["ofm_sources"] == []
    assert payload["ofm_tables"] == []

    response = client.get("/api/production/export-package.zip")
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    assert {
        "00_先读我_数据可用性.csv",
        "01_井位坐标.csv",
        "02_射孔层段.csv",
        "03_井基本动态汇总.csv",
        "04_各井生产历史.csv",
        "05_各井注入历史.csv",
        "06_OFM关键字列表.csv",
        "README_导出说明.txt",
    } <= set(archive.namelist())
    readme = archive.read("README_导出说明.txt").decode("utf-8-sig")
    assert "不是导出程序丢失数据" in readme


def test_ofm_help_distinguishes_dca_parameters_from_production_history():
    help_row = table_explanation("OFM_DATA_DCA_Analytical", 140)

    assert "递减曲线分析" in help_row["purpose"]
    assert "不是逐月实测生产记录" in help_row["note"]
    assert "140" in help_row["availability"]
    assert "关联编号" in column_explanation("OFM_DATA_DCA_Analytical", "DCA_ID")


def test_complete_package_can_be_written_to_a_selected_folder(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "inventory.sqlite")})
    client = app.test_client()
    export_folder = tmp_path / "exports"
    export_folder.mkdir()

    response = client.post("/api/production/export-to-folder", json={"folder": str(export_folder)})

    assert response.status_code == 200
    payload = response.get_json()
    target = export_folder / payload["path"].split("\\")[-1]
    assert target.is_file()
    with zipfile.ZipFile(target) as archive:
        assert "00_先读我_数据可用性.csv" in archive.namelist()
