from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from geo_inventory.curve_analysis import (
    assign_curve_type,
    clear_focus_decision,
    compare_curves,
    curve_statistics,
    curve_type_coverage,
    curve_workbench,
    focus_merge_payload,
    save_focus_decision,
    suggest_curve_type,
)
from geo_inventory.curve_distribution import (
    curve_distribution,
    load_curve_filter,
    save_curve_filter,
)
from geo_inventory.db import Database
from geo_inventory.global_filter import compute_surface_horizon_hits, evaluate_filter, load_filter, save_filter
from geo_inventory.horizon_analysis import (
    assign_horizon_name_aliases, compute_catalog_surface_hits, create_horizon_name_group,
    horizon_detail, horizon_workbench, surface_metadata,
)
from geo_inventory.importers import parse_las
from geo_inventory.inventory_insights import preflight_folder
from geo_inventory.interpretation_core import (
    assign_interpretation_type,
    create_interpretation_type,
    interpretation_workbench,
    parse_core_depth_range,
)
from geo_inventory.jobs import ImportJobManager
from geo_inventory.model_inventory import classify_model_item, model_inventory
from geo_inventory.relationship_search import relationship_search, well_match_score
from geo_inventory.workspace import WORKSPACE_FORMAT_VERSION, WorkspaceManager


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def snapshot_for(root: Path) -> dict:
    return {
        "project": {"root": str(root), "scanned_at": "2026-09-01T00:00:00Z"},
        "representative_analysis": {"las_samples": []},
    }


def test_workspace_save_and_in_place_migration(tmp_path):
    database_path = tmp_path / "source.sqlite"
    Database(database_path).initialize()
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot_for(tmp_path)), encoding="utf-8")
    process_path = tmp_path / "process-source"
    process_path.mkdir()
    (process_path / "curve_profile.json").write_text('{"sample_wells": 1}', encoding="utf-8")
    manager = WorkspaceManager(tmp_path / "active.json")

    created = manager.create_or_save(tmp_path / "source-data", database_path, snapshot_path, source_process=process_path)
    workspace = Path(created["path"])
    assert workspace.suffix == ".nvt"
    assert (workspace / "inventory.sqlite").is_file()
    assert (workspace / "process" / "state.json").is_file()
    assert (workspace / "process" / "curve_profile.json").is_file()
    assert created["project_title"] == "source-data资料清查"
    history = manager.history()
    assert len(history) == 1
    assert history[0]["path"] == str(workspace.resolve())
    assert history[0]["is_active"] is True
    assert history[0]["open_count"] == 1

    manifest_path = workspace / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["format_version"] = 1
    manifest["app_version"] = "0.1.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    opened = manager.open(workspace)
    assert opened["migrated"] is True
    migrated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert migrated["format_version"] == WORKSPACE_FORMAT_VERSION
    assert migrated["last_migration"]["backup_created"] is False
    history = manager.history()
    assert history[0]["open_count"] == 2
    assert history[0]["project_title"] == "source-data资料清查"


def test_workspace_uses_source_directory_for_project_title(tmp_path):
    manager = WorkspaceManager(tmp_path / "active.json")
    source_root = tmp_path / "Project-A" / "Export"
    assert manager._default_project_title(source_root, source_root.name) == "Export资料清查"


