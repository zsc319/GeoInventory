# Third-party dependencies / 第三方依赖

GeoInventory depends on third-party software. Those components are not covered by the GeoInventory proprietary license; each remains governed by its own upstream license and notice files.

GeoInventory 使用第三方软件。这些组件不适用 GeoInventory 的专有许可证，仍分别适用其上游许可证与声明。

| Component | Purpose | Upstream project |
|---|---|---|
| Flask | Local web application framework | https://github.com/pallets/flask |
| openpyxl | XLSX reading and writing | https://foss.heptapod.net/openpyxl/openpyxl |
| pyodbc | Optional ODBC access for `.mdb`/`.accdb` | https://github.com/mkleehammer/pyodbc |
| PySide6 | Optional advanced native desktop interface | https://doc.qt.io/qtforpython-6/ |
| pytest | Development and testing | https://github.com/pytest-dev/pytest |
| PyInstaller | Optional Windows executable packaging | https://pyinstaller.org/ |

Before redistribution, generate and review a dependency bill of materials for the exact locked versions used in the build. PySide6/Qt redistribution in particular must comply with the applicable Qt licensing terms. Microsoft Access Database Engine and Inno Setup are external prerequisites/tools and are not distributed by this source repository unless a release explicitly says otherwise.

再分发前，应针对构建时锁定的准确版本生成并复核软件物料清单。尤其是 PySide6/Qt 的再分发必须符合适用的 Qt 许可条款。Microsoft Access Database Engine 与 Inno Setup 属于外部前置组件/工具；除非具体发行说明另有明确陈述，本源代码仓库不分发它们。

