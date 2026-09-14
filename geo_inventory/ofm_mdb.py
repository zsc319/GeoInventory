from __future__ import annotations

import csv
import io
import json
import math
import re
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .db import Database, utcnow


OFM_TABLE_CATEGORIES = {
    "XY": ("well_coordinates", "井位与井基础信息"),
    "PRD": ("production_history", "生产历史"),
    "INJ": ("injection_history", "注入历史"),
    "PRS": ("pressure_history", "压力测试"),
    "SORTCATEGORY": ("well_classification", "井分类"),
    "NOTES": ("notes", "备注"),
}

CATEGORY_LABELS = {
    "well_coordinates": "井位与井基础信息",
    "production_history": "生产历史",
    "injection_history": "注入历史",
    "pressure_history": "压力测试",
    "completion": "完井与井筒",
    "deviation": "井斜轨迹",
    "marker": "分层标志",
    "log": "测井对象",
    "pvt": "PVT",
    "forecast": "递减分析与预测",
    "well_classification": "井分类",
    "notes": "备注",
    "production_summary": "生产累计与汇总",
    "well_events": "井状态与干预事件",
    "well_testing": "井测试与举升监测",
    "fluid_analysis": "流体与化验分析",
    "production_operations": "生产控制与现场操作",
    "configuration": "OFM 配置与元数据",
}


