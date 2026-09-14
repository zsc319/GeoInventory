from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable


MODEL_TAXONOMY = {
    "geological": {
        "label": "地质模型",
        "types": [
            ("structural_model", "构造模型体", "三维格架、断层框架、角点网格和构造分区"),
            ("sedimentary_model", "沉积模型体", "沉积相、岩相、岩石类型和地质体模型"),
            ("property_model", "属性模型体", "孔隙度、渗透率、饱和度、NTG、VSH 等三维属性"),
            ("reserve_case", "储量 Case 数据", "体积法 Case、GRV、孔隙体积、OOIP/STOIIP 与版本方案"),
            ("petrophysical_map", "孔渗饱平面图", "孔隙度、渗透率、含油/含水饱和度与净毛比平面成果"),
            ("reserve_abundance_map", "储量丰度平面图", "储量丰度、地质储量、可采储量与资源量平面成果"),
        ],
    },
    "reservoir": {
        "label": "油藏模型",
        "types": [
            ("simulation_model", "油藏数值模型体", "ECLIPSE、CMG、tNavigator 等网格、Deck、INIT/Restart 数据"),
            ("pvt", "PVT / 流体模型", "油、气、水 PVT 表，EOS 组分模型和流体区划"),
            ("scal", "相渗与毛管压力", "SCAL、SWOF/SGOF、相渗曲线、毛管压力与岩石压缩性"),
            ("initialization", "初始化与区域", "EQUIL、压力/饱和度初始化、流体接触面和 PVT/SATNUM 分区"),
            ("well_completion", "井与完井控制", "井轨迹、射孔、COMPDAT/WELSPECS、生产和注入约束"),
            ("history_match", "历史拟合", "观测量、拟合误差、参数调整记录和拟合版本"),
            ("forecast_strategy", "生产预测策略", "基准方案、增产/注水/注气、井网调整和开发情景"),
            ("simulation_result", "模拟结果", "Summary、Restart、RSM、场/井预测曲线和三维结果"),
            ("uncertainty_case", "不确定性与多方案", "敏感性、集合模型、概率 Case、优化与风险范围"),
        ],
    },
}


SIMULATION_EXTENSIONS = {".egrid", ".fegrid", ".init", ".finit", ".unrst", ".funrst", ".smspec", ".fsmspec", ".unsmry", ".funsmry", ".rsm", ".grdecl", ".x0001", ".a0001"}
MODEL_CONTEXT = ("model", "modelo", "geomodel", "geocellular", "static model", "modelo geologico", "地质模型", "油藏模型", "数模", "模拟")


