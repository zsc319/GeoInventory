from __future__ import annotations

from pathlib import Path

from geo_inventory.db import Database
from geo_inventory.directory_translation import translate_filename, translate_folder, translation_page
from geo_inventory.project_catalog import catalog_payload, sync_project_catalog


def test_directory_translation_only_records_display_aliases(tmp_path: Path):
    source = tmp_path / "Produccion" / "Registros"
    source.mkdir(parents=True)
    original = source / "Produccion_Aceite_2020.xlsx"
    original.write_bytes(b"keep-source-bytes")
    well_file = source / "EBN-19.dev"
    well_file.write_text("well data", encoding="utf-8")
    database = Database(tmp_path / "inventory.sqlite")

    with database.connect() as conn:
        result = translate_folder(conn, str(tmp_path), source)
        listing = translation_page(conn, str(tmp_path), source)

    sync_project_catalog(database, {"project": {"root": str(tmp_path)}, "representatives": []})
    with database.connect() as conn:
        catalog = catalog_payload(conn, str(tmp_path.resolve()), query="生产")

    assert result["total_files"] == 2
    assert result["translated_files"] == 1
    assert original.is_file()
    assert original.read_bytes() == b"keep-source-bytes"
    aliases = {item["original_name"]: item["display_name"] for item in listing["items"]}
    assert aliases["Produccion_Aceite_2020.xlsx"] == "生产_原油_2020.xlsx"
    assert aliases["EBN-19.dev"] == "EBN-19.dev"
    assert translate_filename("Fallas_Seccion_A.xlsx") == "断层_剖面_A.xlsx"
    assert translate_filename("SONICO DE CEMENTACION.las") == "固井声波.las"
    assert translate_filename("Base_de_Usuario") == "用户数据库"
    assert translate_filename("Reporte_CMI_") == "CMI报告"
    assert catalog["total"] == 1
    assert catalog["items"][0]["display_name"] == "生产_原油_2020.xlsx"