# OFM Access uses technical table/field names that are difficult to interpret
# without reservoir-engineering context.  Keep the explanations in the backend
# so the UI, keyword catalogue and exported manifest share one definition.
TABLE_EXPLANATIONS = {
    "MAESTRA": {
        "purpose": "井主数据与井位总表（西语 OFM 工区）",
        "stores": "保存 TERMINACION/WELLBORE、井口与目标坐标、KB、总井深、油田、层系、井状态、井型等。",
        "note": "该工区以 MAESTRA 替代标准 XY 表；它是井位、井身份与基础分类的主来源。",
    },
    "PROD": {
        "purpose": "逐井逐月生产历史表（西语 OFM 工区）",
        "stores": "保存 TERMINACION、日期、生产天数 DIAS、ACEITE、AGUA、GAS 与 INY 等动态字段。",
        "note": "该工区以 PROD 替代标准 PRD 表。ACEITE/AGUA 按日率解读并结合 DIAS 计算月量；原始数值始终保留在导出与元数据中。",
    },
    "CUMULATIVE": {
        "purpose": "井累计产量参考表",
        "stores": "保存 Wellbore 与 NP 等累计指标。",
        "note": "它用于复核生产历史累产；标准汇总优先按逐月 PROD 重算，以保证计算过程可追溯。",
    },
    "EVENTOS_INTERVENCION": {
        "purpose": "井干预/修井事件表",
        "stores": "保存井名、干预日期、干预编号和泵型/措施信息。",
        "note": "会作为单井生产历程事件导出，不等同于生产月记录。",
    },
    "HISTORICO_CAMBIO_DE_ESTADO": {
        "purpose": "井状态变更历史表",
        "stores": "保存由何种状态变更到何种状态、日期、原因或备注。",
        "note": "用于梳理开关井及状态转换，不会被误当作月产量。",
    },
    "ECOMETROS": {
        "purpose": "回声仪与举升工况测试",
        "stores": "保存井筒/油套压、液面、泵挂深度、转速或产量等人工举升监测数据。",
        "note": "部分记录含 Pws/Pwf 压力字段，可形成压力历史；其余字段保留在原表中，用于分析抽油机或液面工况。",
    },
    "HISTORICO_ECOMETROS_XXX": {
        "purpose": "历史回声仪与举升监测",
        "stores": "保存油管/套管参数、泵深、流体液面、转速和井口压力等历史测试。",
        "note": "属于生产工程实测，不等同于月度生产量；可在原表预览和完整拆分包中导出。",
    },
    "MEDICIONESBIFASICAS": {
        "purpose": "两相计量 / 试井结果",
        "stores": "保存测试日期、液量、油量、含水或气量等单井计量结果。",
        "note": "通常用于校核月度 PROD 产量，不能在未确认单位和计量周期前直接替代生产历史。",
    },
    "CONDICIONDINAMICA": {
        "purpose": "动态流压与泵况测试",
        "stores": "保存 Pws、Pwf、泵挂深度和相关动态工况参数。",
        "note": "可作为压力变化证据；与月度生产表按井名和日期关联，而非把压力视作产量。",
    },
    "CONTROLES": {
        "purpose": "现场生产控制 / 计量记录",
        "stores": "保存生产毛量、净产量、含水、计量时间、罐量或有效性标志。",
        "note": "它可能是日/班次控制记录，时间粒度与 PROD 不同，完整保留为原表供复核。",
    },
    "HISTORICO_MUESTRASAYS": {
        "purpose": "油水样与沉积物历史",
        "stores": "保存含水、沉积物、温度和备注等化验/取样记录。",
        "note": "可辅助解释产出液性质；不是标准逐月产量表。",
    },
    "VISCOSIDAD_CP": {
        "purpose": "原油黏度化验历史",
        "stores": "保存不同温度下的黏度、分析日期和井名。",
        "note": "用于流体性质评价；应保留温度条件，不能把不同温度黏度直接横向比较。",
    },
    "SALINIDAD_PPM": {
        "purpose": "产出水盐度化验历史",
        "stores": "保存盐度、氯离子、温度、实验室和分析日期。",
        "note": "可用于识别水性变化和注采响应，原始单位以表字段为准。",
    },
    "API": {
        "purpose": "原油 API 度与密度化验",
        "stores": "保存 API、相对密度、取样/分析日期。",
        "note": "属于流体性质记录；API 度不是接口编号。",
    },
    "H2S_CAMPO": {
        "purpose": "硫化氢监测",
        "stores": "保存 H2S 浓度、取样日期和井名。",
        "note": "属于安全与流体组成信息，浓度单位应随原表导出一并核对。",
    },
    "CROMAT": {
        "purpose": "天然气色谱组成",
        "stores": "保存 O2、N2、H2S、CO2、C1–C5 等气体组分及日期。",
        "note": "用于气体组成和 PVT/工程分析，不等同于天然气月产量。",
    },
    "XY": {
        "purpose": "井位与井基础信息主表",
        "stores": "通常保存井名、井口/底部坐标、KB、总井深、完井日期及显示标记。",
        "note": "这是建立井空间位置与井身份的主要来源；本表为空时无法从该 MDB 导出井位坐标。",
    },
    "PRD": {
        "purpose": "逐井逐月生产历史表",
        "stores": "通常保存日期、开井小时、月产油、月产水和月产气等观测记录。",
        "note": "初期日产、生产月份、累产及生产曲线主要由本表计算；本表为空时对应标准汇总只有表头。",
    },
    "INJ": {
        "purpose": "逐井逐月注入历史表",
        "stores": "保存注水、注气、注汽、溶剂或其他注入量及日期。",
        "note": "用于识别注入井工作历史；本表为空不等于油井不存在。",
    },
    "PRS": {
        "purpose": "井压力测试/压力历史表",
        "stores": "保存井底流压、井口压力、测试日期及相关压力字段。",
        "note": "用于绘制压力变化；它与产量历史是两类证据。",
    },
    "SORTCATEGORY": {
        "purpose": "井分类与显示分组表",
        "stores": "保存井型、油田、区块、层系、状态或其他 OFM 分类标签。",
        "note": "属于分类元数据，不是生产数值本身。",
    },
    "NOTES": {
        "purpose": "OFM 备注表",
        "stores": "保存与井或对象关联的文本备注。",
        "note": "备注可辅助解释，但不能单独作为产量或井位的硬数据。",
    },
    "OFM_DATA_DCA_ANALYTICAL": {
        "purpose": "递减曲线分析（DCA）参数明细表",
        "stores": "按 DCA_ID 以 Property / Value 键值形式保存拟合方法、递减参数、预测设置或分析选项。",
        "note": "它是分析模型参数，不是逐月实测生产记录；需要与 DCA Case、Entity、Schedule 等表联合解释。",
    },
    "OFM_DATA_DCA_CASE": {
        "purpose": "递减分析方案（Case）目录",
        "stores": "保存每个 DCA 方案的名称、标识及说明。",
        "note": "用于区分不同预测/拟合版本，不等同于一口井或一条生产记录。",
    },
    "OFM_DATA_DCA_CONFIGURATION": {
        "purpose": "递减分析全局配置",
        "stores": "保存 DCA 工具的算法、单位、显示及默认参数设置。",
        "note": "属于软件配置，可帮助复现分析环境。",
    },
    "OFM_DATA_DCA_ENTITY": {
        "purpose": "递减分析对象关联表",
        "stores": "把 DCA_ID、分析方案与井/组等 OFM 对象关联起来。",
        "note": "用于回答某套 DCA 参数属于哪个对象。",
    },
    "OFM_DATA_DCA_FORECAST": {
        "purpose": "递减分析预测序列",
        "stores": "保存 DCA 计算形成的未来油、气、水产量或产率序列。",
        "note": "这是模型预测结果，必须与历史实测产量分开使用。",
    },
    "OFM_DATA_DCA_HISTPRODUCTION": {
        "purpose": "DCA 拟合所用历史样点",
        "stores": "保存进入递减拟合的数据点、日期及产量/产率。",
        "note": "可能是筛选或整理后的拟合输入，不一定等同于完整 PRD 原始历史。",
    },
    "OFM_DATA_DCA_HISTREGRESSION": {
        "purpose": "DCA 历史拟合结果",
        "stores": "保存回归模型、拟合区间、递减率及其他拟合参数。",
        "note": "用于评价历史拟合，不是原始生产观测。",
    },
    "OFM_DATA_DCA_LIMITS": {
        "purpose": "DCA 预测约束与经济极限",
        "stores": "保存最小产量、截止时间、储量或其他预测终止条件。",
        "note": "这些条件会影响预测期限和最终可采量。",
    },
    "OFM_DATA_DCA_RATIO": {
        "purpose": "DCA 比值模型设置",
        "stores": "保存含水、油气比、气油比等比值关系的历史/拟合参数。",
        "note": "比值参数用于耦合油、气、水预测，不是独立实测生产表。",
    },
    "OFM_DATA_DCA_RATIOFORECAST": {
        "purpose": "DCA 比值预测结果",
        "stores": "保存预测期含水或油气比等派生比值序列。",
        "note": "属于计算结果，使用时应标明对应方案。",
    },
    "OFM_DATA_DCA_RESULTS": {
        "purpose": "DCA 汇总结果",
        "stores": "通常保存预测累产、剩余可采量、递减参数和经济极限日期。",
        "note": "属于方案结果，应与 Case 和 Entity 一起确定版本及对象。",
    },
    "OFM_DATA_DCA_SCHEDULE": {
        "purpose": "DCA 预测时段与计划设置",
        "stores": "保存预测起止、分段条件、约束或计划节点。",
        "note": "用于控制预测过程，不是实际开关井记录。",
    },
    "OFM_DATA_PVT": {
        "purpose": "流体 PVT 属性与相关式参数表",
        "stores": "按 Entity / Fluid / Property / Value 保存油、气、水的体积系数、黏度、密度或相关式设置。",
        "note": "用于流体性质和油藏计算，不是井的逐月生产历史。",
    },
    "OFM_DATA_DEVIATION": {
        "purpose": "井斜轨迹数据表",
        "stores": "通常保存井筒测点的 MD、井斜角、方位角或空间坐标。",
        "note": "可用于 MD 与 TVD/SSTVD 转换及射孔段空间投影。",
    },
    "OFM_DATA_MARKER": {
        "purpose": "井上地层标志/分层表",
        "stores": "保存井名、层位名称和分层深度。",
        "note": "相当于 OFM 中的井上层位证据，可与 Well Top 对照。",
    },
    "OFM_DATA_FAULT": {
        "purpose": "井上断层拾取表",
        "stores": "保存井筒穿断层的位置、深度或断层名称。",
        "note": "属于井上断层证据，不等同于地震解释断层面。",
    },
    "OFM_DATA_LOG": {
        "purpose": "OFM 测井对象目录",
        "stores": "保存 OFM 内登记的测井曲线对象、深度和值或引用信息。",
        "note": "是否含完整曲线样点取决于具体 OFM 工区结构。",
    },
    "OFM_DATA_PIPESIM": {
        "purpose": "PIPESIM/井筒流动关联数据",
        "stores": "保存与井筒流动模型、节点或计算结果有关的参数。",
        "note": "属于工程模型数据，不应直接解释为地质属性。",
    },
    "OFM_DATA_PATTERN": {
        "purpose": "井组/注采井网关系表",
        "stores": "保存注采井组、井网或井间关联。",
        "note": "可用于注采对应分析。",
    },
}


