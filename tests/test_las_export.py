from geo_inventory.las_export import build_selected_las, read_las_table


LAS_TEXT = """~Version Information
VERS. 2.0 : CWLS LOG ASCII STANDARD
WRAP. NO :
~Well Information
STRT.M 100.0 : Start
STOP.M 102.0 : Stop
STEP.M 0.5 : Step
NULL. -999.25 : Null
WELL. ALPHA-01 : Well
~Curve Information
DEPT.M : Measured depth
GR.API : Gamma ray
RHOB.G/C3 : Bulk density
~ASCII Log Data
100.0 10.0 2.10
100.5 20.0 2.20
101.0 -999.25 2.30
101.5 40.0 2.40
102.0 50.0 2.50
"""


def test_selected_las_always_keeps_depth_and_only_selected_curves(tmp_path):
    source = tmp_path / "source.las"
    output = tmp_path / "selected.las"
    source.write_text(LAS_TEXT, encoding="utf-8")

    result = build_selected_las(source, ["GR"], output)
    parsed = read_las_table(output)

    assert [curve["mnemonic"] for curve in parsed["curves"]] == ["DEPT", "GR"]
    assert result["depth_mnemonic"] == "DEPT"
    assert result["mnemonics"] == ["GR"]
    assert result["sample_rows"] == 5
    assert result["resampled"] is False
    output_text = output.read_text(encoding="utf-8")
    assert "NULL. -999.25" in output_text
    assert "WELL. ALPHA-01" in output_text


def test_selected_las_can_resample_on_a_regular_depth_grid(tmp_path):
    source = tmp_path / "source.las"
    output = tmp_path / "resampled.las"
    source.write_text(LAS_TEXT, encoding="utf-8")

    result = build_selected_las(source, ["RHOB"], output, resample_step=1.0)
    parsed = read_las_table(output)

    assert [curve["mnemonic"] for curve in parsed["curves"]] == ["DEPT", "RHOB"]
    assert [row[0] for row in parsed["rows"]] == [100.0, 101.0, 102.0]
    assert result["step"] == 1.0
    assert result["sample_rows"] == 3
    assert result["resampled"] is True


def test_selected_las_rejects_missing_curve(tmp_path):
    source = tmp_path / "source.las"
    source.write_text(LAS_TEXT, encoding="utf-8")
    try:
        build_selected_las(source, ["NPHI"], tmp_path / "missing.las")
    except ValueError as exc:
        assert "未找到选中的曲线" in str(exc)
    else:
        raise AssertionError("missing mnemonic should fail")
