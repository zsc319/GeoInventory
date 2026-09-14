"""Canonical, build-embedded identity and legal notices for GeoInventory.

These values are intentionally duplicated in release metadata and visible user
interfaces. Removing or altering the notices does not remove the obligations in
LICENSE, NOTICE.md, or applicable copyright and trademark law.
"""

PROJECT_NAME = "GeoInventory"
PROJECT_NAME_ZH = "地数镜"
AUTHOR_NAME = "Zhu Sicheng"
AUTHOR_NAME_ZH = "朱思成"
AUTHOR_EMAIL = "zhusc.syky@sinopec.com"
AFFILIATION = "Sinopec Petroleum Exploration and Production Research Institute (PEPRIS)"
AFFILIATION_ZH = "中石化石油勘探开发研究院（PEPRIS）"
COPYRIGHT = "Copyright © 2026 Zhu Sicheng. All rights reserved."
LICENSE_SUMMARY = "Source-available; commercial use, modification, and redistribution require prior written permission."
LICENSE_SUMMARY_ZH = "源代码可见；商业使用、修改与再分发须事先取得书面许可。"
TRADEMARK_NOTICE = "SINOPEC and related marks belong to their respective owner(s); display does not imply endorsement."
TRADEMARK_NOTICE_ZH = "SINOPEC/中国石化及相关标识权利归其所有；展示不代表官方认可。"
CANONICAL_REPOSITORY = "https://github.com/zsc319/GeoInventory"


def public_identity(version: str) -> dict[str, str]:
    """Return the immutable identity block exposed by official builds."""
    return {
        "project": PROJECT_NAME,
        "project_zh": PROJECT_NAME_ZH,
        "version": version,
        "author": AUTHOR_NAME,
        "author_zh": AUTHOR_NAME_ZH,
        "email": AUTHOR_EMAIL,
        "affiliation": AFFILIATION,
        "affiliation_zh": AFFILIATION_ZH,
        "copyright": COPYRIGHT,
        "license": LICENSE_SUMMARY,
        "license_zh": LICENSE_SUMMARY_ZH,
        "trademark": TRADEMARK_NOTICE,
        "trademark_zh": TRADEMARK_NOTICE_ZH,
        "repository": CANONICAL_REPOSITORY,
    }