COLUMN_EXPLANATIONS = {
    "DCAID": "递减分析对象/方案的内部关联编号，用来连接多张 DCA 表。",
    "PROPERTY": "参数或属性名称；具体含义需结合所在表和 Value 解读。",
    "VALUE": "参数值；单位和数据性质由 Property 及所在表决定。",
    "ENTITY": "OFM 对象名，可能是一口井、井组、区块或其他分析对象。",
    "FLUID": "流体相别，例如 Oil、Gas 或 Water。",
    "WELL": "井名或井标识。",
    "WELLBORE": "井筒/井名标识；一口井可能对应一个或多个井筒。",
    "TERMINACION": "完井/井筒标识；本 OFM 工区中作为主井名键使用。",
    "DATE": "该条记录对应的日期或月份。",
    "FECHA": "日期字段（西语 Fecha）；具体是日、月末或测试日需结合所在表确认。",
    "DIAS": "统计期内生产天数（西语 Días）。本工区 PROD 表用于把日率推算为月量。",
    "ACEITE": "原油日率/油量（西语 Aceite）。本工区 PROD 表按日率解释，并与 DIAS 相乘得到月油量。",
    "AGUA": "产水日率/水量（西语 Agua）。本工区 PROD 表按日率解释，并与 DIAS 相乘得到月水量。",
    "INY": "注入量或注入标志（西语 Inyección 的缩写）；当前按原表 INY 字段保留为其他注入量，单位需核对 OFM 设置。",
    "XCOOR": "井目标点 / 井筒坐标 X（Easting）。",
    "YCOOR": "井目标点 / 井筒坐标 Y（Northing）。",
    "XCOOROBJ": "井口或对象参考坐标 X；与 XCOOR 的具体差异以原工区井位定义为准。",
    "YCOOROBJ": "井口或对象参考坐标 Y；与 YCOOR 的具体差异以原工区井位定义为准。",
    "CAMPO": "油田名称（西语 Campo）。",
    "YACIMIENTO": "油藏/层系名称（西语 Yacimiento）。",
    "ESTATUS": "井当前状态（西语 Estatus），例如开井、关井或无生产可能。",
    "ARQUPOZO": "井型/井眼结构分类（西语 Arquitectura de pozo）。",
    "HOURS": "统计期内开井小时数；除以 24 可估算开井天数。",
    "OIL": "油量；在 PRD 中通常是统计期月产油量，具体单位应核对工区设置。",
    "WATER": "水量；在 PRD 中通常是统计期月产水量，具体单位应核对工区设置。",
    "GAS": "气量；具体体积单位应核对 OFM 工区单位配置。",
    "COND": "凝析油/凝析液量，具体单位应核对工区设置。",
    "XCOORD": "井坐标 X/Easting。使用前应确认坐标参考系和单位。",
    "YCOORD": "井坐标 Y/Northing。使用前应确认坐标参考系和单位。",
    "SURFX": "井口地表坐标 X。",
    "SURFY": "井口地表坐标 Y。",
    "KBELEV": "方补心（KB）高程，常用于深度基准换算。",
    "TDEPTH": "总井深；需结合字段定义确认是 MD 还是其他深度基准。",
    "CDATE": "完井或投产相关日期，具体业务含义需结合工区定义。",
    "INTEREST": "OFM 显示/关注标记；枚举含义由原工区配置决定，软件保留原值。",
    "BHFP": "井底流压（Bottom-hole flowing pressure）。",
    "WHPRESS": "井口压力。",
    "TOPDEPTH": "井段顶深，通常为 MD。",
    "BOTTOMDEPTH": "井段底深，通常为 MD。",
    "GINJ": "注气量。",
    "WINJ": "注水量。",
    "STINJ": "注汽量。",
    "MINJ": "其他/混合注入量，需核对原工区字段定义。",
    "SOLI": "溶剂注入量。",
    "AIRINJ": "空气注入量。",
}


def table_explanation(table_name: str, row_count: int | None = None) -> dict[str, str]:
    upper = str(table_name or "").upper()
    help_row = TABLE_EXPLANATIONS.get(upper)
    if help_row is None:
        if "ANNOTATION" in upper or "ANNO" in upper:
            help_row = {"purpose": "OFM 图形注记/标注配置", "stores": "保存图形界面中的文字、标注位置或显示样式。", "note": "属于展示元数据，不是井生产记录。"}
        elif "WBD" in upper or "EQUIPMENT" in upper:
            help_row = {"purpose": "井筒结构与设备数据", "stores": "保存套管、油管、射孔或其他井筒示意图对象。", "note": "可用于识别完井与射孔，但需依据设备类型字段判断。"}
        else:
            category, label = _category(table_name)
            fallback = {
                "configuration": "保存 OFM 软件配置、界面状态或内部元数据。",
                "forecast": "保存递减分析或预测相关的参数、关系或结果。",
                "completion": "保存完井、井筒或设备相关记录。",
                "marker": "保存井上层位或构造解释相关记录。",
                "log": "保存测井对象或曲线相关记录。",
                "pvt": "保存流体性质或工程模型相关参数。",
            }.get(category, f"保存{label}相关记录。")
            help_row = {"purpose": label, "stores": fallback, "note": "建议结合字段、样例值和 OFM 原工区定义进一步确认。"}
    result = dict(help_row)
    if row_count is None:
        result["availability"] = "尚未统计记录数。"
    elif row_count:
        result["availability"] = f"当前挂载源中有 {int(row_count):,} 条记录。"
    else:
        result["availability"] = "当前挂载源中为 0 行；导出仅有表头属于源表无记录，并非导出程序丢失数据。"
    return result