def test_curve_typing_comparison_and_manual_unclassified(tmp_path):
    database = Database(tmp_path / "curves.sqlite")
    snapshot = snapshot_for(tmp_path / "project")
    profile = {
        "source": "test",
        "sample_wells": 3,
        "wells": [
            {"well_key": "A", "filename": "A.las", "start": 100, "stop": 500, "step": .1,
             "curves": [{"mnemonic": "GR", "unit": "API", "value_min": 10, "value_max": 120}, {"mnemonic": "RHOB", "unit": "G/C3", "value_min": 2, "value_max": 3}, {"mnemonic": "ONE-WAYTIME62", "unit": "MS"}]},
            {"well_key": "B", "filename": "B.las", "start": 150, "stop": 550, "step": .1,
             "curves": [{"mnemonic": "GR", "unit": "API", "value_min": 12, "value_max": 125}]},
            {"well_key": "C", "filename": "C.las", "start": 200, "stop": 450, "step": .2,
             "curves": [{"mnemonic": "RHOB", "unit": "G/C3", "value_min": 2.1, "value_max": 2.9}]},
        ],
    }
    with database.connect() as conn:
        workbench = curve_workbench(conn, snapshot, profile)
        gr = next(row for row in workbench["curves"] if row["mnemonic"] == "GR")
        assert gr["assignment"]["type_key"] == "gamma_ray"
        assert [row["well_name"] for row in gr["well_details"]] == ["A", "B"]
        assert gr["well_details"][0]["start_md"] == 100
        assert gr["well_details"][0]["value_max"] == 120
        assert "ONE-WAYTIME62" not in {row["mnemonic"] for row in workbench["curves"]}
        assert [row["mnemonic"] for row in workbench["time_depth_curves"]] == ["ONE-WAYTIME62"]
        coverage = curve_type_coverage(workbench)
        gamma = next(row for row in coverage["types"] if row["type_key"] == "gamma_ray")
        assert gamma["well_count"] == 2
        assert gamma["mnemonics"] == ["GR"]
        assign_curve_type(conn, str((tmp_path / "project").resolve()), "GR", None)
        workbench = curve_workbench(conn, snapshot, profile)
        assert next(row for row in workbench["curves"] if row["mnemonic"] == "GR")["assignment"]["type_key"] == "unclassified"
        coverage = curve_type_coverage(workbench)
        assert next(row for row in coverage["types"] if row["type_key"] == "unclassified")["well_count"] == 2

    comparison = compare_curves(curve_statistics(profile), "GR", "RHOB")
    assert comparison["intersection_wells"] == ["A"]
    assert comparison["only_a_wells"] == ["B"]
    assert comparison["only_b_wells"] == ["C"]
    assert comparison["sampling_same"] is False


def test_preflight_recurses_classifies_and_skips_workspace(tmp_path):
    nested = tmp_path / "LAS" / "batch-2"
    nested.mkdir(parents=True)
    (tmp_path / "LAS" / "WELL-A.las").write_text("~Version\n", encoding="utf-8")
    (nested / "WELL-B.LAS").write_text("~Version\n", encoding="utf-8")
    (tmp_path / "survey.sgy").write_bytes(b"segy")
    (tmp_path / "core_1000ft-1001ft.jpg").write_bytes(b"image")
    hidden_workspace = tmp_path / "old.nvt"
    hidden_workspace.mkdir()
    (hidden_workspace / "inventory.sqlite").write_bytes(b"ignored")

    result = preflight_folder(tmp_path)
    groups = {row["type_key"]: row for row in result["groups"]}
    assert result["total_files"] == 4
    assert groups["las"]["count"] == 2
    assert groups["seismic"]["importable_count"] == 1
    assert groups["image"]["importable_count"] == 0
    assert all("old.nvt" not in row["relative_path"] for row in result["files"])


def test_model_inventory_distinguishes_geological_and_reservoir_objects():
    items = [
        {"id": 1, "relative_path": "GeoModel/Porosity Model/PHI.roff", "filename": "PHI.roff", "extension": ".roff", "bytes": 100, "representative": 1},
        {"id": 2, "relative_path": "Reserves/储量丰度平面图.tif", "filename": "储量丰度平面图.tif", "extension": ".tif", "bytes": 20, "representative": 0},
        {"id": 3, "relative_path": "ECLIPSE/BASE_CASE.DATA", "filename": "BASE_CASE.DATA", "extension": ".data", "bytes": 300, "representative": 0},
        {"id": 4, "relative_path": "ECLIPSE/PVT/PVTO.INC", "filename": "PVTO.INC", "extension": ".inc", "bytes": 30, "representative": 0},
        {"id": 5, "relative_path": "Simulation/History Match/hm_v3.xlsx", "filename": "hm_v3.xlsx", "extension": ".xlsx", "bytes": 40, "representative": 0},
    ]
    assert classify_model_item("Interpretation/reservoir properties.csv", "reservoir properties.csv", ".csv") is None
    result = model_inventory(items)
    types = {row["type_key"] for row in result["items"]}
    assert types == {"property_model", "reserve_abundance_map", "simulation_model", "pvt", "history_match"}
    assert result["summary"]["geological_files"] == 2
    assert result["summary"]["reservoir_files"] == 3
    assert result["summary"]["total_types"] == 15


