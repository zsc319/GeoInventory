from __future__ import annotations

import os
import re
import struct
from collections import defaultdict
from pathlib import Path
from typing import Any

from .importers import scan_segy, scan_zgy
from .project_scan import SEGY_EXTENSIONS, SEISMIC_EXTENSIONS


FORMAT_NAMES = {
    1: "IBM 32-bit 浮点", 2: "32-bit 整数", 3: "16-bit 整数", 5: "IEEE 32-bit 浮点",
    6: "IEEE 64-bit 浮点", 7: "24-bit 整数", 8: "8-bit 整数", 9: "64-bit 整数",
    10: "32-bit 无符号整数", 11: "16-bit 无符号整数", 12: "64-bit 无符号整数",
    15: "24-bit 无符号整数", 16: "8-bit 无符号整数",
}
FORMAT_BYTES = {1: 4, 2: 4, 3: 2, 5: 4, 6: 8, 7: 3, 8: 1, 9: 8, 10: 4, 11: 2, 12: 8, 15: 3, 16: 1}

ATTRIBUTE_RULES = [
    ("速度", ("VELOCITY", "VEL_", "VRMS", "VINT", "RMS_VEL", "VELOCIDAD")),
    ("阻抗", ("IMPEDANCE", "IMPEDANCIA", "AI_", "ACOUSTIC_IMPEDANCE")),
    ("相干/方差", ("COHERENCE", "COHERENCY", "SEMBLANCE", "VARIANCE", "CHAOS")),
    ("频率属性", ("FREQUENCY", "FREQ", "SPECTRAL")),
    ("相位属性", ("PHASE", "FASE")),
    ("包络/能量", ("ENVELOPE", "ENERGY", "SWEETNESS", "RMS_AMPLITUDE")),
    ("反演属性", ("INVERSION", "POROSITY", "POROSIDAD", "LITHOLOGY", "FACIES")),
    ("振幅", ("AMPLITUDE", "AMPLITUD", "STACK", "PSTM", "PSDM", "MIGRATION", "MIGRACION", "CFCG", "SFSG")),
]


def _decode_text_header(raw: bytes) -> str:
    candidates = [raw.decode("ascii", errors="replace"), raw.decode("cp500", errors="replace")]

    def score(text: str) -> float:
        upper = text.upper()
        keywords = sum(upper.count(word) for word in ("C01", "SEG-Y", "SEGY", "INLINE", "XLINE", "SURVEY", "SAMPLE"))
        readable = sum(ch.isalnum() or ch in " .,:;_-/()" for ch in text) / max(1, len(text))
        return keywords * 20 + readable

    return max(candidates, key=score).replace("\x00", " ").strip()


def infer_semantics(path: str | Path, text_header: str = "") -> dict[str, Any]:
    path = Path(path)
    evidence = f"{path.stem} {text_header[:1600]}".upper()
    domain = "深度域" if any(token in evidence for token in ("DEPTH", "PSDM", "TVD", "MD MIG")) else "时间域" if any(token in evidence for token in ("TWT", "TIME", "PSTM", "MSEC")) else "待确认"
    attribute = next((label for label, tokens in ATTRIBUTE_RULES if any(token in evidence for token in tokens)), "属性待确认")
    relative = str(path).replace("/", "\\").upper()
    dimension = "2D" if any(token in relative for token in ("\\2D\\", "SÍSMICA 2D", "SEISMIC 2D", "LINE_2D")) else "3D"
    source = "文件名 + 文本头关键字" if text_header else "文件名关键字"
    return {"domain": domain, "attribute_type": attribute, "dimension": dimension, "semantic_source": source}


