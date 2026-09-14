from __future__ import annotations

"""Local, display-only translation of directory file names.

No source path is renamed and no file content is opened.  The module only
walks file names, records a display alias in the active workspace database,
and lets the catalogue use that alias when it is available.
"""

import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from .db import utcnow


TRANSLATOR_VERSION = "local-glossary-v1"

# The glossary intentionally focuses on common Spanish/English oilfield and
# Petrel export vocabulary.  Unknown well names, mnemonics and numbers are
# preserved verbatim rather than guessed.
LOCAL_GLOSSARY = {
    "aceite": "原油", "agua": "水", "gas": "天然气", "produccion": "生产", "productor": "生产井",
    "inyeccion": "注入", "inyector": "注水井", "pozo": "井", "pozos": "井", "well": "井", "wells": "井",
    "cabezal": "井口", "trayectoria": "井轨迹", "desviacion": "井斜", "perforacion": "射孔",
    "completacion": "完井", "completion": "完井", "registro": "测井", "registros": "测井",
    "log": "测井", "logs": "测井", "welllog": "测井", "welllogs": "测井",
    "nucleo": "岩心", "nucleos": "岩心", "core": "岩心", "cores": "岩心",
    "horizonte": "层位", "horizontes": "层位", "horizon": "层位", "horizons": "层位",
    "falla": "断层", "fallas": "断层", "fault": "断层", "faults": "断层",
    "sismica": "地震", "seismica": "地震", "seismic": "地震", "segy": "地震数据",
    "velocidad": "速度", "velocity": "速度", "migracion": "偏移", "migration": "偏移",
    "tiempo": "时间", "time": "时间", "profundidad": "深度", "depth": "深度",
    "modelo": "模型", "model": "模型", "geologico": "地质", "geology": "地质",
    "reservorio": "油藏", "yacimiento": "油藏", "reservoir": "油藏",
    "presion": "压力", "pressure": "压力", "saturacion": "饱和度", "porosidad": "孔隙度",
    "permeabilidad": "渗透率", "facies": "相", "litologia": "岩性", "lithology": "岩性",
    "superficie": "表面", "surface": "表面", "poligono": "多边形", "polygon": "多边形",
    "linea": "测线", "line": "测线", "seccion": "剖面", "secciones": "剖面",
    "secuencia": "层序", "tabla": "表", "datos": "数据", "data": "数据",
    "estimacion": "估算", "estimation": "估算", "interpretacion": "解释", "interpretation": "解释",
    "reporte": "报告", "report": "报告", "usuario": "用户", "user": "用户",
    "base": "基础", "database": "数据库", "cementacion": "固井", "cementation": "固井",
    "sonico": "声波", "sonic": "声波",
    "diario": "日报", "mensual": "月度", "anual": "年度", "original": "原始",
    "modificacion": "修改", "modification": "修改", "version": "版本", "final": "最终",
}

# Compound names occur frequently in Spanish Petrel/OFM exports.  They are
# resolved before token-by-token translation so word order remains meaningful.
PHRASE_GLOSSARY = {
    "sonicodecementacion": "固井声波",
    "cementacionsonica": "固井声波",
    "basedeusuario": "用户数据库",
    "userdatabase": "用户数据库",
    "reportecmi": "CMI报告",
    "cmireporte": "CMI报告",
}