def test_focus_merge_prefers_complete_curve_and_persists_manual_override(tmp_path):
    database = Database(tmp_path / "focus.sqlite")
    snapshot = snapshot_for(tmp_path / "project")
    profile = {
        "source": "test-profile", "sample_wells": 2,
        "wells": [
            {"well_key": "A", "well_name": "A", "filename": "A.las", "start": 0, "stop": 100, "step": .1,
             "curves": [
                 {"mnemonic": "GR", "unit": "API", "sample_count": 500, "value_min": 10, "value_max": 120, "p05": 20, "p95": 100},
                 {"mnemonic": "GR_1", "unit": "API", "sample_count": 1001, "value_min": 10, "value_max": 120, "p05": 20, "p95": 100},
             ]},
            {"well_key": "B", "well_name": "B", "filename": "B.las", "start": 10, "stop": 80, "step": .1,
             "curves": [{"mnemonic": "GR", "unit": "API", "sample_count": 701, "value_min": 12, "value_max": 118, "p05": 22, "p95": 98}]},
        ],
    }
    assert suggest_curve_type("GR_DS")["type_key"] == "gamma_ray"
    with database.connect() as conn:
        automatic = focus_merge_payload(conn, snapshot, profile, "gamma_ray")
        row_a = next(row for row in automatic["rows"] if row["well_key"] == "A")
        assert automatic["summary"]["multi_candidate_wells"] == 1
        assert row_a["selected"]["mnemonic"] == "GR_1"
        assert row_a["selected"]["near_duplicates"]
        save_focus_decision(
            conn, str((tmp_path / "project").resolve()), "gamma_ray", "A", None,
            [row_a["selected"]["candidate_id"]],
        )
        manual = focus_merge_payload(conn, snapshot, profile, "gamma_ray")
        row_a = next(row for row in manual["rows"] if row["well_key"] == "A")
        assert row_a["selected"]["mnemonic"] == "GR"
        assert row_a["decision_status"] == "manual"
        clear_focus_decision(conn, str((tmp_path / "project").resolve()), "gamma_ray", "A")
        restored = focus_merge_payload(conn, snapshot, profile, "gamma_ray")
        assert next(row for row in restored["rows"] if row["well_key"] == "A")["selected"]["mnemonic"] == "GR_1"


def test_core_depth_parser_and_interpretation_type_persistence(tmp_path):
    feet = parse_core_depth_range("BOX_1000ft - 1001ft.jpg")
    assert feet["top_md_m"] == 304.8
    assert feet["base_md_m"] == pytest.approx(305.1048)
    assert parse_core_depth_range("core-photo-without-depth.jpg") is None

    database = Database(tmp_path / "interpretation.sqlite")
    snapshot = snapshot_for(tmp_path / "project")
    with database.connect() as conn:
        created = create_interpretation_type(conn, str((tmp_path / "project").resolve()), "黏土体积", "VSH")
        workbench = interpretation_workbench(conn, snapshot)
    assert created["type_key"] == "vsh"
    assert any(row["type_key"] == "vsh" and row["name"] == "黏土体积" for row in workbench["types"])