def classify_model_item(relative_path: str, filename: str, extension: str) -> dict[str, Any] | None:
    text = f"{relative_path} {filename}".replace("_", " ").replace("-", " ").replace("\\", " ").replace("/", " ").lower()
    suffix = extension.lower() if extension else Path(filename).suffix.lower()
    model_context = any(token in text for token in MODEL_CONTEXT)

    rules = [
        ("reservoir", "simulation_result", suffix in SIMULATION_EXTENSIONS - {".egrid", ".fegrid", ".grdecl"} or any(token in text for token in ("restart", "summary", "unsmry", "smspec", "resultado", "resultados", "模拟结果"))),
        ("reservoir", "pvt", any(token in text for token in (" pvt", "pvto", "pvtg", "pvdo", "pvdg", "eos", "equation of state", "black oil", "流体模型", "组分模型"))),
        ("reservoir", "scal", any(token in text for token in ("scal", "swof", "sgof", "swfn", "sgfn", "relative perm", "relperm", "capillary", "pcow", "pcog", "相渗", "毛管压力", "岩石压缩"))),
        ("reservoir", "history_match", any(token in text for token in ("history match", "historymatch", "historical match", "ajuste historico", "history matching", "历史拟合", "拟合误差"))),
        ("reservoir", "forecast_strategy", any(token in text for token in ("forecast", "prediction", "scenario", "strategy", "pronostico", "prediccion", "development plan", "生产预测", "预测方案", "开发策略"))),
        ("reservoir", "uncertainty_case", any(token in text for token in ("uncertainty", "sensitivity", "ensemble", "probabilistic", "optimization", "multiple realizations", "不确定性", "敏感性", "集合模型", "优化方案"))),
        ("reservoir", "simulation_model", suffix in {".egrid", ".fegrid", ".grdecl"} or any(token in text for token in ("eclipse", "intersect", "tnavigator", "t navigator", "cmg", "corner point", "simulation model", "modelo dinamico", "数值模型", "数模模型"))),
        ("reservoir", "well_completion", model_context and any(token in text for token in ("welspec", "compdat", "schedule", "well control", "completion", "perforation", "井控", "完井", "射孔", "生产井", "注水井", "注气井"))),
        ("reservoir", "initialization", model_context and any(token in text for token in ("equil", "initialization", "initial pressure", "satnum", "pvtnum", "fipnum", "contact", "初始化", "压力场", "饱和度场", "流体接触"))),
        ("geological", "reserve_abundance_map", any(token in text for token in ("reserve abundance", "reserves abundance", "abundancia de reservas", "储量丰度", "资源丰度"))),
        ("geological", "petrophysical_map", any(token in text for token in ("porosity map", "permeability map", "saturation map", "ntg map", "mapa porosidad", "mapa permeabilidad", "孔隙度平面", "渗透率平面", "饱和度平面", "孔渗饱平面", "净毛比平面"))),
        ("geological", "reserve_case", any(token in text for token in ("reserve case", "reserves case", "volumetric case", "stoiip", "ooip", "grv", "hydrocarbon pore volume", "reservas", "储量 case", "储量计算", "体积法储量", "地质储量"))),
        ("geological", "sedimentary_model", any(token in text for token in ("facies model", "facies realization", "depositional model", "sedimentary model", "rock type model", "litofacies", "facies", "沉积模型", "沉积相模型", "岩相模型", "岩石类型模型")) and (model_context or "facies" in text or "相模型" in text)),
        ("geological", "property_model", any(token in text for token in ("property model", "petrophysical model", "porosity model", "permeability model", "saturation model", "ntg model", "vsh model", "modelo de propiedades", "属性模型", "孔隙度模型", "渗透率模型", "饱和度模型", "净毛比模型"))),
        ("geological", "structural_model", model_context and any(token in text for token in ("structural", "structure", "framework", "pillar grid", "fault framework", "corner point", "grid skeleton", "构造模型", "构造格架", "断层框架", "角点网格", "网格骨架"))),
    ]
    for group, type_key, matched in rules:
        if matched:
            strength = "strong" if suffix in SIMULATION_EXTENSIONS or not model_context else "contextual"
            return {"group": group, "type_key": type_key, "confidence": strength}
    return None


def model_inventory(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    classified = []
    for item in items:
        match = classify_model_item(item.get("relative_path", ""), item.get("filename", ""), item.get("extension", ""))
        if match:
            classified.append({**item, **match})

    type_rows = []
    for group_key, group in MODEL_TAXONOMY.items():
        for type_key, label, description in group["types"]:
            rows = [row for row in classified if row["group"] == group_key and row["type_key"] == type_key]
            extensions = Counter(row.get("extension") or "[none]" for row in rows)
            type_rows.append({
                "group": group_key, "group_label": group["label"], "type_key": type_key,
                "label": label, "description": description, "files": len(rows),
                "bytes": sum(int(row.get("bytes") or 0) for row in rows),
                "representatives": sum(bool(row.get("representative")) for row in rows),
                "extensions": [{"extension": key, "files": value} for key, value in extensions.most_common(6)],
                "status": "discovered" if rows else "missing",
            })
    groups = []
    for group_key, group in MODEL_TAXONOMY.items():
        rows = [row for row in type_rows if row["group"] == group_key]
        groups.append({
            "key": group_key, "label": group["label"], "files": sum(row["files"] for row in rows),
            "bytes": sum(row["bytes"] for row in rows), "discovered_types": sum(row["files"] > 0 for row in rows),
            "total_types": len(rows), "types": rows,
        })
    return {
        "summary": {
            "files": len(classified), "bytes": sum(int(row.get("bytes") or 0) for row in classified),
            "geological_files": sum(row["group"] == "geological" for row in classified),
            "reservoir_files": sum(row["group"] == "reservoir" for row in classified),
            "discovered_types": sum(row["files"] > 0 for row in type_rows), "total_types": len(type_rows),
        },
        "groups": groups,
        "items": classified[:500],
        "method": "仅依据完整目录索引中的路径、文件名和扩展名分类；不读取大型模型体数值。识别结果是待人工复核的初筛，不代表模型可运行。",
    }
