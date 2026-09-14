# GeoInventory · 地数镜

[简体中文](README.zh-CN.md) | English

**Local-first geological data inventory, quality-control, and spatial-association software.** GeoInventory helps teams understand legacy subsurface data before interpretation: what exists, where it came from, how complete it is, and how wells, logs, horizons, seismic, polygons, and production records relate.

> Status: v0.6.1, pre-release / internal evaluation. The software is not a certified interpretation, reserves, safety, or regulatory decision system.

## Highlights

- Local-first operation: source files remain in place; a `.nvt` workspace stores indexes, analysis state, and user decisions.
- Unified well identity with explicit evidence, aliases, ambiguity candidates, and traceability to original files.
- Inventory and QC for Well Head, LAS, DEV, Well Top, core, interpretation tables, SEG-Y, horizons, faults, polygons, and production data.
- Lazy parsing for large datasets: SEG-Y amplitudes and complete LAS samples are not read during the initial inventory scan.
- Well–horizon–curve–spatial association, coverage matrices, curve comparison, trajectory calculations, and export manifests.
- A browser-based local interface plus an experimental native Windows desktop client.

## Supported inputs

| Domain | Current input coverage |
|---|---|
| Wells | CSV, TSV, XLSX well headers and aliases |
| Well logs | Common LAS 2.0 files |
| Trajectories | CSV, TSV, XLSX and common DEV text exports |
| Interpretation / core | Tabular interpretation data and image-based core collections |
| Seismic | SEG-Y headers and geometry metadata |
| Surfaces / spatial | GeoJSON, SHP polygons or closed PolyLineZ, XYZ/ZMAP+, and selected Petrel-style surface exports |
| Production | CSV/XLSX exports and read-only OFM `.mdb`/`.accdb` access when a compatible ODBC driver is installed |

## Scope and limitations

GeoInventory is an inventory and preliminary QC tool. It does **not** replace professional geoscience interpretation, coordinate-reference verification, seismic processing, petrophysical normalization, reservoir simulation, reserves auditing, or source-data governance.

- Similar names or statistical similarity are candidates, not proof that records represent the same well or curve.
- CRS is never inferred solely from coordinate magnitude. Users must confirm the coordinate system.
- Default SEG-Y byte locations cover common Rev. 1 layouts; non-standard exports may require custom mappings.
- Surface/well intersections and data-quality indicators require professional review.
- Original files are intended to remain read-only, but users must still maintain independent backups.
- The current UI and documentation are primarily Chinese; English localization is a planned extension.

See [NOTICE.md](NOTICE.md) for trademark, attribution, data, and institutional-disclaimer details.

## Install and run

### Requirements

- Windows 10/11 (primary tested platform)
- Python 3.11 or 3.12 recommended
- A modern browser for the local web interface
- Microsoft Access Database Engine matching Python architecture, only for OFM Access databases

### Web interface

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
.\run.ps1
```

Open `http://127.0.0.1:5178`. GeoInventory listens on localhost by default and does not intentionally upload project data.

### Native Windows client (experimental)

```powershell
python -m pip install -r requirements.txt
python -m pip install -r .\requirements-desktop.txt
python .\本地化桌面端\tk_desktop.py
```

The Tk client works with the Python standard library; PySide6 enables the advanced native client. Packaging scripts are available under `packaging/` and `本地化桌面端/packaging/`.

To create the official Windows x64 portable archive, run `packaging\build_release.ps1`. The script builds into the ignored `.release_work/` directory and produces a ZIP plus `SHA256SUMS.txt`.

### Test

```powershell
python -m pip install -r requirements-dev.txt
pytest -q
```

Synthetic examples are provided in `samples/`. Do not commit real project data, `.nvt` workspaces, databases, scan snapshots, logs, or local paths.

## Dependency summary

- Runtime: Flask, openpyxl, pyodbc
- Optional desktop UI: PySide6
- Development/test: pytest
- Windows packaging: PyInstaller; Inno Setup is optional for an installer

Each dependency remains governed by its own license. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Roadmap

- Configurable mappings for Petrel, Techlog, OpenWorks, SEG-Y headers, and curve units
- Version-aware interpretation comparison and richer audit trails
- CRS transformation workflows and scalable spatial indexes
- GeoPackage and additional industry interchange formats
- Multi-language UI and reproducible reporting
- Plugin/API boundaries for organization-specific rules without embedding confidential data

## Author and citation

Created and maintained by **Zhu Sicheng (朱思成)**  
Email: **zhusc.syky@sinopec.com**

If you refer to this project in an approved report, paper, presentation, or derivative work, cite the project name, author, version, year, and repository URL. Machine-readable metadata is provided in [CITATION.cff](CITATION.cff).

Suggested citation:

> Zhu, Sicheng. (2026). *GeoInventory (地数镜), version 0.6.1*. https://github.com/zsc319/GeoInventory

## License and commercial use

Copyright © 2026 Zhu Sicheng. All rights reserved.

This repository is **source-available, not open source**. Viewing and evaluation through GitHub are permitted. Commercial use, production deployment, copying, modification, redistribution, sublicensing, resale, and creation of derivative works require prior written permission unless applicable law expressly provides otherwise. See [LICENSE](LICENSE) for the controlling terms.

The SINOPEC/中国石化 names and logos are trademarks or registered trademarks of their respective owner(s). Their appearance identifies the author's stated affiliation and does not grant trademark rights or imply institutional endorsement. Remove the marks from redistributed or modified versions unless you have separate written authorization.

Official builds embed the author, contact, copyright, license summary, trademark notice, and canonical repository in the UI and `/api/software-identity`. These notices deter misrepresentation but cannot make source code technically unmodifiable. Verify downloadable builds against the SHA-256 checksum published with each GitHub Release.
