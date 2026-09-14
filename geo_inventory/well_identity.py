from __future__ import annotations

from typing import Any, Iterable


HARD_WELL_SOURCES = ("well_head", "deviation", "las", "production")
HARD_WELL_SOURCE_LABELS = {
    "well_head": "Well Head",
    "deviation": "DEV",
    "las": "LAS",
    "production": "生产动态",
}


def classify_well_identity(
    source_types: Iterable[str], coordinate_spread: float | None = None, conflict_tolerance: float = 5.0
) -> dict[str, Any]:
    """Separate physical-well evidence from cross-source name verification."""

    sources = {str(value) for value in source_types if value}
    hard_sources = [value for value in HARD_WELL_SOURCES if value in sources]
    missing = [value for value in HARD_WELL_SOURCES if value not in sources]
    exists = bool(hard_sources)
    coordinate_conflict = coordinate_spread is not None and coordinate_spread > conflict_tolerance

    if not exists:
        label = "待核实身份"
        score = 40
        reason = "只有 Well Top、Checkshot 等辅助记录，尚无 Well Head、DEV、LAS 或生产动态硬数据"
    elif coordinate_conflict:
        label = "待核实身份"
        score = 68
        reason = f"井实体已有硬数据，但不同来源井口坐标偏差 {coordinate_spread:.2f}，超过 {conflict_tolerance:g}"
    elif len(hard_sources) >= 2:
        label = "多源互证"
        score = min(98, 84 + len(hard_sources) * 4)
        reason = "、".join(HARD_WELL_SOURCE_LABELS[value] for value in hard_sources) + " 独立指向同一标准井名"
    else:
        label = "单源硬证据"
        score = 80
        reason = f"{HARD_WELL_SOURCE_LABELS[hard_sources[0]]} 已证明井实体存在；尚缺 " + "、".join(
            HARD_WELL_SOURCE_LABELS[value] for value in missing
        ) + " 交叉互证"

    return {
        "existence_confirmed": exists,
        "existence_label": "井实体已确认" if exists else "井实体待确认",
        "identity_status": label,
        "quality_label": label,
        "confidence_score": score,
        "verification_reason": reason,
        "hard_source_types": hard_sources,
        "hard_source_count": len(hard_sources),
        "missing_hard_sources": missing,
        "coordinate_conflict": coordinate_conflict,
    }