def column_explanation(table_name: str, column_name: str) -> str:
    key = _normalize(column_name)
    if key in COLUMN_EXPLANATIONS:
        return COLUMN_EXPLANATIONS[key]
    upper_table = str(table_name or "").upper()
    if key in {"ID", "CASEID", "ENTITYID", "CONFIGURATIONID"}:
        return "OFM 内部关联编号，用来连接相关表；通常不具备直接地质含义。"
    if "NAME" in key:
        return "对象、方案或分类的名称。"
    if "UNIT" in key:
        return "数值单位或单位体系标识。"
    if "START" in key or "BEGIN" in key:
        return "记录、分析或预测区间的起始位置/日期。"
    if "END" in key or "STOP" in key:
        return "记录、分析或预测区间的结束位置/日期。"
    if "RATE" in key:
        return "产率、注入率或递减率；具体单位与含义需结合表名及 OFM 单位设置。"
    if "DEPTH" in key:
        return "深度字段；使用前应确认 MD/TVD/SSTVD 基准和长度单位。"
    if "STATUS" in key:
        return "对象或记录的状态标记。"
    if "COMMENT" in key or "NOTE" in key or "DESCRIPTION" in key:
        return "文本说明或备注。"
    return f"{table_explanation(table_name)['purpose']}中的原始字段；请结合样例值与 OFM 工区字段定义确认。"


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", text)


def _well_key(value: Any) -> str:
    return _normalize(value)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return f"<binary {len(data)} bytes>"
    return str(value)


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]" ) + "]"


def _category(table_name: str) -> tuple[str, str]:
    upper = table_name.upper()
    if upper in OFM_TABLE_CATEGORIES:
        return OFM_TABLE_CATEGORIES[upper]
    normalized = _normalize(upper)
    if normalized in {"MAESTRA", "MASTERWELL", "WELLS"}:
        key = "well_coordinates"
    elif normalized in {"PROD", "PRODUCTION", "PRODUCCION"}:
        key = "production_history"
    elif normalized in {"CUMULATIVE", "ACUMULADO"}:
        key = "production_summary"
    elif "INTERVENCION" in normalized or "CAMBIODEESTADO" in normalized:
        key = "well_events"
    elif "ECOMETRO" in normalized or normalized.startswith("MEDICION") or "CONDICIONDINAMICA" in normalized or normalized in {"PFC", "PFF", "REPPFC", "REPPFF"}:
        key = "well_testing"
    elif any(token in normalized for token in ("VISCOSIDAD", "SALINIDAD", "H2SCAMPO", "CROMAT", "STIFFDAVIS")) or normalized == "API":
        key = "fluid_analysis"
    elif normalized in {"CONTROLES", "GESTIONACCIONES", "HISTORICOMUESTRASAYS", "DECLINACIONTEMPORAL"}:
        key = "production_operations"
    elif "WBD" in upper or "EQUIPMENT" in upper:
        key = "completion"
    elif "DEVIATION" in upper:
        key = "deviation"
    elif "MARKER" in upper or "FAULT" in upper:
        key = "marker"
    elif "LOG" in upper:
        key = "log"
    elif "PVT" in upper or "PIPESIM" in upper:
        key = "pvt"
    elif "DCA" in upper or "FORECAST" in upper:
        key = "forecast"
    else:
        key = "configuration"
    return key, CATEGORY_LABELS[key]


def access_driver() -> str:
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("读取 OFM MDB 需要 pyodbc；当前 Python 环境未安装") from exc
    drivers = [name for name in pyodbc.drivers() if "access driver" in name.lower()]
    if not drivers:
        raise RuntimeError("未检测到 Microsoft Access Database Engine（MDB/ACCDB ODBC 驱动）")
    return drivers[-1]


def connect_readonly(path: str | Path):
    import pyodbc

    resolved = Path(path).expanduser().resolve()
    if resolved.suffix.lower() not in {".mdb", ".accdb"}:
        raise ValueError("OFM 数据库必须是 .mdb 或 .accdb 文件")
    if not resolved.is_file():
        raise FileNotFoundError(f"OFM 数据库不存在：{resolved}")
    driver = access_driver()
    connection = pyodbc.connect(
        f"DRIVER={{{driver}}};DBQ={resolved};READONLY=TRUE;",
        autocommit=True,
        timeout=20,
    )
    return connection, driver


def _table_names(connection) -> list[str]:
    return sorted(
        {str(row.table_name) for row in connection.cursor().tables(tableType="TABLE")},
        key=str.upper,
    )


def _columns(connection, table_name: str) -> list[dict[str, Any]]:
    return [
        {
            "name": str(row.column_name),
            "type": str(row.type_name or ""),
            "size": int(row.column_size or 0),
            "nullable": bool(row.nullable),
        }
        for row in connection.cursor().columns(table=table_name)
    ]


def _row_dicts(connection, table_name: str, limit: int | None = None, offset: int = 0) -> Iterable[dict[str, Any]]:
    query = f"SELECT * FROM {_quote(table_name)}"
    cursor = connection.cursor().execute(query)
    names = [str(item[0]) for item in cursor.description or []]
    skipped = 0
    emitted = 0
    for raw in cursor:
        if skipped < offset:
            skipped += 1
            continue
        if limit is not None and emitted >= limit:
            break
        yield {name: _json_value(value) for name, value in zip(names, raw)}
        emitted += 1


def inspect_mdb(path: str | Path, sample_limit: int = 3) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    connection, driver = connect_readonly(resolved)
    try:
        tables = []
        for name in _table_names(connection):
            count = int(connection.cursor().execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0])
            category, category_label = _category(name)
            tables.append({
                "name": name,
                "row_count": count,
                "category": category,
                "category_label": category_label,
                "columns": _columns(connection, name),
                "sample": list(_row_dicts(connection, name, sample_limit)) if count else [],
            })
        stat = resolved.stat()
        return {
            "path": str(resolved),
            "filename": resolved.name,
            "file_size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "driver": driver,
            "table_count": len(tables),
            "nonempty_table_count": sum(1 for row in tables if row["row_count"]),
            "total_rows": sum(row["row_count"] for row in tables),
            "tables": tables,
        }
    finally:
        connection.close()


def _case_row(row: dict[str, Any]) -> dict[str, Any]:
    return {_normalize(key): value for key, value in row.items()}


def _classification_rows(connection, names: set[str]) -> dict[str, dict[str, Any]]:
    table = next((name for name in names if name.upper() == "SORTCATEGORY"), None)
    if not table:
        return {}
    result = {}
    for original in _row_dicts(connection, table):
        row = _case_row(original)
        key = _well_key(row.get("WELL"))
        if key:
            result[key] = row
    return result