def _normalized(value: str) -> str:
    plain = "".join(char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", plain.lower())


def parse_custom_glossary(value: str | None) -> dict[str, str]:
    """Parse optional ``source = 中文`` entries without network translation."""
    output: dict[str, str] = {}
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s*(?:=|=>|→)\s*", line, maxsplit=1)
        if len(parts) != 2:
            continue
        source, target = parts[0].strip(), parts[1].strip()
        key = _normalized(source)
        if key and target:
            output[key] = target
    return output


def translate_filename(filename: str, custom_glossary: dict[str, str] | None = None) -> str:
    path = Path(filename)
    suffix = path.suffix
    stem = filename[:-len(suffix)] if suffix else filename
    glossary = dict(LOCAL_GLOSSARY)
    glossary.update(custom_glossary or {})
    # A custom entry can also target a complete phrase, for example
    # ``Reporte_CMI = CMI 解释报告``.  It wins over built-in terminology.
    phrase_key = _normalized(stem)
    phrase = (custom_glossary or {}).get(phrase_key) or PHRASE_GLOSSARY.get(phrase_key)
    if phrase:
        return f"{phrase}{suffix}"
    pieces = re.split(r"([_\-\s\[\](){}]+)", stem)
    changed = False
    translated: list[str] = []
    for piece in pieces:
        if not piece or re.fullmatch(r"[_\-\s\[\](){}]+", piece):
            translated.append(piece)
            continue
        replacement = glossary.get(_normalized(piece))
        if replacement:
            translated.append(replacement)
            changed = changed or replacement != piece
        else:
            translated.append(piece)
    display = "".join(translated) + suffix
    return display if changed else filename


def translate_folder(
    conn: sqlite3.Connection,
    project_root: str,
    folder: str | Path,
    custom_glossary_text: str | None = None,
) -> dict[str, Any]:
    target = Path(folder).expanduser().resolve()
    if not target.is_dir():
        raise ValueError("请选择可访问的本地文件夹")
    custom = parse_custom_glossary(custom_glossary_text)
    root_key = str(Path(project_root).resolve())
    folder_key = str(target)
    translated = unchanged = total = 0
    rows: list[tuple[Any, ...]] = []

    # Re-running the exact folder refreshes its aliases in-place.  Source files
    # and project-catalog records are never deleted or renamed.
    conn.execute("DELETE FROM directory_display_translations WHERE project_root=? AND folder_path=?", (root_key, folder_key))
    for directory, _, filenames in os.walk(target, followlinks=False):
        parent = Path(directory)
        for filename in filenames:
            source = parent / filename
            try:
                source_path = source.resolve()
                resolved = str(source_path)
                relative = str(source_path.relative_to(target))
            except OSError:
                continue
            display = translate_filename(filename, custom)
            total += 1
            if display == filename:
                unchanged += 1
            else:
                translated += 1
            rows.append((root_key, folder_key, resolved, relative, filename, display, source.suffix.lower(), utcnow(), TRANSLATOR_VERSION))
            if len(rows) >= 800:
                _upsert_rows(conn, rows)
                rows.clear()
    if rows:
        _upsert_rows(conn, rows)
    conn.commit()
    return {
        "folder": folder_key, "total_files": total, "translated_files": translated,
        "unchanged_files": unchanged, "custom_terms": len(custom), "translator": "本地术语表",
        "translator_version": TRANSLATOR_VERSION,
    }


def _upsert_rows(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    conn.executemany(
        """INSERT INTO directory_display_translations(
               project_root,folder_path,file_path,relative_path,original_name,display_name,extension,translated_at,translator_version
           ) VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(project_root,file_path) DO UPDATE SET
             folder_path=excluded.folder_path,relative_path=excluded.relative_path,original_name=excluded.original_name,
             display_name=excluded.display_name,extension=excluded.extension,translated_at=excluded.translated_at,
             translator_version=excluded.translator_version""",
        rows,
    )


def translation_page(
    conn: sqlite3.Connection,
    project_root: str,
    folder: str | Path,
    query: str = "",
    page: int = 1,
    per_page: int = 160,
) -> dict[str, Any]:
    folder_key = str(Path(folder).expanduser().resolve())
    root_key = str(Path(project_root).resolve())
    page = max(1, int(page))
    per_page = max(20, min(300, int(per_page)))
    where = ["project_root=?", "folder_path=?"]
    params: list[Any] = [root_key, folder_key]
    clean = str(query or "").strip()
    if clean:
        where.append("(original_name LIKE ? COLLATE NOCASE OR display_name LIKE ? COLLATE NOCASE OR relative_path LIKE ? COLLATE NOCASE)")
        like = f"%{clean}%"
        params.extend([like, like, like])
    clause = " AND ".join(where)
    total = int(conn.execute(f"SELECT COUNT(*) count FROM directory_display_translations WHERE {clause}", params).fetchone()["count"])
    rows = [dict(row) for row in conn.execute(
        f"""SELECT relative_path,original_name,display_name,extension,translated_at
            FROM directory_display_translations WHERE {clause}
            ORDER BY relative_path COLLATE NOCASE LIMIT ? OFFSET ?""",
        (*params, per_page, (page - 1) * per_page),
    )]
    counts = conn.execute(
        """SELECT COUNT(*) total,SUM(CASE WHEN display_name<>original_name THEN 1 ELSE 0 END) translated,
                  MAX(translated_at) updated_at
           FROM directory_display_translations WHERE project_root=? AND folder_path=?""",
        (root_key, folder_key),
    ).fetchone()
    return {
        "folder": folder_key, "items": rows, "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "translated_files": int(counts["translated"] or 0), "updated_at": counts["updated_at"],
    }
