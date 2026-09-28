from __future__ import annotations

import struct

import numpy as np
import pytest

from app import create_app
from geo_inventory.project_scan import save_snapshot
from geo_inventory.seismic_section import _decode_samples, preview_section


def _write_segy(path, *, two_d=False, variable=False):
    text = ("C01 SYNTHETIC 2D TIME" if two_d else "C01 SYNTHETIC 3D PSTM TIME").ljust(3200).encode("ascii")
    binary = bytearray(400)
    struct.pack_into(">H", binary, 16, 2000)
    struct.pack_into(">H", binary, 20, 8)
    struct.pack_into(">H", binary, 24, 5)
    payload = bytearray(text) + binary
    positions = [(0, index) for index in range(6)] if two_d else [(il, xl) for il in (10, 11, 12) for xl in (20, 21, 22)]
    for index, (inline, crossline) in enumerate(positions):
        header = bytearray(240)
        struct.pack_into(">h", header, 70, 1)
        struct.pack_into(">ii", header, 180, 500000 + index * 10, 2400000 + index * 10)
        if not two_d:
            struct.pack_into(">ii", header, 188, inline, crossline)
        struct.pack_into(">H", header, 114, 7 if variable and index == 0 else 8)
        payload.extend(header)
        payload.extend(struct.pack(">8f", *np.arange(index, index + 8, dtype=float)))
    path.write_bytes(payload)


def test_section_preview_selects_inline_crossline_and_bounds_payload(tmp_path):
    path = tmp_path / "cube_PSTM.sgy"
    _write_segy(path)
    inline = preview_section(path, tmp_path, axis="inline", value=11, max_traces=2, max_samples=4)
    assert inline["dimension"] == "3D"
    assert inline["selected_line"] == 11
    assert inline["trace_labels"] == [20, 22]
    assert inline["display_trace_count"] == 2
    assert inline["display_sample_count"] == 4
    assert inline["vertical_step"] == pytest.approx(4)
    assert len(inline["amplitudes"]) == 2
    assert len(inline["amplitudes"][0]) == 4
    crossline = preview_section(path, tmp_path, axis="crossline", value=21)
    assert crossline["trace_labels"] == [10, 11, 12]
    assert crossline["selected_line"] == 21
    nearest = preview_section(path, tmp_path, axis="inline", value=1000)
    assert nearest["selected_line"] == 12


def test_section_preview_2d_and_sample_decoding(tmp_path):
    path = tmp_path / "line_2D_TIME.sgy"
    _write_segy(path, two_d=True)
    result = preview_section(path, tmp_path)
    assert result["dimension"] == "2D"
    assert result["axis"] == "trace"
    assert result["trace_labels"] == [1, 2, 3, 4, 5, 6]
    assert _decode_samples(struct.pack(">II", 0x41100000, 0xC1100000), 1).tolist() == [1.0, -1.0]
    assert _decode_samples(bytes.fromhex("FFFFFF000001"), 7).tolist() == [-1.0, 1.0]


def test_section_preview_reads_petrel_swapped_grid_bytes(tmp_path):
    path = tmp_path / "petrel_PSTM.sgy"
    text = ("C01 SURVEY 3D TIME INLINE: 1008-1009 XLINE: 2001-2002").ljust(3200).encode("cp500")
    binary = bytearray(400)
    struct.pack_into(">H", binary, 16, 2000)
    struct.pack_into(">H", binary, 20, 2)
    struct.pack_into(">H", binary, 24, 5)
    payload = bytearray(text) + binary
    for inline in (1008, 1009):
        for crossline in (2001, 2002):
            header = bytearray(240)
            struct.pack_into(">h", header, 70, -100)
            struct.pack_into(">ii", header, 180, inline, crossline)
            struct.pack_into(">ii", header, 188, int((500000 + crossline * 15) * 100), int((2400000 + inline * 15) * 100))
            struct.pack_into(">H", header, 114, 2)
            payload.extend(header)
            payload.extend(struct.pack(">ff", -1.0, 1.0))
    path.write_bytes(payload)
    result = preview_section(path, tmp_path, axis="inline", value=1009)
    assert result["byte_layout"] == "petrel_pep_swapped"
    assert result["trace_labels"] == [2001, 2002]
    assert result["amplitudes"][0] == [-127, 127]


def test_section_preview_rejects_variable_traces_and_paths_outside_project(tmp_path):
    path = tmp_path / "variable.sgy"
    _write_segy(path, variable=True)
    with pytest.raises(ValueError, match="变长道"):
        preview_section(path, tmp_path)
    with pytest.raises(ValueError, match="当前项目目录"):
        preview_section(path, tmp_path / "other")
    truncated = tmp_path / "truncated.sgy"
    truncated.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="固定长度"):
        preview_section(truncated, tmp_path)
    little_endian = tmp_path / "little.sgy"
    payload = bytearray(path.read_bytes())
    struct.pack_into("<H", payload, 3200 + 24, 5)
    little_endian.write_bytes(payload)
    with pytest.raises(ValueError, match="小端"):
        preview_section(little_endian, tmp_path)


def test_section_api_and_page_mount(tmp_path):
    path = tmp_path / "cube_PSTM.sgy"
    _write_segy(path)
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot({
        "project": {"root": str(tmp_path), "name": "Test", "scanned_at": "2026-01-01", "total_files": 1, "total_bytes": path.stat().st_size},
        "categories": [], "representatives": [],
    }, snapshot_path)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.sqlite"), "PROJECT_SNAPSHOT": str(snapshot_path)})
    client = app.test_client()
    response = client.post("/api/seismic-inventory/section", json={"path": str(path), "axis": "inline", "value": 10})
    assert response.status_code == 200
    assert response.get_json()["selected_line"] == 10
    assert b"seismic-section-canvas" in client.get("/").data
    assert client.get("/static/seismic-section.js").status_code == 200
