"""Bounded, read-only SEG-Y section previews for the seismic inventory."""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path
from typing import Any

import numpy as np

from .importers import _decode_segy_text_header, _detect_trace_layout, _sample_trace_headers
from .project_scan import SEGY_EXTENSIONS
from .seismic_inventory import FORMAT_BYTES, infer_semantics


MAX_FILE_TRACES = 5_000_000
MAX_DISPLAY_TRACES = 240
MAX_DISPLAY_SAMPLES = 800


def _decode_samples(raw: bytes, format_code: int) -> np.ndarray:
    """Decode SEG-Y big-endian samples; return float64 for display scaling."""
    types = {
        2: ">i4", 3: ">i2", 5: ">f4", 6: ">f8", 8: "i1",
        9: ">i8", 10: ">u4", 11: ">u2", 12: ">u8", 16: "u1",
    }
    if format_code == 1:
        words = np.frombuffer(raw, dtype=">u4").astype(np.uint32)
        fraction = (words & 0x00FFFFFF).astype(np.float64) / 2**24
        exponent = ((words >> 24) & 0x7F).astype(np.int16) - 64
        sign = np.where((words & 0x80000000) != 0, -1.0, 1.0)
        return sign * fraction * np.exp2(4 * exponent)
    if format_code in (7, 15):
        triples = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        result = (triples[:, 0] << 16) | (triples[:, 1] << 8) | triples[:, 2]
        if format_code == 7:
            result = np.where(result & 0x800000, result - 0x1000000, result)
        return result.astype(np.float64)
    if format_code not in types:
        raise ValueError(f"剖面预览暂不支持 SEG-Y 样点格式代码 {format_code}")
    return np.frombuffer(raw, dtype=types[format_code]).astype(np.float64)


def _header_word(mapping: np.memmap, start: int, record_bytes: int, count: int, offset: int, dtype: str) -> np.ndarray:
    return np.ndarray((count,), dtype=dtype, buffer=mapping, offset=start + offset, strides=(record_bytes,))


def _equally_spaced(indexes: np.ndarray, limit: int) -> np.ndarray:
    if len(indexes) <= limit:
        return indexes
    return indexes[np.linspace(0, len(indexes) - 1, limit, dtype=np.int64)]