def test_curve_and_interpretation_share_an_exclusive_las_candidate_pool(tmp_path):
    database = Database(tmp_path / "shared-classification.sqlite")
    project_root = tmp_path / "project"
    snapshot = snapshot_for(project_root)
    profile = {
        "source": "test",
        "sample_wells": 1,
        "wells": [{
            "well_key": "A", "filename": "A.las", "start": 100, "stop": 500, "step": .1,
            "curves": [
                {"mnemonic": "GR", "unit": "API", "value_min": 10, "value_max": 120},
                {"mnemonic": "CUSTOM_ATTR", "unit": "", "value_min": 0, "value_max": 1},
            ],
        }],
    }
    root = str(project_root.resolve())
    with database.connect() as conn:
        curve_workbench(conn, snapshot, profile)
        interpretations = interpretation_workbench(conn, snapshot, profile)
        assert {row["attribute_name"] for row in interpretations["attributes"]} == {"CUSTOM_ATTR"}

        assign_interpretation_type(conn, root, "CUSTOM_ATTR", "porosity")
        curves = curve_workbench(conn, snapshot, profile)
        assert "CUSTOM_ATTR" not in {row["mnemonic"] for row in curves["curves"]}

        assign_curve_type(conn, root, "CUSTOM_ATTR", "gamma_ray")
        interpretations = interpretation_workbench(conn, snapshot, profile)
        assert "CUSTOM_ATTR" not in {row["attribute_name"] for row in interpretations["attributes"]}
        assert not conn.execute(
            "SELECT 1 FROM interpretation_type_assignments WHERE project_root=? AND attribute_name=?",
            (root, "CUSTOM_ATTR"),
        ).fetchone()


def test_global_curve_filter_and_background_progress(tmp_path):
    database = Database(tmp_path / "filter.sqlite")
    snapshot = snapshot_for(tmp_path / "project")
    wells = [{"project_key": "A", "x": 0, "y": 0}, {"project_key": "B", "x": 1, "y": 1}]
    profile = {"wells": [
        {"well_key": "A", "filename": "A.las", "start": 0, "stop": 100, "step": 1, "curves": [{"mnemonic": "GR"}]},
        {"well_key": "B", "filename": "B.las", "start": 0, "stop": 100, "step": 1, "curves": [{"mnemonic": "RHOB"}]},
    ]}
    with database.connect() as conn:
        rules = save_filter(conn, {"active": True, "conditions": [{"type": "curve", "operator": "exists", "value": "GR"}]})
        assert load_filter(conn) == rules
        matched, meta = evaluate_filter(conn, snapshot, wells, profile)
        assert matched == {"A"}
        assert meta["matched"] == 1

    jobs = ImportJobManager()
    job = jobs.submit([SAMPLES / "WELL-001.las"], tmp_path / "jobs.sqlite", {"data_type": "las"}, tmp_path / "process")
    seen_running = False
    for _ in range(100):
        status = jobs.status(job["id"])
        seen_running |= status["status"] == "running" or status["percent"] > 0
        if status["status"] in {"complete", "partial", "failed"}:
            break
        time.sleep(.02)
    assert status["status"] == "complete"
    assert status["percent"] == 100
    assert status["files"][0]["progress"] == 1.0
    assert seen_running or status["percent"] == 100
    assert (tmp_path / "process" / "jobs" / f"{job['id']}.json").is_file()


def test_curve_object_filter_and_on_demand_value_distribution(tmp_path):
    database = Database(tmp_path / "distribution.sqlite")
    snapshot = snapshot_for(tmp_path / "project")
    parsed = parse_las(SAMPLES / "WELL-001.las", include_samples=True, sample_limit=128)
    assert parsed["depth_unit"] == "M"
    assert len(next(row for row in parsed["curves"] if row["mnemonic"] == "GR")["samples"]) == 5
    profile = {
        "source": "test", "sample_wells": 1,
        "wells": [{
            "well_key": "WELL001", "well_name": "WELL-001", "filename": "WELL-001.las",
            "file_path": str(SAMPLES / "WELL-001.las"), "start": 1000, "stop": 1002,
            "step": .5, "depth_unit": "M",
            "curves": [{key: row.get(key) for key in (
                "mnemonic", "unit", "description", "sample_count", "value_min", "value_max",
                "value_mean", "value_std", "p05", "p50", "p95",
            )} for row in parsed["curves"][1:]],
        }],
    }
    with database.connect() as conn:
        workbench = curve_workbench(conn, snapshot, profile, {"WELL001"})
        rules = save_curve_filter(conn, {
            "active": True,
            "type_keys": ["gamma_ray"],
            "classification": "classified",
            "layer": {"mode": "md", "top_md": 1000.5, "base_md": 1001.5},
        })
        assert load_curve_filter(conn) == rules
        result = curve_distribution(
            conn, snapshot, profile, workbench, {"WELL001"},
            {"mode": "single_curve", "mnemonics": ["GR"], "bins": 12},
        )
        save_curve_filter(conn, {"active": True, "mnemonics": ["RHOB"]})
        coverage_detail = curve_distribution(
            conn, snapshot, profile, workbench, {"WELL001"},
            {"mode": "single_curve", "mnemonics": ["GR"], "bins": 12, "coverage_detail": True},
        )
    assert result["meta"]["matched_wells"] == 1
    assert result["meta"]["sample_points"] == 3
    assert result["groups"][0]["unit"] == "API"
    assert result["groups"][0]["series"][0]["stats"]["p50"] == 70.4
    assert coverage_detail["meta"]["matched_wells"] == 1
    assert coverage_detail["meta"]["sample_points"] == 5


