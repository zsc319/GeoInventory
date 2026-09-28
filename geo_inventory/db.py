from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_path TEXT,
    data_type TEXT NOT NULL,
    batch TEXT,
    version TEXT,
    crs TEXT,
    imported_at TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'importing',
    warning TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS wells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    normalized_key TEXT NOT NULL UNIQUE,
    uwi TEXT,
    x REAL,
    y REAL,
    crs TEXT,
    kb_elevation REAL,
    total_depth REAL,
    preferred_source_id INTEGER,
    confidence_score INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (preferred_source_id) REFERENCES sources(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_wells_uwi
ON wells(uwi) WHERE uwi IS NOT NULL AND uwi <> '';

CREATE TABLE IF NOT EXISTS alias_rules (
    alias_key TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS well_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    well_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    raw_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    row_no INTEGER,
    uwi TEXT,
    x REAL,
    y REAL,
    crs TEXT,
    kb_elevation REAL,
    total_depth REAL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (well_id) REFERENCES wells(id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_well_sources_well ON well_sources(well_id);
CREATE INDEX IF NOT EXISTS idx_well_sources_source ON well_sources(source_id);

CREATE TABLE IF NOT EXISTS las_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_id INTEGER NOT NULL,
    start_md REAL,
    stop_md REAL,
    step REAL,
    depth_unit TEXT,
    null_value REAL,
    curve_count INTEGER NOT NULL DEFAULT 0,
    sample_rows INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE,
    FOREIGN KEY (well_id) REFERENCES wells(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS las_curves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    las_file_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    well_id INTEGER NOT NULL,
    mnemonic TEXT NOT NULL,
    unit TEXT,
    description TEXT,
    sample_count INTEGER NOT NULL DEFAULT 0,
    value_min REAL,
    value_max REAL,
    FOREIGN KEY (las_file_id) REFERENCES las_files(id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE,
    FOREIGN KEY (well_id) REFERENCES wells(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_las_curves_name ON las_curves(mnemonic);

CREATE TABLE IF NOT EXISTS deviation_stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_id INTEGER NOT NULL,
    md REAL NOT NULL,
    inclination REAL NOT NULL,
    azimuth REAL NOT NULL,
    tvd REAL NOT NULL,
    northing REAL NOT NULL,
    easting REAL NOT NULL,
    tvdss REAL,
    z_msl REAL,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE,
    FOREIGN KEY (well_id) REFERENCES wells(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dev_well_md ON deviation_stations(well_id, md);

CREATE TABLE IF NOT EXISTS interpretation_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_id INTEGER NOT NULL,
    row_no INTEGER,
    top_md REAL,
    base_md REAL,
    attribute_name TEXT NOT NULL,
    attribute_value TEXT,
    numeric_value REAL,
    unit TEXT,
    version TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE,
    FOREIGN KEY (well_id) REFERENCES wells(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_interp_attr ON interpretation_values(attribute_name);

CREATE TABLE IF NOT EXISTS seismic_surveys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    dimension TEXT NOT NULL,
    trace_count INTEGER NOT NULL,
    sample_count_min INTEGER,
    sample_count_max INTEGER,
    sample_interval_us INTEGER,
    format_code INTEGER,
    x_min REAL,
    x_max REAL,
    y_min REAL,
    y_max REAL,
    inline_min INTEGER,
    inline_max INTEGER,
    crossline_min INTEGER,
    crossline_max INTEGER,
    footprint_area REAL,
    crs TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS polygons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    crs TEXT,
    geometry_json TEXT NOT NULL,
    area REAL,
    x_min REAL,
    x_max REAL,
    y_min REAL,
    y_max REAL,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_catalog_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_root TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    category_key TEXT NOT NULL,
    source_folder TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    modified_at TEXT,
    representative INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_project_catalog_category
ON project_catalog_items(project_root, category_key);

CREATE INDEX IF NOT EXISTS idx_project_catalog_folder
ON project_catalog_items(project_root, source_folder);

CREATE TABLE IF NOT EXISTS project_catalog_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_root TEXT NOT NULL,
    category_key TEXT NOT NULL,
    parent_id INTEGER,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES project_catalog_groups(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_group_name
ON project_catalog_groups(project_root, category_key, COALESCE(parent_id, 0), name);

CREATE TABLE IF NOT EXISTS project_catalog_group_items (
    group_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (group_id, item_id),
    FOREIGN KEY (group_id) REFERENCES project_catalog_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES project_catalog_items(id) ON DELETE CASCADE
);

-- A well group is an explicit, non-destructive delivery scope.  It stores
-- project well keys rather than touching aliases or source files.
CREATE TABLE IF NOT EXISTS project_well_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_root TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_root, name)
);

CREATE TABLE IF NOT EXISTS project_well_group_members (
    group_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    PRIMARY KEY (group_id, well_key),
    FOREIGN KEY (group_id) REFERENCES project_well_groups(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_well_group_members
ON project_well_group_members(group_id, well_key);

CREATE TABLE IF NOT EXISTS project_well_aliases (
    project_root TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_root, alias_key)
);

CREATE TABLE IF NOT EXISTS curve_types (
    project_root TEXT NOT NULL,
    type_key TEXT NOT NULL,
    name TEXT NOT NULL,
    canonical_role TEXT,
    color TEXT NOT NULL DEFAULT '#37c8c2',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_root, type_key)
);

CREATE TABLE IF NOT EXISTS curve_type_assignments (
    project_root TEXT NOT NULL,
    mnemonic TEXT NOT NULL,
    type_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'automatic',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_root, mnemonic),
    FOREIGN KEY (project_root, type_key) REFERENCES curve_types(project_root, type_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS interpretation_types (
    project_root TEXT NOT NULL,
    type_key TEXT NOT NULL,
    name TEXT NOT NULL,
    canonical_role TEXT,
    color TEXT NOT NULL DEFAULT '#37c8c2',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_root, type_key)
);

CREATE TABLE IF NOT EXISTS interpretation_type_assignments (
    project_root TEXT NOT NULL,
    attribute_name TEXT NOT NULL,
    type_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'automatic',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_root, attribute_name),
    FOREIGN KEY (project_root, type_key) REFERENCES interpretation_types(project_root, type_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS curve_focus_decisions (
    project_root TEXT NOT NULL,
    type_key TEXT NOT NULL,
    well_key TEXT NOT NULL,
    selected_candidate_id TEXT,
    excluded_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_root, type_key, well_key),
    FOREIGN KEY (project_root, type_key) REFERENCES curve_types(project_root, type_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workspace_settings (
    setting_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Production clustering schemes are first-class workspace objects.  The
-- exported JSON/CSV bundle remains useful for delivery, while these rows let
-- the UI restore a scheme without retraining when the .nvt is opened again.
CREATE TABLE IF NOT EXISTS production_clustering_plans (
    plan_id TEXT PRIMARY KEY,
    plan_name TEXT NOT NULL,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_production_clustering_plans_updated
ON production_clustering_plans(updated_at DESC);

-- Map well groups deliberately live outside a clustering plan: a group is a
-- spatial/workspace selection and can be reused by every saved model scheme.
CREATE TABLE IF NOT EXISTS production_clustering_well_groups (
    group_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL,
    well_keys_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_production_clustering_well_groups_updated
ON production_clustering_well_groups(updated_at DESC);

-- Named volumetric parameter cases.  Every property keeps its own choice of
-- constant value or referenced ZMAP surface so several reserve cases can be
-- restored and audited from the workspace.
CREATE TABLE IF NOT EXISTS reserve_parameter_groups (
    group_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    result_summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reserve_parameter_groups_updated
ON reserve_parameter_groups(updated_at DESC);

CREATE TABLE IF NOT EXISTS reserve_surfaces (
    surface_id INTEGER PRIMARY KEY AUTOINCREMENT,
    surface_name TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Materialized per-well production features.  A source signature invalidates
-- this automatically when production rows, events, intervals or locations
-- change, while repeated model runs can reuse the same audited feature set.
CREATE TABLE IF NOT EXISTS production_clustering_feature_cache (
    cache_key TEXT PRIMARY KEY,
    source_signature TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    well_count INTEGER NOT NULL DEFAULT 0,
    built_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS horizon_well_hits (
    project_root TEXT NOT NULL,
    horizon_key TEXT NOT NULL,
    well_key TEXT NOT NULL,
    hit INTEGER NOT NULL,
    intersection_md REAL,
    surface_z REAL,
    method TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (project_root, horizon_key, well_key)
);

-- A user-maintained vocabulary for Well Top names and structural-surface names.
-- The raw files are never renamed: aliases only affect how inventory statistics
-- and cross-source matching are grouped inside one project.
CREATE TABLE IF NOT EXISTS horizon_name_groups (
    project_root TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_root, canonical_key)
);

CREATE TABLE IF NOT EXISTS horizon_name_aliases (
    project_root TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    alias_name TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_root, alias_key),
    FOREIGN KEY (project_root, canonical_key)
        REFERENCES horizon_name_groups(project_root, canonical_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_horizon_name_aliases_canonical
ON horizon_name_aliases(project_root, canonical_key);

CREATE TABLE IF NOT EXISTS production_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    top_md REAL NOT NULL,
    base_md REAL NOT NULL,
    interval_name TEXT,
    status TEXT,
    event_date TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_interval_well
ON production_intervals(well_key, top_md, base_md);

CREATE TABLE IF NOT EXISTS production_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    production_month TEXT,
    days_on REAL,
    liquid_rate REAL,
    oil_rate REAL,
    water_rate REAL,
    water_cut REAL,
    monthly_oil REAL,
    monthly_water REAL,
    cumulative_oil REAL,
    cumulative_water REAL,
    pressure REAL,
    pressure_type TEXT,
    status TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_monthly_well_month
ON production_monthly(well_key, production_month);

-- A correction run is a versioned, reversible snapshot.  The raw imported
-- production table is never updated in place; readers opt into the latest
-- completed run and can switch back to the original source at any time.
CREATE TABLE IF NOT EXISTS production_correction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    corrected_mdb_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    well_count INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    corrected_well_count INTEGER NOT NULL DEFAULT 0,
    corrected_value_count INTEGER NOT NULL DEFAULT 0,
    inferred_days_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_correction_runs_status
ON production_correction_runs(status, id DESC);

CREATE TABLE IF NOT EXISTS production_corrected_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    production_month TEXT,
    days_on REAL,
    liquid_rate REAL,
    oil_rate REAL,
    water_rate REAL,
    gas_rate REAL,
    water_cut REAL,
    monthly_oil REAL,
    monthly_water REAL,
    monthly_gas REAL,
    cumulative_oil REAL,
    cumulative_water REAL,
    cumulative_gas REAL,
    pressure REAL,
    pressure_type TEXT,
    status TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (run_id) REFERENCES production_correction_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_corrected_run_well_month
ON production_corrected_monthly(run_id, well_key, production_month);

CREATE TABLE IF NOT EXISTS production_correction_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    field_key TEXT NOT NULL,
    field_label TEXT NOT NULL,
    unit TEXT,
    original_value REAL,
    corrected_value REAL,
    changed INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    FOREIGN KEY (run_id) REFERENCES production_correction_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_correction_audit_run_well
ON production_correction_audit(run_id, well_key, changed DESC, field_key);

CREATE TABLE IF NOT EXISTS production_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    event_date TEXT,
    event_type TEXT NOT NULL,
    status TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_production_event_well_date
ON production_events(well_key, event_date);

CREATE TABLE IF NOT EXISTS ofm_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    modified_at TEXT,
    driver TEXT,
    table_count INTEGER NOT NULL DEFAULT 0,
    nonempty_table_count INTEGER NOT NULL DEFAULT 0,
    total_rows INTEGER NOT NULL DEFAULT 0,
    mounted_at TEXT NOT NULL,
    warning TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ofm_table_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ofm_source_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    category TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    columns_json TEXT NOT NULL DEFAULT '[]',
    sample_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (ofm_source_id) REFERENCES ofm_sources(id) ON DELETE CASCADE,
    UNIQUE (ofm_source_id, table_name)
);

CREATE TABLE IF NOT EXISTS ofm_wells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    alias TEXT,
    x REAL,
    y REAL,
    surface_x REAL,
    surface_y REAL,
    kb_elevation REAL,
    total_depth REAL,
    completion_date TEXT,
    well_type TEXT,
    field_name TEXT,
    zone_name TEXT,
    status TEXT,
    interest TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ofm_wells_key ON ofm_wells(well_key);

CREATE TABLE IF NOT EXISTS ofm_injection_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    production_month TEXT,
    gas_injection REAL,
    water_injection REAL,
    steam_injection REAL,
    misc_injection REAL,
    solvent_injection REAL,
    air_injection REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ofm_injection_well_month
ON ofm_injection_monthly(well_key, production_month);

CREATE TABLE IF NOT EXISTS ofm_deviation_stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    md REAL NOT NULL,
    tvd REAL,
    x_offset REAL,
    y_offset REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ofm_deviation_well_md
ON ofm_deviation_stations(well_key, md);

CREATE TABLE IF NOT EXISTS ofm_markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    well_key TEXT NOT NULL,
    well_name TEXT NOT NULL,
    marker_name TEXT NOT NULL,
    depth_md REAL,
    marker_date TEXT,
    picker TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ofm_marker_well_depth
ON ofm_markers(well_key, depth_md);

-- Display-only aliases for files beneath a user-selected folder.  These are
-- intentionally kept separate from the catalogue: a translated name is a
-- lightweight view preference and never changes a source path or filename.
CREATE TABLE IF NOT EXISTS directory_display_translations (
    project_root TEXT NOT NULL,
    folder_path TEXT NOT NULL,
    file_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    extension TEXT,
    translated_at TEXT NOT NULL,
    translator_version TEXT NOT NULL,
    PRIMARY KEY (project_root, file_path)
);

CREATE INDEX IF NOT EXISTS idx_directory_display_translations_folder
ON directory_display_translations(project_root, folder_path);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @staticmethod
    def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    @staticmethod
    def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