def preview_section(
    path: str | Path,
    root: str | Path,
    axis: str | None = None,
    value: int | float | str | None = None,
    max_traces: int = 160,
    max_samples: int = 500,
) -> dict[str, Any]:
    source = Path(path).resolve()
    project_root = Path(root).resolve()
    try:
        source.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("只能预览当前项目目录内的地震文件") from exc
    if source.suffix.lower() not in SEGY_EXTENSIONS or not source.is_file():
        raise ValueError("剖面预览目前支持当前工区内的 SEG-Y 文件")
    try:
        max_traces = min(MAX_DISPLAY_TRACES, max(2, int(max_traces)))
        max_samples = min(MAX_DISPLAY_SAMPLES, max(2, int(max_samples)))
    except (TypeError, ValueError) as exc:
        raise ValueError("剖面预览的道数或样点数不正确") from exc

    size = source.stat().st_size
    if size < 3600:
        raise ValueError("文件小于 3600 字节，不是完整 SEG-Y")
    with source.open("rb") as handle:
        text_header = _decode_segy_text_header(handle.read(3200))
        binary = handle.read(400)
        interval_us = struct.unpack_from(">H", binary, 16)[0]
        samples = struct.unpack_from(">H", binary, 20)[0]
        format_code = struct.unpack_from(">H", binary, 24)[0]
        extended = struct.unpack_from(">h", binary, 304)[0]
        if extended < 0:
            raise ValueError("扩展文本头数量未知，无法安全定位地震道")
        if not samples or not interval_us:
            raise ValueError("二进制头缺少有效样点数或采样间隔")
        if format_code not in FORMAT_BYTES:
            if struct.unpack_from("<H", binary, 24)[0] in FORMAT_BYTES:
                raise ValueError("检测到小端 SEG-Y；当前剖面预览仅支持大端编码")
            raise ValueError(f"剖面预览暂不支持 SEG-Y 样点格式代码 {format_code}")
        trace_start = 3600 + extended * 3200
        record_bytes = 240 + samples * FORMAT_BYTES[format_code]
        payload = size - trace_start
        if payload <= 0 or payload % record_bytes:
            raise ValueError("当前剖面预览只支持固定长度地震道；文件可能包含变长道或已截断")
        count = payload // record_bytes
        if count > MAX_FILE_TRACES:
            raise ValueError(f"地震体超过 {MAX_FILE_TRACES:,} 道的轻量预览上限")
        diagnostic = _sample_trace_headers(handle, trace_start, size, samples, FORMAT_BYTES[format_code], 512)
        if not diagnostic:
            raise ValueError("没有可读取的 SEG-Y 道头")
        layout = _detect_trace_layout(diagnostic, text_header)

    mapping = np.memmap(source, dtype=np.uint8, mode="r")
    trace_samples = _header_word(mapping, trace_start, record_bytes, count, 114, ">u2")
    if np.any((trace_samples != 0) & (trace_samples != samples)):
        raise ValueError("文件中存在变长道，无法按固定网格安全预览")
    il = _header_word(mapping, trace_start, record_bytes, count, layout["inline"], ">i4") if layout["inline"] is not None else None
    xl = _header_word(mapping, trace_start, record_bytes, count, layout["crossline"], ">i4") if layout["crossline"] is not None else None
    text_says_2d = bool(re.search(r"(?:ESTUDIO|SURVEY|LINEA|LINE)\s*:?\s*2D|\b2D\b", text_header, re.IGNORECASE))
    text_says_3d = bool(re.search(r"(?:ESTUDIO|SURVEY)\s*:?\s*3D|\b3D\b", text_header, re.IGNORECASE))
    is_3d = bool(il is not None and xl is not None and not text_says_2d and
                 (np.min(il) != np.max(il) or np.min(xl) != np.max(xl)) and
                 (text_says_3d or (np.min(il) != np.max(il) and np.min(xl) != np.max(xl))))
    line_ranges: dict[str, Any] = {}
    selected_line: int | None = None
    if is_3d:
        if axis not in (None, "inline", "crossline"):
            raise ValueError("请选择 Inline 或 Crossline 剖面")
        axis = axis or "inline"
        for name, words in (("inline", il), ("crossline", xl)):
            unique = np.unique(words[words != 0])
            if not len(unique):
                raise ValueError("道头中没有有效的 Inline/Crossline 编号")
            line_ranges[name] = {"minimum": int(unique[0]), "maximum": int(unique[-1]), "count": int(len(unique))}
            if name == axis:
                if value is None or value == "":
                    selected_line = int(unique[len(unique) // 2])
                else:
                    try:
                        requested = float(value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("剖面编号必须是数字") from exc
                    if not math.isfinite(requested):
                        raise ValueError("剖面编号必须是有限数字")
                    selected_line = int(unique[np.argmin(np.abs(unique.astype(np.float64) - requested))])
        words = il if axis == "inline" else xl
        trace_indexes = np.flatnonzero(words == selected_line)
        other = xl if axis == "inline" else il
        trace_indexes = trace_indexes[np.argsort(other[trace_indexes], kind="stable")]
        label_values = other[trace_indexes]
    else:
        axis = "trace"
        trace_indexes = np.arange(count, dtype=np.int64)
        label_values = trace_indexes + 1
    if not len(trace_indexes):
        raise ValueError("所选剖面没有地震道")
    source_trace_count = int(len(trace_indexes))
    trace_indexes = _equally_spaced(trace_indexes, max_traces)
    if is_3d:
        label_values = (xl if axis == "inline" else il)[trace_indexes]
    else:
        label_values = trace_indexes + 1

    delays = _header_word(mapping, trace_start, record_bytes, count, 108, ">i2")[trace_indexes]
    intervals = _header_word(mapping, trace_start, record_bytes, count, 116, ">u2")[trace_indexes]
    intervals = np.where(intervals == 0, interval_us, intervals)
    if np.any(delays != delays[0]) or np.any(intervals != intervals[0]):
        raise ValueError("所选剖面的道起始时间或采样间隔不一致，无法在同一垂向轴上显示")
    sample_stride = max(1, math.ceil(samples / max_samples))
    sample_indexes = np.arange(0, samples, sample_stride, dtype=np.int64)
    amplitudes = np.empty((len(trace_indexes), len(sample_indexes)), dtype=np.float64)
    for position, trace_index in enumerate(trace_indexes):
        begin = trace_start + int(trace_index) * record_bytes + 240
        raw = mapping[begin:begin + samples * FORMAT_BYTES[format_code]].tobytes()
        amplitudes[position] = _decode_samples(raw, format_code)[sample_indexes]
    finite = amplitudes[np.isfinite(amplitudes)]
    if not finite.size:
        raise ValueError("所选剖面没有有效振幅样点")
    clip = float(np.percentile(np.abs(finite), 98)) or float(np.max(np.abs(finite))) or 1.0
    scaled = np.rint(np.clip(np.where(np.isfinite(amplitudes), amplitudes, 0) / clip, -1, 1) * 127).astype(np.int16)
    scaled[~np.isfinite(amplitudes)] = -128
    semantics = infer_semantics(source, text_header)
    time_domain = semantics["domain"] == "时间域"
    vertical_start = float(delays[0]) if time_domain else 0.0
    vertical_step = float(intervals[0]) * sample_stride / 1000 if time_domain else float(sample_stride)
    return {
        "filename": source.name, "dimension": "3D" if is_3d else "2D", "axis": axis,
        "selected_line": selected_line, "line_ranges": line_ranges, "byte_layout": layout["name"],
        "format_code": format_code, "domain": semantics["domain"],
        "vertical_start": vertical_start, "vertical_step": vertical_step,
        "vertical_unit": "ms" if time_domain else "样点", "sample_interval_us": int(intervals[0]),
        "source_trace_count": source_trace_count, "display_trace_count": int(len(trace_indexes)),
        "source_sample_count": samples, "display_sample_count": int(len(sample_indexes)),
        "trace_labels": [int(item) for item in label_values],
        "amplitudes": scaled.tolist(), "clip_amplitude": clip,
        "amplitude_min": float(finite.min()), "amplitude_max": float(finite.max()),
        "method": "只读固定长度 SEG-Y；按道号排序后等距抽取地震道和样点，道间距仅作显示且不代表真实空间距离；振幅按绝对值 98% 分位截幅并量化用于显示。垂向域来自文件名/文本头推断，深度域显示样点序号。",
    }