def test_broad_curve_distribution_caps_las_reads_but_explicit_wells_are_exact(tmp_path, monkeypatch):
    import geo_inventory.curve_distribution as distribution_module

    database = Database(tmp_path / "broad-distribution.sqlite")
    source = tmp_path / "representative.las"
    source.write_text("placeholder", encoding="utf-8")
    snapshot = snapshot_for(tmp_path)
    wells = [{
        "well_key": f"WELL{index:04d}", "well_name": f"WELL-{index:04d}",
        "filename": "representative.las", "file_path": str(source), "depth_unit": "M",
        "curves": [{"mnemonic": "GR", "unit": "API"}],
    } for index in range(205)]
    profile = {"source": "test", "sample_wells": len(wells), "wells": wells}
    reads: list[str] = []

    def fake_las(path, modified_ns, size, mnemonic_key):
        reads.append(path)
        assert mnemonic_key == "GR"
        return {"depth_unit": "M", "curves": [{"mnemonic": "GR", "unit": "API", "samples": [{"md": 1000, "value": 65.0}]}]}

    monkeypatch.setattr(distribution_module, "_read_targeted_las", fake_las)
    workbench = {"curves": [{"mnemonic": "GR", "suggestion": {"type_key": "gamma_ray"}}], "types": [{"type_key": "gamma_ray", "name": "自然伽马", "color": "#53c7b7"}]}
    with database.connect() as conn:
        broad = curve_distribution(conn, snapshot, profile, workbench, {row["well_key"] for row in wells}, {"mode": "single_curve", "mnemonics": ["GR"]})
        exact = curve_distribution(conn, snapshot, profile, workbench, {row["well_key"] for row in wells}, {"mode": "single_curve", "mnemonics": ["GR"], "well_keys": [row["well_key"] for row in wells[:3]]})
    assert broad["meta"]["source_sampled"] is True
    assert broad["meta"]["source_files_candidate"] == 205
    assert broad["meta"]["source_files_read"] == 160
    assert exact["meta"]["source_sampled"] is False
    assert exact["meta"]["source_files_read"] == 3
    assert len(reads) == 163