# OFM projects are often localized or customized.  The standard tables
# (XY/PRD/INJ/PRS) may be replaced by Spanish master/production tables, so
# business extraction is driven by table names *and* column signatures.
OFM_FIELD_ALIASES = {
    "well": ("WELL", "WELLBORE", "TERMINACION", "POZO", "NOMBREPOZO"),
    "alias": ("ALIAS", "WELLALIAS"),
    "x": ("XCOORD", "XCOOR", "X", "EASTING"),
    "y": ("YCOORD", "YCOOR", "Y", "NORTHING"),
    "surface_x": ("SURFX", "XCOOROBJ", "XOBJ", "SURFACEX"),
    "surface_y": ("SURFY", "YCOOROBJ", "YOBJ", "SURFACEY"),
    "kb": ("KBELEV", "KB", "ELEVATION"),
    "total_depth": ("TDEPTH", "PT", "TOTALDEPTH", "TD"),
    "date": ("DATE", "FECHA", "CDATE", "EVENTDATE"),
    "days": ("HOURS", "DIAS", "DAYSON", "DAYS"),
    "oil": ("OIL", "ACEITE", "QO", "QOBPD"),
    "water": ("WATER", "AGUA", "QW", "QWBPD"),
    "gas": ("GAS", "QG", "GASPRODUCTION"),
    "injection": ("GINJ", "WINJ", "INY", "INYECCION", "INJECTION"),
    "pressure": ("BHFP", "WHPRESS", "PWSPWF", "PRESSURE", "PRESION"),
    "md": ("MD", "MDDEPTH", "MEASUREDDEPTH"),
    "tvd": ("TVD",),
    "x_offset": ("XDELT", "XOFFSET", "EASTINGOFFSET"),
    "y_offset": ("YDELT", "YOFFSET", "NORTHINGOFFSET"),
    "marker": ("NAME", "MARKER", "HORIZON", "MARCADOR"),
    "depth": ("DEPTH", "MD", "PROFUNDIDAD"),
    "picker": ("PICKER", "INTERPRETER", "USUARIO"),
    "status": ("STATUS", "ESTATUS", "CAMBIOA", "STATE"),
    "field": ("FIELD", "CAMPO"),
    "zone": ("ZONE", "YACIMIENTO", "RESERVOIR"),
    "well_type": ("TYPE", "ARQUPOZO", "WELLTYPE"),
}


def _field(row: dict[str, Any], field_name: str) -> Any:
    for alias in OFM_FIELD_ALIASES[field_name]:
        value = row.get(_normalize(alias))
        if _text(value) is not None:
            return value
    return None


def _table_by_signature(
    names: set[str],
    columns: dict[str, set[str]],
    aliases: tuple[str, ...],
    required: tuple[str, ...],
) -> str | None:
    by_normalized = {_normalize(name): name for name in names}
    for alias in aliases:
        hit = by_normalized.get(_normalize(alias))
        if hit:
            return hit
    candidates: list[tuple[int, str]] = []
    for name in names:
        available = columns.get(name, set())
        score = sum(bool(available & {_normalize(value) for value in OFM_FIELD_ALIASES[field]}) for field in required)
        if score == len(required):
            candidates.append((score, name))
    return sorted(candidates, key=lambda item: item[1].upper())[0][1] if candidates else None


def _profile_row(
    kind: str,
    table: str | None,
    row_count: int,
    fields: dict[str, str],
    note: str,
    mapped_rows: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "table": table,
        "row_count": int(row_count or 0),
        "fields": fields,
        "note": note,
        "available": bool(table and row_count),
        # ``row_count`` is the raw source-table count.  ``mapped_rows`` is
        # populated after value validation, so a user can distinguish a table
        # that exists from the subset that has usable key/date/value fields.
        "mapped_rows": mapped_rows,
    }


