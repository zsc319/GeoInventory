# 地数镜 · GeoInventory

简体中文 | [English](README.md)

**本地优先的地质资料清查、质量控制与空间关联软件。** GeoInventory 用于在解释工作开始前梳理老工区资料：有什么、来自哪里、完备程度如何，以及井、测井、层位、地震、Polygon 和生产数据之间如何关联。

> 当前状态：v0.6.1，预发布 / 内部评估版。本软件不是经认证的地质解释、储量评价、安全或监管决策系统。

## 核心特点

- 本地优先：源文件保留在原位置；`.nvt` 工区保存索引、分析状态和人工判断。
- 以证据统一井身份，保留别名、歧义候选以及到原始文件的追溯关系。
- 清查与质控范围包括 Well Head、LAS、DEV、Well Top、岩心、解释表、SEG-Y、层位、断层、Polygon 和生产数据。
- 大文件按需解析：首次清查不读取 SEG-Y 振幅，也不遍历全部 LAS 数值样点。
- 提供井—层位—曲线—空间关联、覆盖矩阵、曲线对比、轨迹计算和带清单的导出。
- 同时提供本地浏览器界面和实验性的 Windows 原生桌面端。

## 支持的输入

| 资料域 | 当前输入范围 |
|---|---|
| 井 | CSV、TSV、XLSX 井头表和井名别名表 |
| 测井 | 常见 LAS 2.0 文件 |
| 井斜 | CSV、TSV、XLSX 及常见 DEV 文本导出 |
| 解释 / 岩心 | 表格型解释数据和图片型岩心资料 |
| 地震 | SEG-Y 文件头、二进制头和道头几何信息 |
| 层面 / 空间 | GeoJSON、SHP Polygon 或闭合 PolyLineZ、XYZ/ZMAP+ 及部分 Petrel 风格层面导出 |
| 生产 | CSV/XLSX 导出；安装匹配位数 ODBC 驱动后可只读访问 OFM `.mdb`/`.accdb` |

## 使用范围与功能边界

GeoInventory 是资料清查和初步质量控制工具，**不能替代**专业地学解释、坐标参考系核验、地震处理、测井标准化、油藏数值模拟、储量审计或源数据治理。

- 相似井名或统计相似仅是候选证据，不能直接证明为同一口井或同一条曲线。
- 系统不会仅依据坐标数值猜测 CRS；坐标系必须由用户确认。
- 默认 SEG-Y 字节位覆盖常见 Rev. 1 布局，非标准导出可能需要自定义映射。
- 层面—井轨迹交会与数据质量提示必须经过专业人员复核。
- 软件按只读原则处理原始文件，但用户仍应独立备份重要资料。
- 当前界面和操作文档以中文为主，英文界面属于后续扩展方向。

商标、引用、数据与单位关系的完整说明见 [NOTICE.md](NOTICE.md)。

## 安装与运行

### 环境要求

- Windows 10/11（当前主要测试平台）
- 建议 Python 3.11 或 3.12
- 本地网页端需要现代浏览器
- 仅在读取 OFM Access 数据库时，需要安装与 Python 位数一致的 Microsoft Access Database Engine

### 本地网页端

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
.\run.ps1
```

打开 `http://127.0.0.1:5178`。GeoInventory 默认只监听本机地址，不会主动上传项目资料。

### Windows 原生桌面端（实验性）

```powershell
python -m pip install -r requirements.txt
python -m pip install -r .\requirements-desktop.txt
python .\本地化桌面端\tk_desktop.py
```

Tk 客户端仅依赖 Python 标准库；安装 PySide6 后可使用高级原生客户端。构建脚本位于 `packaging/` 与 `本地化桌面端/packaging/`。

运行 `packaging\build_release.ps1` 可生成官方 Windows x64 便携包。脚本会在已被忽略的 `.release_work/` 目录内生成 ZIP 与 `SHA256SUMS.txt`。

### 测试

```powershell
python -m pip install -r requirements-dev.txt
pytest -q
```

`samples/` 仅包含人工构造的示例。请勿提交真实项目资料、`.nvt` 工区、数据库、扫描快照、日志或本机路径。

## Python 包与构建依赖

- 运行：Flask、openpyxl、pyodbc
- 可选桌面界面：PySide6
- 开发与测试：pytest
- Windows 打包：PyInstaller；如需安装程序可另装 Inno Setup

各依赖继续适用其自身许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 后续可扩展方向

- Petrel、Techlog、OpenWorks、SEG-Y 道头与曲线单位的可配置映射
- 面向版本的解释成果对比和更完整的审计链
- CRS 转换工作流和大规模空间索引
- GeoPackage 及更多行业交换格式
- 多语言界面与可复现报告
- 通过插件/API 承载单位专用规则，避免在代码中固化涉密信息

## 作者与引用

创作与维护：**Zhu Sicheng（朱思成）**  
邮箱：**zhusc.syky@sinopec.com**

在获得许可的报告、论文、演示或衍生成果中引用本项目时，应注明项目名称、作者、版本、年份和仓库地址。机器可读的引用元数据见 [CITATION.cff](CITATION.cff)。

建议引用格式：

> Zhu, Sicheng. (2026). *GeoInventory (地数镜), version 0.6.1*. https://github.com/zsc319/GeoInventory

## 版权与商业使用

Copyright © 2026 Zhu Sicheng。保留所有权利。

本仓库属于**源代码可见项目，不是开源软件**。允许通过 GitHub 查看与评估；除适用法律明确允许的情形外，任何商业使用、生产部署、复制、修改、再分发、转授权、转售或制作衍生作品，均须事先取得书面许可。以 [LICENSE](LICENSE) 为准。

SINOPEC/中国石化名称及图形属于其权利人所有的商标或注册商标。其出现仅用于说明作者所陈述的工作单位，不授予任何商标权，也不代表单位对本项目作出官方认可。未经商标权利人另行书面许可，修改版或再分发版本必须移除相关标识。

官方构建会在界面、`/api/software-identity` 身份接口中固化作者、联系方式、版权、许可摘要、商标声明和规范仓库地址。这些信息可提高换壳成本并用于识别不当署名，但无法在技术上使公开源代码变成不可修改。下载发行包时，请使用 GitHub Release 同时公布的 SHA-256 校验值核对文件。