def test_relationship_search_keeps_well_top_surface_and_curve_evidence_separate(tmp_path):
    database = Database(tmp_path / "relationship.sqlite")
    project = tmp_path / "project"
    project.mkdir()
    top_path = project / "Welltops WT2023"
    top_path.write_text(
        """BEGIN HEADER
X
Y
Z
MD
Type
Surface
Well
END HEADER
580180 2461616 -900 1000.5 Horizon \"KSF\" \"EBN-3000EXP\"
580180 2461616 -901 1001.5 Horizon \"KAN\" \"EBN-3000EXP\"
580280 2461716 -800 900 Horizon \"KSF\" \"CCL-1000\"
580280 2461716 -900 1000 Horizon \"MND23\" \"CCL-1000\"
""",
        encoding="utf-8",
    )
    dev_path = project / "EBN-3000EXP.dev"
    dev_path.write_text(
        "0 580180 2461616 58 0\n1001 580181 2461617 -943 1001\n1200 580182 2461618 -1142 1200\n",
        encoding="utf-8",
    )
    dev_path_2 = project / "CCL-1000.dev"
    dev_path_2.write_text(
        "0 580280 2461716 58 0\n900 580281 2461717 -842 900\n1200 580282 2461718 -1142 1200\n",
        encoding="utf-8",
    )
    snapshot = snapshot_for(project)
    root = str(project.resolve())
    wells = [
        {
            "id": "project:EBN3000EXP", "project_key": "EBN3000EXP", "canonical_name": "EBN-3000EXP",
            "source_types": ["well_head", "las", "deviation", "well_top"], "x": 580180, "y": 2461616,
            "_las_paths": [str(SAMPLES / "WELL-001.las")], "_dev_paths": [str(dev_path)],
            "total_depth": 1200, "max_survey_md": 1200,
        },
        {
            "id": "project:CCL1000", "project_key": "CCL1000", "canonical_name": "CCL-1000",
            "source_types": ["well_head", "las", "deviation", "well_top"], "x": 580280, "y": 2461716,
            "_las_paths": [str(SAMPLES / "WELL-001.las")], "_dev_paths": [str(dev_path_2)],
            "total_depth": 1200, "max_survey_md": 1200,
        },
    ]
    score, reasons = well_match_score("Ebano-3000", "EBN-3000EXP")
    assert score >= .95
    assert any("元音" in reason for reason in reasons)
    with database.connect() as conn:
        conn.execute(
            """INSERT INTO project_catalog_items(
                project_root,file_path,relative_path,filename,extension,category_key,source_folder,bytes,modified_at,representative
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (root, str(top_path), top_path.name, top_path.name, "[none]", "well_tops", ".", top_path.stat().st_size, "2023-01-01", 1),
        )
        conn.commit()
        default_preview = relationship_search(conn, snapshot, wells, "Ebano-3000")
        found = relationship_search(conn, snapshot, wells, "Ebano-3000, KSF, GR")
        missing = relationship_search(conn, snapshot, wells, "Ebano-3000, KTS, GR")
        multi_well = relationship_search(conn, snapshot, wells, "Ebano-3000, CCL-1000", "multi_well")
        multi_layer = relationship_search(conn, snapshot, wells, "Ebano-3000, KSF, KAN", "multi_layer")
    assert default_preview["curve"]["status"] == "default_selected"
    assert default_preview["curve"]["selection_mode"] == "default_preview"
    assert default_preview["curve"]["selected"]["mnemonic"] == "GR"
    assert {row["mnemonic"] for row in default_preview["curve"]["available_curves"]} >= {"GR", "RHOB", "NPHI"}
    assert {row["name"] for row in default_preview["layer"]["options"]} >= {"KSF", "KAN"}
    assert found["well"]["selected"]["name"] == "EBN-3000EXP"
    assert found["layer"]["well_top_matches"][0]["surface"] == "KSF"
    assert found["layer"]["interval"]["top_md"] == 1000.5
    assert found["layer"]["interval"]["base_md"] == 1001.5
    assert found["curve"]["selected"]["mnemonic"] == "GR"
    assert found["curve"]["stats"]["count"] == 3
    assert found["trajectory"]["interval_top"]["tvd"] == pytest.approx(1000.5)
    assert missing["layer"]["well_top_matches"] == []
    assert missing["layer"]["structural_matches"] == []
    assert {row["surface"] for row in missing["layer"]["available_tops"]} == {"KSF", "KAN"}
    assert multi_well["mode"] == "multi_well"
    assert len(multi_well["multi_well"]["wells"]) == 2
    assert len(multi_well["multi_well"]["wells"][0]["trajectory"]["map_points"]) == 3
    assert multi_well["multi_well"]["wells"][0]["trajectory"]["map_points"][0]["md"] == 0
    assert multi_well["multi_well"]["pairs"][0]["head_distance"] == pytest.approx(2 ** .5 * 100)
    assert multi_well["multi_well"]["common_layers"] == ["KSF"]
    assert multi_layer["mode"] == "multi_layer"
    assert [row["query"] for row in multi_layer["multi_layer"]["resolved_layers"]] == ["KSF", "KAN"]
    assert multi_layer["multi_layer"]["segments"][0]["md_thickness"] == 1.0


def test_horizon_hit_uses_md_to_tvdss_conversion(tmp_path):
    surface = tmp_path / "H1.ptd"
    surface.write_text(
        "FSASCI 0 1 COMPUTED 0 0.1E+31\nFSLIMI 0 10 0 10 50 50\n"
        "FSNROW 2 2\nFSXINC 10 10\n->MSMODL\n50 50\n50 50\n",
        encoding="utf-8",
    )
    snapshot = snapshot_for(tmp_path / "project")
    snapshot["surface"] = {
        "path": str(surface), "rows": 2, "columns": 2,
        "x_increment": 10, "y_increment": 10,
        "bounds": {"x_min": 0, "x_max": 10, "y_min": 0, "y_max": 10},
    }
    wells = [{"project_key": "VERTICAL-1", "x": 0, "y": 0, "total_depth": 100, "kb_elevation": 0, "_dev_paths": []}]
    database = Database(tmp_path / "horizon.sqlite")
    with database.connect() as conn:
        result = compute_surface_horizon_hits(conn, snapshot, wells, "surface:H1.ptd")
        hit = dict(conn.execute("SELECT * FROM horizon_well_hits").fetchone())
    assert result["hit_wells"] == 1
    assert hit["intersection_md"] == 50
    assert hit["surface_z"] == 50


def test_horizon_workbench_counts_top_versions_against_hard_wells(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    top_v1 = project / "Welltops V1"
    top_v2 = project / "Welltops V2"
    header = "BEGIN HEADER\nX\nY\nZ\nMD\nType\nSurface\nWell\nEND HEADER\n"
    top_v1.write_text(
        header
        + '0 0 -100 100 Horizon "KSF" "W-1"\n'
        + '1 1 -150 150 Horizon "KAN" "W-1"\n'
        + '2 2 -120 120 Horizon "KSF" "W-2"\n',
        encoding="utf-8",
    )
    top_v2.write_text(
        header
        + '0 0 -101 101 Horizon "KSF" "W-1"\n'
        + '2 2 -121 121 Horizon "KSF" "W-2"\n',
        encoding="utf-8",
    )
    surface = project / "KSF_V12.ptd"
    surface.write_text(
        "FSASCI 0 1 COMPUTED 0 0.1E+31\nFSLIMI 0 10 0 10 50 90\n"
        "FSNROW 2 2\nFSXINC 10 10\n->MSMODL\n50 60\n70 80\n",
        encoding="utf-8",
    )
    snapshot = snapshot_for(project)
    root = str(project.resolve())
    wells = [
        {"id": f"project:W{index}", "project_key": f"W{index}", "canonical_name": f"W-{index}",
         "source_types": ["well_head"], "curve_names": ["GR"] if index == 1 else [],
         "x": float(index), "y": float(index), "_dev_paths": [], "max_survey_md": 300}
        for index in (1, 2, 3)
    ]
    database = Database(tmp_path / "layers.sqlite")
    with database.connect() as conn:
        for index, path in enumerate((top_v1, top_v2), 1):
            conn.execute(
                """INSERT INTO project_catalog_items(
                    project_root,file_path,relative_path,filename,extension,category_key,source_folder,bytes,modified_at,representative
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (root, str(path), path.name, path.name, "[none]", "well_tops", ".", path.stat().st_size, f"2026-01-0{index}", index == 1),
            )
        conn.execute(
            """INSERT INTO project_catalog_items(
                project_root,file_path,relative_path,filename,extension,category_key,source_folder,bytes,modified_at,representative
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (root, str(surface), surface.name, surface.name, ".ptd", "horizons", ".", surface.stat().st_size, "2026-01-03", 1),
        )
        conn.commit()
        result = horizon_workbench(conn, snapshot, wells)
        detail = horizon_detail(conn, snapshot, wells, "well_top", "KSF")
    ksf = next(row for row in result["well_top"]["layers"] if row["key"] == "KSF")
    assert result["summary"]["unified_wells"] == 3
    assert result["summary"]["well_top_files"] == 2
    assert ksf["well_count"] == 2
    assert ksf["missing_count"] == 1
    assert ksf["version_count"] == 2
    assert detail["well_count"] == 2
    assert [row["well_name"] for row in detail["missing_wells"]] == ["W-3"]
    assert surface_metadata(surface)["kind"] == "regular_surface"


def test_horizon_name_unification_merges_well_top_coverage(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    tops = project / "well_tops.txt"
    header = "BEGIN HEADER\nX\nY\nZ\nMD\nType\nSurface\nWell\nEND HEADER\n"
    tops.write_text(
        header
        + '0 0 -100 100 Horizon "T_INF" "W-1"\n'
        + '1 1 -130 130 Horizon "T_base" "W-2"\n',
        encoding="utf-8",
    )
    snapshot = snapshot_for(project)
    root = str(project.resolve())
    wells = [
        {"project_key": f"W{index}", "canonical_name": f"W-{index}", "source_types": ["well_head"],
         "curve_names": [], "x": float(index), "y": float(index), "_dev_paths": []}
        for index in (1, 2, 3)
    ]
    database = Database(tmp_path / "canonical-layers.sqlite")
    with database.connect() as conn:
        conn.execute(
            """INSERT INTO project_catalog_items(
                project_root,file_path,relative_path,filename,extension,category_key,source_folder,bytes,modified_at,representative
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (root, str(tops), tops.name, tops.name, ".txt", "well_tops", ".", tops.stat().st_size, "2026-01-01", 1),
        )
        create_horizon_name_group(conn, root, "T 统一顶界")
        assign_horizon_name_aliases(conn, root, "T统一顶界", [
            {"key": "TINF", "name": "T_INF"}, {"key": "TBASE", "name": "T_base"},
        ])
        result = horizon_workbench(conn, snapshot, wells)
        detail = horizon_detail(conn, snapshot, wells, "well_top", "T统一顶界")
    assert result["summary"]["well_top_layers"] == 1
    layer = result["well_top"]["layers"][0]
    assert layer["unified_name"] == "T 统一顶界"
    assert layer["raw_names"] == ["T_INF", "T_base"]
    assert layer["well_count"] == 2
    assert layer["missing_count"] == 1
    assert detail["name"] == "T 统一顶界"
    assert detail["well_count"] == 2


def test_xyz_zmap_surface_intersects_vertical_well(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    surface = project / "KSF.zmap+"
    surface.write_text("0 0 50\n10 0 50\n0 10 50\n10 10 50\n", encoding="utf-8")
    snapshot = snapshot_for(project)
    root = str(project.resolve())
    wells = [{
        "project_key": "W1", "canonical_name": "W-1", "source_types": ["well_head"],
        "x": 0.0, "y": 0.0, "total_depth": 100.0, "kb_elevation": 0.0, "_dev_paths": [],
    }]
    database = Database(tmp_path / "xyz-horizon.sqlite")
    with database.connect() as conn:
        cursor = conn.execute(
            """INSERT INTO project_catalog_items(
                project_root,file_path,relative_path,filename,extension,category_key,source_folder,bytes,modified_at,representative
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (root, str(surface), surface.name, surface.name, ".zmap+", "horizons", ".", surface.stat().st_size, "2026-01-03", 1),
        )
        conn.commit()
        result = compute_catalog_surface_hits(conn, snapshot, wells, cursor.lastrowid)
        hit = dict(conn.execute("SELECT * FROM horizon_well_hits").fetchone())
    assert surface_metadata(surface)["kind"] == "xyz_surface"
    assert result["point_count"] == 4
    assert result["hit_wells"] == 1
    assert hit["intersection_md"] == pytest.approx(50.0)
    assert hit["surface_z"] == 50.0
