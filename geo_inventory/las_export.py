from __future__ import annotations

import bisect
import math
import re
from pathlib import Path
from typing import Any


DEPTH_NAMES = {"DEPT", "DEPTH", "MD", "TDEP"}


def _number(value: Any) -> float | None:
    try:
        result = float(str(value).replace(",", "").strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _header_line(line: str) -> dict[str, Any] | None:
    content, _, description = line.partition(":")
    # Do not consume whitespace after the LAS separator before reading the
    # unit. ``NULL. -999.25`` has an empty unit, while ``GR.API`` has API.
    match = re.match(r"^\s*([^\.\s]+)\s*\.([^\s]*)\s*(.*?)\s*$", content)
    if not match:
        return None
    mnemonic, unit, value = match.groups()
    return {
        "mnemonic": mnemonic.upper(), "unit": unit or None,
        "value": value.strip(), "description": description.strip(), "raw": line.rstrip(),
    }


def _decode(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "latin-1", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path.name}: LAS 编码无法识别")


def read_las_table(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"源 LAS 文件不存在：{source}")
    text, encoding = _decode(source)
    section = ""
    version: list[dict[str, Any]] = []
    well: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    data_lines: list[str] = []
    wrap = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("~"):
            section = line[1:2].upper()
            continue
        if section == "A":
            data_lines.append(line)
            continue
        if section not in {"V", "W", "C"}:
            continue
        parsed = _header_line(line)
        if not parsed:
            continue
        if section == "V":
            version.append(parsed)
            if parsed["mnemonic"] == "WRAP":
                wrap = str(parsed["value"]).upper() == "YES"
        elif section == "W":
            well.append(parsed)
        else:
            curves.append(parsed)
    if not curves:
        raise ValueError(f"{source.name}: 未识别到 LAS 曲线定义")
    depth_index = next((index for index, curve in enumerate(curves) if curve["mnemonic"] in DEPTH_NAMES), 0)
    expected = len(curves)
    if wrap:
        tokens = " ".join(data_lines).replace(",", " ").split()
        raw_rows = [tokens[index:index + expected] for index in range(0, len(tokens), expected)]
    else:
        raw_rows = [line.replace(",", " ").split() for line in data_lines]
    rows: list[list[float | None]] = []
    for values in raw_rows:
        if len(values) < expected:
            continue
        parsed_values = [_number(values[index]) for index in range(expected)]
        if parsed_values[depth_index] is not None:
            rows.append(parsed_values)
    null_item = next((item for item in well if item["mnemonic"] == "NULL"), None)
    return {
        "path": source, "encoding": encoding, "version": version, "well": well,
        "curves": curves, "rows": rows, "wrap": wrap, "depth_index": depth_index,
        "depth_unit": curves[depth_index].get("unit") or "m",
        "null": _number(null_item.get("value")) if null_item else -999.25,
    }


def _fmt(value: float) -> str:
    if abs(value) >= 1_000_000 or (value and abs(value) < 1e-5):
        return f"{value:.9g}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _interpolate(samples: list[tuple[float, float]], depth: float, max_gap: float) -> float | None:
    if not samples:
        return None
    depths = [item[0] for item in samples]
    position = bisect.bisect_left(depths, depth)
    if position < len(samples) and abs(samples[position][0] - depth) <= 1e-8:
        return samples[position][1]
    if position <= 0 or position >= len(samples):
        return None
    left_depth, left_value = samples[position - 1]
    right_depth, right_value = samples[position]
    gap = right_depth - left_depth
    if gap <= 0 or gap > max_gap:
        return None
    ratio = (depth - left_depth) / gap
    return left_value + (right_value - left_value) * ratio


def _regular_depths(start: float, stop: float, step: float) -> list[float]:
    direction = 1.0 if stop >= start else -1.0
    signed_step = abs(step) * direction
    count = int(math.floor(abs(stop - start) / abs(step) + 1e-9)) + 1
    if count > 5_000_000:
        raise ValueError("重采样后超过 500 万行，请增大采样间隔")
    values = [start + index * signed_step for index in range(count)]
    if values and abs(values[-1] - stop) > abs(step) * 1e-6:
        values.append(stop)
    return values


def build_selected_las(
    source_path: str | Path,
    selected_mnemonics: list[str] | set[str],
    output_path: str | Path,
    *,
    resample_step: float | None = None,
) -> dict[str, Any]:
    table = read_las_table(source_path)
    wanted = {str(value).upper().strip() for value in selected_mnemonics if str(value).strip()}
    depth_index = table["depth_index"]
    selected_indices = [
        index for index, curve in enumerate(table["curves"])
        if index != depth_index and curve["mnemonic"] in wanted
    ]
    if not selected_indices:
        raise ValueError(f"{table['path'].name}: 未找到选中的曲线")
    depth_values = [row[depth_index] for row in table["rows"] if row[depth_index] is not None]
    if not depth_values:
        raise ValueError(f"{table['path'].name}: DEPTH 道没有有效样点")
    original_step = next(
        (_number(item["value"]) for item in table["well"] if item["mnemonic"] == "STEP" and _number(item["value"]) is not None),
        None,
    )
    if original_step is None and len(depth_values) > 1:
        differences = [abs(depth_values[index] - depth_values[index - 1]) for index in range(1, min(len(depth_values), 5000)) if depth_values[index] != depth_values[index - 1]]
        original_step = sorted(differences)[len(differences) // 2] if differences else None

    null_value = table["null"] if table["null"] is not None else -999.25
    if resample_step is not None:
        resample_step = float(resample_step)
        if not math.isfinite(resample_step) or resample_step <= 0:
            raise ValueError("重采样间隔必须为大于 0 的数值")
        output_depths = _regular_depths(depth_values[0], depth_values[-1], resample_step)
        ascending = depth_values[-1] >= depth_values[0]
        curve_samples: dict[int, list[tuple[float, float]]] = {}
        for curve_index in selected_indices:
            samples = [(row[depth_index], row[curve_index]) for row in table["rows"] if row[depth_index] is not None and row[curve_index] is not None and row[curve_index] != null_value]
            samples = [(float(depth), float(value)) for depth, value in samples]
            samples.sort(key=lambda item: item[0])
            curve_samples[curve_index] = samples
        gap_limit = max(abs(original_step or resample_step) * 5, resample_step * 2)
        output_rows = []
        for depth in output_depths:
            lookup_depth = depth
            values = [_interpolate(curve_samples[index], lookup_depth, gap_limit) for index in selected_indices]
            output_rows.append([depth, *values])
        if not ascending:
            # Samples were sorted only for interpolation; output_depths already
            # retain the source direction.
            pass
        step_value = resample_step if output_depths[-1] >= output_depths[0] else -resample_step
    else:
        output_rows = [[row[depth_index], *[row[index] for index in selected_indices]] for row in table["rows"]]
        step_value = original_step

    selected_curves = [table["curves"][index] for index in selected_indices]
    depth_curve = table["curves"][depth_index]
    start, stop = output_rows[0][0], output_rows[-1][0]
    replacements = {"STRT": start, "STOP": stop, "STEP": step_value, "NULL": null_value}
    well_lines: list[str] = []
    seen: set[str] = set()
    for item in table["well"]:
        mnemonic = item["mnemonic"]
        if mnemonic in replacements and replacements[mnemonic] is not None:
            unit = item.get("unit") or (table["depth_unit"] if mnemonic in {"STRT", "STOP", "STEP"} else "")
            well_lines.append(f"{mnemonic}.{unit or ''} {_fmt(float(replacements[mnemonic]))} : {item.get('description') or ''}")
            seen.add(mnemonic)
        else:
            well_lines.append(item["raw"])
    for mnemonic in ("STRT", "STOP", "STEP", "NULL"):
        if mnemonic not in seen and replacements[mnemonic] is not None:
            unit = table["depth_unit"] if mnemonic != "NULL" else ""
            well_lines.append(f"{mnemonic}.{unit} {_fmt(float(replacements[mnemonic]))} : Generated by GeoInventory")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("~Version Information\nVERS. 2.0 : CWLS LOG ASCII STANDARD\nWRAP. NO : One line per depth step\n")
        handle.write("~Well Information\n")
        for line in well_lines:
            handle.write(f"{line}\n")
        handle.write("~Curve Information\n")
        handle.write(f"{depth_curve['mnemonic']}.{depth_curve.get('unit') or table['depth_unit']} : {depth_curve.get('description') or 'Depth'}\n")
        for curve in selected_curves:
            handle.write(f"{curve['mnemonic']}.{curve.get('unit') or ''} : {curve.get('description') or ''}\n")
        handle.write("~ASCII Log Data\n")
        for row in output_rows:
            values = [_fmt(float(row[0]))]
            values.extend(_fmt(float(value)) if value is not None and value != null_value else _fmt(float(null_value)) for value in row[1:])
            handle.write(" ".join(values) + "\n")
    return {
        "source_file": table["path"].name, "output_file": output.name,
        "mnemonics": [curve["mnemonic"] for curve in selected_curves],
        "depth_mnemonic": depth_curve["mnemonic"], "depth_unit": table["depth_unit"],
        "start": start, "stop": stop, "step": step_value,
        "original_step": original_step, "resampled": resample_step is not None,
        "sample_rows": len(output_rows), "wrap_source": table["wrap"],
    }