def canonical_volume_name(path: str | Path) -> str:
    stem = Path(path).stem
    # Keep processing labels (PSTM, CFCG, velocity) because they identify
    # distinct deliverables. Only remove unmistakable copy/version suffixes.
    stem = re.sub(r"(?:[ _.-]*(?:COPY|COPIA|BACKUP|BAK|DUPLICATE))(?:[ _.-]*\d+)?$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    return re.sub(r"[\s._-]+", " ", stem).strip() or Path(path).name


def _vertical_summary(domain: str, sample_count: int | None, interval_us: int | None) -> tuple[str, str | None]:
    if not interval_us:
        return "待确认", None
    if domain == "时间域":
        interval = f"{interval_us / 1000:g} ms"
        extent = f"约 {(max(0, (sample_count or 1) - 1) * interval_us / 1000):g} ms 长度" if sample_count else None
        return interval, extent
    if domain == "深度域":
        return f"头字段 {interval_us}（深度单位待确认）", None
    return f"头字段 {interval_us} μs/单位", None


def _quick_segy(path: Path) -> dict[str, Any]:
    file_size = path.stat().st_size
    result: dict[str, Any] = {
        "format": "SEG-Y", "bytes": file_size, "trace_count": None, "trace_count_exact": False,
        "sample_count_min": None, "sample_count_max": None, "sample_interval_us": None,
        "sample_encoding": "待解析", "text_header_preview": "", "quick_status": "header",
    }
    if file_size < 3600:
        result.update({"quick_status": "invalid", "warning": "文件小于 3600 字节，不是完整 SEG-Y"})
        return {**result, **infer_semantics(path)}
    with path.open("rb") as handle:
        text_header = _decode_text_header(handle.read(3200))
        binary = handle.read(400)
    interval = struct.unpack(">H", binary[16:18])[0]
    samples = struct.unpack(">H", binary[20:22])[0]
    format_code = struct.unpack(">H", binary[24:26])[0]
    extended = struct.unpack(">h", binary[304:306])[0]
    extended = extended if extended >= 0 else 0
    result.update({
        "sample_interval_us": interval or None, "sample_count_min": samples or None,
        "sample_count_max": samples or None, "format_code": format_code or None,
        "sample_encoding": FORMAT_NAMES.get(format_code, f"格式代码 {format_code}" if format_code else "待解析"),
        "text_header_preview": text_header[:480],
    })
    bytes_per_sample = FORMAT_BYTES.get(format_code)
    trace_offset = 3600 + extended * 3200
    if samples and bytes_per_sample and file_size >= trace_offset:
        trace_bytes = 240 + samples * bytes_per_sample
        payload = file_size - trace_offset
        result["trace_count"] = payload // trace_bytes
        result["trace_count_exact"] = payload % trace_bytes == 0
        result["trace_count_method"] = "按固定样点数与文件长度精确计算" if result["trace_count_exact"] else "按固定样点数与文件长度估算；可能存在变长道"
    semantics = infer_semantics(path, text_header)
    interval_label, vertical_extent = _vertical_summary(semantics["domain"], samples or None, interval or None)
    return {**result, **semantics, "sample_interval_label": interval_label, "vertical_extent": vertical_extent}


def _quick_zgy(path: Path) -> dict[str, Any]:
    stats = scan_zgy(path)
    semantics = infer_semantics(path)
    if stats.get("z_unit_dimension"):
        unit = str(stats["z_unit_dimension"]).lower()
        semantics["domain"] = "时间域" if "time" in unit else "深度域" if any(token in unit for token in ("length", "depth")) else semantics["domain"]
        semantics["semantic_source"] = "ZGY 元数据 + 文件名"
    return {
        "format": "ZGY", "bytes": path.stat().st_size,
        "trace_count": stats.get("trace_count") or None, "trace_count_exact": bool(stats.get("zgy_decoder")),
        "sample_count_min": stats.get("sample_count_min"), "sample_count_max": stats.get("sample_count_max"),
        "sample_interval_us": None, "sample_interval_label": str(stats.get("z_increment")) if stats.get("z_increment") is not None else "待解析",
        "sample_encoding": stats.get("data_type") or "ZGY 压缩数据", "text_header_preview": "",
        "quick_status": "decoded" if stats.get("zgy_decoder") else "indexed",
        "warning": "；".join(stats.get("warnings") or []) or None,
        "inline_min": stats.get("inline_min"), "inline_max": stats.get("inline_max"),
        "crossline_min": stats.get("crossline_min"), "crossline_max": stats.get("crossline_max"),
        "x_min": stats.get("x_min"), "x_max": stats.get("x_max"), "y_min": stats.get("y_min"), "y_max": stats.get("y_max"),
        "z_min": stats.get("z_min"), "z_max": stats.get("z_max"), "zgy_decoder": stats.get("zgy_decoder", False),
        **semantics,
    }


def quick_file_metadata(path: str | Path, root: str | Path | None = None) -> dict[str, Any]:
    path = Path(path).resolve()
    stat = path.stat()
    result = _quick_zgy(path) if path.suffix.lower() == ".zgy" else _quick_segy(path)
    relative = str(path.relative_to(Path(root).resolve())) if root else path.name
    return {
        "path": str(path), "relative_path": relative, "directory": str(Path(relative).parent),
        "filename": path.name, "extension": path.suffix.lower(), "modified_at": stat.st_mtime,
        "volume_name": canonical_volume_name(path), "group_key": re.sub(r"[^A-Z0-9]+", "", canonical_volume_name(path).upper()),
        **result,
    }


def build_inventory(root: str | Path, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    files = []
    errors = []
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.suffix.lower() not in SEISMIC_EXTENSIONS:
                continue
            try:
                files.append(quick_file_metadata(path, root))
            except (OSError, ValueError, struct.error) as exc:
                errors.append({"path": str(path), "error": str(exc)})

    # Reuse the expensive representative scans already stored in the project
    # snapshot. Path equality keeps copies in other folders separate.
    detailed_by_path = {}
    for item in ((snapshot or {}).get("seismic") or {}).values():
        if isinstance(item, dict) and item.get("path"):
            detailed_by_path[str(Path(item["path"]).resolve()).lower()] = item
    for item in files:
        detailed = detailed_by_path.get(item["path"].lower())
        if detailed:
            item.update(enrich_detail(item, detailed))
            item["analysis_status"] = "已完整解析"
        else:
            item["analysis_status"] = "快速头信息" if item["quick_status"] in {"header", "decoded"} else "仅索引"

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in files:
        grouped[item["group_key"] or item["path"].lower()].append(item)
    groups = []
    for key, paths in grouped.items():
        paths.sort(key=lambda row: row["relative_path"].lower())
        groups.append({
            "key": key, "name": paths[0]["volume_name"], "path_count": len(paths),
            "formats": sorted({row["format"] for row in paths}), "dimensions": sorted({row["dimension"] for row in paths}),
            "domains": sorted({row["domain"] for row in paths}), "attribute_types": sorted({row["attribute_type"] for row in paths}),
            "bytes": sum(row["bytes"] for row in paths), "paths": paths,
        })
    groups.sort(key=lambda row: (row["name"].lower(), row["key"]))
    return {
        "root": str(root), "groups": groups, "file_count": len(files), "group_count": len(groups),
        "sgy_count": sum(item["extension"] in SEGY_EXTENSIONS for item in files),
        "zgy_count": sum(item["extension"] == ".zgy" for item in files),
        "duplicate_group_count": sum(group["path_count"] > 1 for group in groups),
        "bytes": sum(item["bytes"] for item in files), "errors": errors[:100],
        "method": "全目录检索扩展名；SEG-Y 快速读取文本头/二进制头，完整 Inline/Crossline 范围按选中文件再扫描；同名副本合并为一个大类且路径逐条保留。",
    }


def enrich_detail(base: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    semantics = infer_semantics(base["path"], stats.get("text_header_preview") or base.get("text_header_preview") or "")
    inline_min, inline_max = stats.get("inline_min"), stats.get("inline_max")
    crossline_min, crossline_max = stats.get("crossline_min"), stats.get("crossline_max")
    inline_count = abs(int(inline_max) - int(inline_min)) + 1 if inline_min is not None and inline_max is not None else None
    crossline_count = abs(int(crossline_max) - int(crossline_min)) + 1 if crossline_min is not None and crossline_max is not None else None
    sample_count = stats.get("sample_count_max")
    interval = stats.get("sample_interval_us")
    interval_label, vertical_extent = _vertical_summary(semantics["domain"], sample_count, interval)
    z_min = stats.get("z_min")
    z_max = stats.get("z_max")
    if semantics["domain"] == "时间域" and stats.get("delay_time_min_ms") is not None:
        z_min = stats["delay_time_min_ms"]
        z_max = stats.get("trace_end_time_max_ms")
        vertical_extent = f"{z_min:g} → {z_max:g} ms" if z_max is not None else vertical_extent
    trace_grid = f"{inline_count} × {crossline_count}" if inline_count and crossline_count else None
    if semantics["dimension"] == "2D" and stats.get("trace_count"):
        trace_grid = f"1 条测线 × {int(stats['trace_count']):,} 道"
    return {
        **base, **stats, **semantics,
        "trace_grid": trace_grid,
        "inline_count": inline_count, "crossline_count": crossline_count,
        "z_min": z_min, "z_max": z_max,
        "sample_encoding": FORMAT_NAMES.get(stats.get("format_code"), base.get("sample_encoding") or "待确认"),
        "sample_interval_label": interval_label, "vertical_extent": vertical_extent,
        "analysis_status": "已完整解析", "trace_count_exact": True,
    }


def analyze_file(path: str | Path, root: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    root = Path(root).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("只能解析当前项目目录内的地震文件") from exc
    if path.suffix.lower() not in SEISMIC_EXTENSIONS or not path.is_file():
        raise ValueError("指定路径不是可访问的 SEG-Y/ZGY 地震文件")
    base = quick_file_metadata(path, root)
    stats = scan_zgy(path) if path.suffix.lower() == ".zgy" else scan_segy(path)
    return enrich_detail(base, stats)