def _matched_columns(available: set[str], field_names: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field_name in field_names:
        matched = next(
            (alias for alias in OFM_FIELD_ALIASES[field_name] if _normalize(alias) in available),
            None,
        )
        if matched:
            result[field_name] = matched
    return result


def _profile_group(
    kind: str,
    tables: list[str],
    row_counts: dict[str, int],
    note: str,
) -> dict[str, Any]:
    """Expose a compact semantic index for customized OFM workspaces.

    A local OFM project may put equally useful engineering measurements in
    several Spanish-named tables.  They remain independently browseable and
    exportable, while this group tells a geologist what the set represents.
    """
    available = [name for name in tables if row_counts.get(name, 0)]
    return _profile_row(
        kind,
        " / ".join(available) if available else None,
        sum(row_counts.get(name, 0) for name in available),
        {},
        note,
    )


def _tables_with_normalized_names(names: set[str], *aliases: str) -> list[str]:
    wanted = {_normalize(alias) for alias in aliases}
    return sorted((name for name in names if _normalize(name) in wanted), key=str.upper)


def import_mdb(
    sqlite_conn,
    path: str | Path,
    source_id: int,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    metadata = inspect_mdb(path)
    if progress_callback:
        progress_callback(0.18, "已读取 MDB 表结构")
    cursor = sqlite_conn.execute(
        """INSERT INTO ofm_sources(
           source_id,file_path,file_size,modified_at,driver,table_count,nonempty_table_count,total_rows,mounted_at,warning
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            source_id, metadata["path"], metadata["file_size"], metadata["modified_at"], metadata["driver"],
            metadata["table_count"], metadata["nonempty_table_count"], metadata["total_rows"], utcnow(), None,
        ),
    )
    ofm_source_id = int(cursor.lastrowid)
    for table in metadata["tables"]:
        sqlite_conn.execute(
            """INSERT INTO ofm_table_catalog(
               ofm_source_id,table_name,category,row_count,columns_json,sample_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                ofm_source_id, table["name"], table["category"], table["row_count"],
                Database.json(table["columns"]), Database.json(table["sample"]),
            ),
        )

    connection, _ = connect_readonly(path)
    names = set(_table_names(connection))
    columns = {name: {_normalize(item["name"]) for item in _columns(connection, name)} for name in names}
    row_counts = {item["name"]: int(item["row_count"] or 0) for item in metadata["tables"]}
    classifications = _classification_rows(connection, names)
    mapped = {
        "wells": 0, "production": 0, "pressure": 0, "injection": 0,
        "perforations": 0, "events": 0, "deviation": 0, "markers": 0,
    }
    profile: list[dict[str, Any]] = []
    well_context: dict[str, dict[str, Any]] = {}
    try:
        master_name = _table_by_signature(names, columns, ("XY", "MAESTRA", "MASTER_WELL"), ("well", "x", "y"))
        prod_name = _table_by_signature(names, columns, ("PRD", "PROD", "PRODUCTION"), ("well", "date", "oil", "water"))
        prs_name = _table_by_signature(names, columns, ("PRS", "PRESSURE", "PRESION"), ("well", "date", "pressure"))
        inj_name = _table_by_signature(names, columns, ("INJ", "INJECTION", "INYECCION"), ("well", "date", "injection"))
        deviation_name = _table_by_signature(names, columns, ("OFM_DATA_DEVIATION", "DEVIATION"), ("well", "md", "tvd"))
        marker_name = _table_by_signature(names, columns, ("OFM_DATA_MARKER", "MARKER"), ("well", "marker", "depth"))
        equipment_name = _table_by_signature(names, columns, ("OFM_DATA_WBD_EQUIPMENT", "WBD_EQUIPMENT"), ("well",))
        status_name = _table_by_signature(names, columns, ("HISTORICO_CAMBIO_DE_ESTADO", "STATUS_HISTORY"), ("well", "date", "status"))
        intervention_name = _table_by_signature(names, columns, ("EVENTOS_INTERVENCION", "INTERVENTION_EVENTS"), ("well", "date"))
        cumulative_name = _table_by_signature(names, columns, ("CUMULATIVE", "ACUMULADO"), ("well",))
        testing_tables = _tables_with_normalized_names(
            names, "ECOMETROS", "HISTORICO_ECOMETROS_XXX", "MEDICIONESBIFASICAS",
            "CONDICIONDINAMICA", "PFC", "PFF", "REP_PFC", "REP_PFF",
        )
        fluid_tables = _tables_with_normalized_names(
            names, "VISCOSIDAD_CP", "SALINIDAD_PPM", "API", "H2S_CAMPO", "CROMAT", "ANALISIS_STIFF_DAVIS",
        )
        operations_tables = _tables_with_normalized_names(
            names, "CONTROLES", "HISTORICO_MUESTRASAYS", "GESTION_ACCIONES", "DECLINACION_TEMPORAL",
        )
        dca_tables = sorted((name for name in names if _normalize(name).startswith("OFMDATADCA")), key=str.upper)
        pvt_tables = _tables_with_normalized_names(names, "OFM_DATA_PVT")

        profile.extend([
            _profile_row("井位与井主数据", master_name, row_counts.get(master_name or "", 0), _matched_columns(columns.get(master_name or "", set()), ("well", "x", "y", "alias", "kb", "total_depth", "field", "zone", "status", "well_type")), "作为本工区井位、井名、井型与层系的主来源。"),
            _profile_row("逐月生产历史", prod_name, row_counts.get(prod_name or "", 0), _matched_columns(columns.get(prod_name or "", set()), ("well", "date", "days", "oil", "water", "gas", "injection")), "用于计算生产月份、初期产量、含水、累产及单井生产曲线。"),
            _profile_row("压力测试 / 流压", prs_name, row_counts.get(prs_name or "", 0), _matched_columns(columns.get(prs_name or "", set()), ("well", "date", "pressure")), "按井名、日期和有效压力值抽取；其他回声仪字段仍完整保留在 MDB 原表。"),
            _profile_row("注入历史", inj_name or prod_name, row_counts.get(inj_name or prod_name or "", 0), _matched_columns(columns.get(inj_name or prod_name or "", set()), ("well", "date", "injection")), "本工区从 PROD.INY 识别到注入字段；未假设它一定是注水或注气，原始字段名与数值随导出保留。"),
            _profile_row("井轨迹", deviation_name, row_counts.get(deviation_name or "", 0), _matched_columns(columns.get(deviation_name or "", set()), ("well", "md", "tvd", "x_offset", "y_offset")), "保留 MD、TVD 和相对井口的 X/Y 偏移，可用于与井口坐标合成平面轨迹。"),
            _profile_row("井上标志 / 分层", marker_name, row_counts.get(marker_name or "", 0), _matched_columns(columns.get(marker_name or "", set()), ("well", "marker", "depth", "date", "picker")), "作为 OFM 井上层位标志，与 Well Top 分层方案并列保留。"),
            _profile_group("井状态与干预事件", [name for name in (status_name, intervention_name) if name], row_counts, "状态变更与措施事件会进入单井生产历程，但不混入逐月产量。"),
            _profile_row("累计产量参考", cumulative_name, row_counts.get(cumulative_name or "", 0), _matched_columns(columns.get(cumulative_name or "", set()), ("well",)), "用于复核累产；标准汇总仍以逐月生产记录重算。"),
            _profile_group("举升、液面与试井", testing_tables, row_counts, "回声仪、两相计量、动态流压和泵况记录已分类，可逐表预览或在完整 ZIP 的 OFM 原表目录取得。"),
            _profile_group("流体性质与化验", fluid_tables, row_counts, "黏度、盐度、API、H2S 与色谱等有效化验表已识别；它们保留测试温度和日期，不混入生产量。"),
            _profile_group("现场控制与样品", operations_tables, row_counts, "生产控制、油水样/沉积物、措施与递减辅助记录按原始粒度保留，适合与 PROD 交叉复核。"),
            _profile_group("DCA 递减分析与预测", dca_tables, row_counts, "DCA Case、对象、历史拟合、预测和结果均可浏览/导出；这些是分析结果或配置，不能替代实测生产记录。"),
            _profile_group("PVT 流体模型", pvt_tables, row_counts, "PVT 的 Entity / Fluid / Property / Value 参数已识别，可用于后续流体模型检查。"),
        ])

        if master_name:
            values = []
            event_values = []
            for original in _row_dicts(connection, master_name):
                row = _case_row(original)
                well_name = _text(_field(row, "well"))
                if not well_name:
                    continue
                key = _well_key(well_name)
                classification = classifications.get(key, {})
                status = _text(_field(classification, "status") or _field(row, "status"))
                context = {
                    "status": status,
                    "field": _text(_field(classification, "field") or _field(row, "field")),
                    "zone": _text(_field(classification, "zone") or _field(row, "zone")),
                }
                well_context[key] = context
                completion = _text(_field(row, "date"))
                values.append((
                    source_id, key, well_name, _text(_field(row, "alias")), _number(_field(row, "x")),
                    _number(_field(row, "y")), _number(_field(row, "surface_x")), _number(_field(row, "surface_y")),
                    _number(_field(row, "kb")), _number(_field(row, "total_depth")), completion,
                    _text(_field(classification, "well_type") or _field(row, "well_type")), context["field"],
                    context["zone"], status, _text(row.get("INTEREST")), Database.json(original),
                ))
                if completion:
                    event_values.append((source_id, key, well_name, completion, "投产/完井日期", status, Database.json(original)))
            sqlite_conn.executemany(
                """INSERT INTO ofm_wells(source_id,well_key,well_name,alias,x,y,surface_x,surface_y,kb_elevation,total_depth,
                   completion_date,well_type,field_name,zone_name,status,interest,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values,
            )
            sqlite_conn.executemany(
                """INSERT INTO production_events(source_id,well_key,well_name,event_date,event_type,status,metadata_json)
                   VALUES(?,?,?,?,?,?,?)""", event_values,
            )
            mapped["wells"] += len(values)
            mapped["events"] += len(event_values)
        if progress_callback:
            progress_callback(0.34, "识别并映射 OFM 井位、井名与基础分类")

        if prod_name:
            spanish_rates = _normalize(prod_name) == "PROD" or "ACEITE" in columns.get(prod_name, set())
            production_values = []
            injection_values = []
            for original in _row_dicts(connection, prod_name):
                row = _case_row(original)
                well_name = _text(_field(row, "well"))
                if not well_name:
                    continue
                key = _well_key(well_name)
                raw_days = _number(_field(row, "days"))
                days = raw_days if spanish_rates else (raw_days / 24 if raw_days and raw_days > 0 else None)
                oil = _number(_field(row, "oil"))
                water = _number(_field(row, "water"))
                if spanish_rates:
                    oil_rate, water_rate = oil, water
                    monthly_oil = oil * days if oil is not None and days else None
                    monthly_water = water * days if water is not None and days else None
                    basis = "日率字段（ACEITE/AGUA）× DIAS 推算月量"
                else:
                    monthly_oil, monthly_water = oil, water
                    oil_rate = oil / days if oil is not None and days else None
                    water_rate = water / days if water is not None and days else None
                    basis = "月体积字段（OIL/WATER）÷ 开井天数计算日率"
                rates = [value for value in (oil_rate, water_rate) if value is not None]
                liquid_rate = sum(rates) if rates else None
                water_cut = water_rate / liquid_rate * 100 if water_rate is not None and liquid_rate else None
                production_values.append((
                    source_id, key, well_name, _text(_field(row, "date")), days, liquid_rate, oil_rate, water_rate,
                    water_cut, monthly_oil, monthly_water, well_context.get(key, {}).get("status"),
                    Database.json({"table": prod_name, "basis": basis, "raw": original}),
                ))
                injection = _number(_field(row, "injection"))
                if injection is not None:
                    injection_values.append((source_id, key, well_name, _text(_field(row, "date")), None, None, None, injection, None, None, Database.json({"table": prod_name, "raw": original})))
            sqlite_conn.executemany(
                """INSERT INTO production_monthly(source_id,well_key,well_name,production_month,days_on,liquid_rate,oil_rate,water_rate,
                   water_cut,monthly_oil,monthly_water,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", production_values,
            )
            sqlite_conn.executemany(
                """INSERT INTO ofm_injection_monthly(source_id,well_key,well_name,production_month,gas_injection,water_injection,steam_injection,
                   misc_injection,solvent_injection,air_injection,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", injection_values,
            )
            mapped["production"] += len(production_values)
            mapped["injection"] += len(injection_values)
        if progress_callback:
            progress_callback(0.58, "映射逐月生产历史并保留原始字段口径")

        if prs_name:
            values = []
            for original in _row_dicts(connection, prs_name):
                row = _case_row(original)
                well_name, pressure = _text(_field(row, "well")), _number(_field(row, "pressure"))
                if well_name and pressure is not None:
                    values.append((source_id, _well_key(well_name), well_name, _text(_field(row, "date")), pressure, "OFM 压力字段", Database.json({"table": prs_name, "raw": original})))
            sqlite_conn.executemany("""INSERT INTO production_monthly(source_id,well_key,well_name,production_month,pressure,pressure_type,metadata_json)
                VALUES(?,?,?,?,?,?,?)""", values)
            mapped["pressure"] += len(values)

        if inj_name and inj_name != prod_name:
            values = []
            for original in _row_dicts(connection, inj_name):
                row = _case_row(original)
                well_name = _text(_field(row, "well"))
                if well_name:
                    values.append((source_id, _well_key(well_name), well_name, _text(_field(row, "date")), _number(row.get("GINJ")), _number(row.get("WINJ")), _number(row.get("STINJ")), _number(row.get("MINJ") or _field(row, "injection")), _number(row.get("SOLI")), _number(row.get("AIRINJ")), Database.json({"table": inj_name, "raw": original})))
            sqlite_conn.executemany("""INSERT INTO ofm_injection_monthly(source_id,well_key,well_name,production_month,gas_injection,water_injection,steam_injection,
                misc_injection,solvent_injection,air_injection,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", values)
            mapped["injection"] += len(values)
        if progress_callback:
            progress_callback(0.68, "映射压力、注入及辅助动态数据")

        if deviation_name:
            values = []
            for original in _row_dicts(connection, deviation_name):
                row = _case_row(original)
                well_name, md = _text(_field(row, "well")), _number(_field(row, "md"))
                if well_name and md is not None:
                    values.append((source_id, _well_key(well_name), well_name, md, _number(_field(row, "tvd")), _number(_field(row, "x_offset")), _number(_field(row, "y_offset")), Database.json({"table": deviation_name, "raw": original})))
            sqlite_conn.executemany("""INSERT INTO ofm_deviation_stations(source_id,well_key,well_name,md,tvd,x_offset,y_offset,metadata_json)
                VALUES(?,?,?,?,?,?,?,?)""", values)
            mapped["deviation"] += len(values)

        if marker_name:
            values = []
            for original in _row_dicts(connection, marker_name):
                row = _case_row(original)
                well_name, marker = _text(_field(row, "well")), _text(_field(row, "marker"))
                if well_name and marker:
                    values.append((source_id, _well_key(well_name), well_name, marker, _number(_field(row, "depth")), _text(_field(row, "date")), _text(_field(row, "picker")), Database.json({"table": marker_name, "raw": original})))
            sqlite_conn.executemany("""INSERT INTO ofm_markers(source_id,well_key,well_name,marker_name,depth_md,marker_date,picker,metadata_json)
                VALUES(?,?,?,?,?,?,?,?)""", values)
            mapped["markers"] += len(values)

        if equipment_name:
            values = []
            for original in _row_dicts(connection, equipment_name):
                row = _case_row(original)
                well_name = _text(_field(row, "well"))
                top, base = _number(row.get("TOPDEPTH")), _number(row.get("BOTTOMDEPTH"))
                description = " ".join(filter(None, (_text(row.get("TYPE")), _text(row.get("KIND")), _text(row.get("OPTION")))))
                if well_name and top is not None and base is not None and re.search(r"perfor|completion|射孔|open\s*hole", description, re.I):
                    if base < top:
                        top, base = base, top
                    values.append((source_id, _well_key(well_name), well_name, top, base, description or "OFM 射孔/完井段", None, _text(_field(row, "date")), Database.json({"table": equipment_name, "raw": original})))
            sqlite_conn.executemany("""INSERT INTO production_intervals(source_id,well_key,well_name,top_md,base_md,interval_name,status,event_date,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?)""", values)
            mapped["perforations"] += len(values)

        for event_table, event_label in ((status_name, "井状态变更"), (intervention_name, "井干预 / 修井")):
            if not event_table:
                continue
            values = []
            for original in _row_dicts(connection, event_table):
                row = _case_row(original)
                well_name, event_date = _text(_field(row, "well")), _text(_field(row, "date"))
                if not well_name or not event_date:
                    continue
                before = _text(row.get("CAMBIODEESTADODE"))
                after = _text(row.get("CAMBIOA") or _field(row, "status"))
                detail = " → ".join(value for value in (before, after) if value)
                label = f"{event_label}{'：' + detail if detail else ''}"
                values.append((source_id, _well_key(well_name), well_name, event_date, label, after or well_context.get(_well_key(well_name), {}).get("status"), Database.json({"table": event_table, "raw": original})))
            sqlite_conn.executemany("""INSERT INTO production_events(source_id,well_key,well_name,event_date,event_type,status,metadata_json)
                VALUES(?,?,?,?,?,?,?)""", values)
            mapped["events"] += len(values)
        if progress_callback:
            progress_callback(0.88, "映射 OFM 井轨迹、层位标志、射孔与状态事件")
    finally:
        connection.close()

    mapped_profile_kinds = {
        "井位与井主数据": "wells",
        "逐月生产历史": "production",
        "压力测试 / 流压": "pressure",
        "注入历史": "injection",
        "井轨迹": "deviation",
        "井上标志 / 分层": "markers",
        "井状态与干预事件": "events",
    }
    for item in profile:
        mapped_key = mapped_profile_kinds.get(item["kind"])
        if mapped_key:
            item["mapped_rows"] = mapped[mapped_key]

    business_rows = sum(mapped.values())
    warning = None
    if business_rows == 0:
        warning = "MDB 已成功挂载，但未在标准或本地化业务表中识别到可映射的井位、生产、轨迹、层位标志、注入、压力或射孔记录；当前可查看原始表结构与配置/DCA/PVT 等数据。"
        sqlite_conn.execute("UPDATE ofm_sources SET warning=? WHERE id=?", (warning, ofm_source_id))
    return {
        "ofm_source_id": ofm_source_id,
        "table_count": metadata["table_count"],
        "nonempty_table_count": metadata["nonempty_table_count"],
        "total_rows": metadata["total_rows"],
        "mapped": mapped,
        "profile": profile,
        "warning": warning,
        "driver": metadata["driver"],
        "source_system": "OFM Access MDB（只读挂载）",
    }


def read_table(path: str | Path, table_name: str, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    connection, _ = connect_readonly(path)
    try:
        names = _table_names(connection)
        actual = next((name for name in names if name.casefold() == table_name.casefold()), None)
        if not actual:
            raise KeyError(f"MDB 中不存在表：{table_name}")
        columns = [row["name"] for row in _columns(connection, actual)]
        total = int(connection.cursor().execute(f"SELECT COUNT(*) FROM {_quote(actual)}").fetchone()[0])
        safe_limit = max(1, min(int(limit), 1000))
        safe_offset = max(0, int(offset))
        return {
            "table": actual,
            "columns": columns,
            "column_help": {name: column_explanation(actual, name) for name in columns},
            "explanation": table_explanation(actual, total),
            "rows": list(_row_dicts(connection, actual, safe_limit, safe_offset)),
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }
    finally:
        connection.close()


def add_access_tables_to_zip(
    archive: zipfile.ZipFile,
    path: str | Path,
    table_names: Iterable[str],
    prefix: str = "OFM原表",
) -> None:
    connection, _ = connect_readonly(path)
    try:
        for index, table_name in enumerate(table_names, 1):
            columns = [row["name"] for row in _columns(connection, table_name)]
            safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", table_name)
            # Stream table rows directly into the ZIP member.  Some OFM DCA
            # forecast tables contain millions of rows; building one giant
            # StringIO first can make a valid export look like it has stalled
            # or exhaust desktop memory.
            with archive.open(f"{prefix}/{index:02d}_{safe_name}.csv", "w") as binary_stream:
                with io.TextIOWrapper(binary_stream, encoding="utf-8-sig", newline="") as text_stream:
                    writer = csv.DictWriter(text_stream, fieldnames=columns, extrasaction="ignore")
                    writer.writeheader()
                    for row in _row_dicts(connection, table_name):
                        writer.writerow(row)
    finally:
        connection.close()


def table_csv_bytes(path: str | Path, table_name: str) -> bytes:
    connection, _ = connect_readonly(path)
    try:
        actual = next((name for name in _table_names(connection) if name.casefold() == table_name.casefold()), None)
        if not actual:
            raise KeyError(f"MDB 中不存在表：{table_name}")
        columns = [row["name"] for row in _columns(connection, actual)]
        stream = io.StringIO(newline="")
        stream.write("\ufeff")
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in _row_dicts(connection, actual):
            writer.writerow(row)
        return stream.getvalue().encode("utf-8")
    finally:
        connection.close()
