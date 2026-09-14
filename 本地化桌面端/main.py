"""地数镜原生桌面端。

本模块刻意不启动 Flask，也不内嵌浏览器。它直接读取 .nvt 工区的
SQLite 与项目快照；大型 LAS/SEG-Y/DEV 只在用户明确查看时按需读取。
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DESKTOP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DESKTOP_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QCoreApplication, QPointF, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from geo_inventory import analytics
from geo_inventory.db import Database
from geo_inventory.project_catalog import catalog_payload, parse_dev_stations, project_wells
from geo_inventory.project_scan import load_snapshot


APP_NAME = "地数镜 GeoInventory · 本地桌面版"


def format_number(value: Any, digits: int = 0) -> str:
    if value is None:
        return "—"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits:
        return f"{numeric:,.{digits}f}"
    return f"{numeric:,.0f}"


def open_in_explorer(path: str | Path) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"原始文件不存在：{target}")
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", "/select,", str(target.resolve())])
    else:
        subprocess.Popen(["xdg-open", str(target.parent.resolve())])


@dataclass
class WorkspaceData:
    """A small, read-mostly bridge to the existing .nvt data model."""

    folder: Path | None = None
    database: Database | None = None
    snapshot: dict[str, Any] | None = None

    def open(self, selected: str | Path) -> None:
        selected_path = Path(selected).expanduser().resolve()
        candidates = [selected_path]
        if selected_path.name.lower() == "data":
            candidates.append(selected_path.parent)
        if selected_path.is_dir():
            candidates.extend(child for child in selected_path.glob("*.nvt") if child.is_dir())
        for candidate in candidates:
            snapshot_path = candidate / "project_snapshot.json"
            database_path = candidate / "inventory.sqlite"
            if snapshot_path.is_file() and database_path.is_file():
                self.folder = candidate
                self.database = Database(database_path)
                self.snapshot = load_snapshot(snapshot_path)
                return
        raise ValueError("未在所选位置发现同时包含 inventory.sqlite 与 project_snapshot.json 的 .nvt 工区。")

    @property
    def project_root(self) -> str:
        if not self.snapshot:
            return ""
        return str(Path(self.snapshot["project"]["root"]).resolve())

    @property
    def ready(self) -> bool:
        return bool(self.database and self.snapshot)

    def wells(self) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        assert self.database and self.snapshot
        with self.database.connect() as connection:
            return project_wells(connection, self.snapshot)

    def summary(self) -> dict[str, Any]:
        if not self.ready:
            return {"files": 0, "wells": 0, "coordinates": 0, "las": 0, "dev": 0}
        assert self.database and self.snapshot
        wells = self.wells()
        categories = {row["key"]: row for row in self.snapshot.get("categories", [])}
        return {
            "files": self.snapshot.get("project", {}).get("total_files", self.snapshot.get("project", {}).get("file_count", 0)),
            "wells": len(wells),
            "coordinates": sum(row.get("x") is not None and row.get("y") is not None for row in wells),
            "las": sum("las" in row.get("source_types", []) for row in wells),
            "dev": sum("deviation" in row.get("source_types", []) for row in wells),
            "seismic": int(categories.get("seismic_3d", {}).get("files", 0)) + int(categories.get("seismic_2d", {}).get("files", 0)),
        }

    def catalog(self, category: str | None = None, query: str = "", per_page: int = 350) -> dict[str, Any]:
        if not self.ready:
            return {"items": [], "counts": [], "total": 0}
        assert self.database
        with self.database.connect() as connection:
            return catalog_payload(connection, self.project_root, category, query, page=1, per_page=per_page)

    def trajectory(self, well_key: str) -> list[dict[str, float]]:
        """Load only the selected well's first DEV, never a whole project."""
        for well in self.wells():
            if well["project_key"] == well_key:
                paths = sorted(well.get("_dev_paths") or [])
                if paths:
                    return parse_dev_stations(paths[0])
        return []

    def source_files(self, well_key: str) -> list[dict[str, Any]]:
        for well in self.wells():
            if well["project_key"] == well_key:
                return list(well.get("_sources") or [])
        return []

    def production_count(self) -> int:
        if not self.ready:
            return 0
        assert self.database
        with self.database.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM production_monthly").fetchone()[0])


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—", note: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        self.title = QLabel(title)
        self.title.setObjectName("metricTitle")
        self.value = QLabel(value)
        self.value.setObjectName("metricValue")
        self.note = QLabel(note)
        self.note.setObjectName("metricNote")
        self.note.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.note)
        layout.addStretch(1)

    def set_values(self, value: str, note: str) -> None:
        self.value.setText(value)
        self.note.setText(note)


class PlanMap(QWidget):
    """A lightweight native 2D map with wheel zoom and drag pan."""

    well_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(420, 360)
        self.setMouseTracking(True)
        self.wells: list[dict[str, Any]] = []
        self.trajectory: list[dict[str, float]] = []
        self.selected_key = ""
        self.point_size = 4.0
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self._drag_start: QPointF | None = None
        self._pan_start = QPointF(0, 0)

    def set_data(self, wells: list[dict[str, Any]]) -> None:
        self.wells = [row for row in wells if row.get("x") is not None and row.get("y") is not None]
        self.reset_view()

    def set_trajectory(self, points: list[dict[str, float]]) -> None:
        self.trajectory = points
        self.update()

    def set_point_size(self, value: float) -> None:
        self.point_size = value
        self.update()

    def reset_view(self) -> None:
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self.update()

    def _bounds(self) -> tuple[float, float, float, float] | None:
        points = [(float(row["x"]), float(row["y"])) for row in self.wells]
        points += [(float(row["x"]), float(row["y"])) for row in self.trajectory if "x" in row and "y" in row]
        if not points:
            return None
        xs, ys = [point[0] for point in points], [point[1] for point in points]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        if xmin == xmax:
            xmin -= 1
            xmax += 1
        if ymin == ymax:
            ymin -= 1
            ymax += 1
        return xmin, xmax, ymin, ymax

    def _transform(self):
        bounds = self._bounds()
        if not bounds:
            return None
        xmin, xmax, ymin, ymax = bounds
        rect = self.rect().adjusted(48, 36, -24, -38)
        scale = min(rect.width() / (xmax - xmin), rect.height() / (ymax - ymin))
        offset_x = rect.left() + (rect.width() - (xmax - xmin) * scale) / 2
        offset_y = rect.bottom() - (rect.height() - (ymax - ymin) * scale) / 2

        def point(x: float, y: float) -> QPointF:
            base = QPointF(offset_x + (x - xmin) * scale, offset_y - (y - ymin) * scale)
            center = QPointF(self.width() / 2, self.height() / 2)
            return center + (base - center) * self.zoom + self.pan
        return point, bounds

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API naming
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#081812"))
        transform = self._transform()
        if not transform:
            painter.setPen(QColor("#8aa59a"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "没有可投影的井坐标")
            return
        point, bounds = transform
        xmin, xmax, ymin, ymax = bounds
        painter.setPen(QPen(QColor("#1f4035"), 1))
        for index in range(6):
            x = 48 + (self.width() - 72) * index / 5
            y = 36 + (self.height() - 74) * index / 5
            painter.drawLine(int(x), 36, int(x), self.height() - 38)
            painter.drawLine(48, int(y), self.width() - 24, int(y))
        painter.setPen(QColor("#6d897d"))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(50, self.height() - 15, f"X {format_number(xmin)} – {format_number(xmax)}")
        painter.drawText(self.width() - 180, self.height() - 15, f"Y {format_number(ymin)} – {format_number(ymax)}")
        if len(self.trajectory) > 1:
            halo = QPainterPath()
            halo.moveTo(point(self.trajectory[0]["x"], self.trajectory[0]["y"]))
            for row in self.trajectory[1:]:
                halo.lineTo(point(row["x"], row["y"]))
            painter.setPen(QPen(QColor("#020b08"), 5.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(halo)
            painter.setPen(QPen(QColor("#53c9b9"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(halo)
        font = QFont("Segoe UI", 8)
        painter.setFont(font)
        for row in self.wells:
            marker = point(float(row["x"]), float(row["y"]))
            selected = row.get("project_key") == self.selected_key
            painter.setBrush(QColor("#ffb24a") if selected else QColor("#b9e94b"))
            painter.setPen(QPen(QColor("#f4fff9") if selected else QColor("#213d31"), 1))
            radius = self.point_size + (1.5 if selected else 0)
            painter.drawEllipse(marker, radius, radius)
            if selected:
                painter.setPen(QColor("#e2f4e9"))
                painter.drawText(marker + QPointF(8, -7), row.get("canonical_name", ""))
        painter.setPen(QColor("#8fa99f"))
        painter.drawText(12, 22, f"{format_number(len(self.wells))} 口坐标井 · 滚轮缩放 · 按住左键平移 · 单击井点查看")

    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.zoom = max(0.3, min(9, self.zoom * factor))
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._pan_start = QPointF(self.pan)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.position() - self._drag_start
            if delta.manhattanLength() > 3:
                self.pan = self._pan_start + delta
                self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        moved = self._drag_start is not None and (event.position() - self._drag_start).manhattanLength() > 3
        self._drag_start = None
        if moved:
            return
        transform = self._transform()
        if not transform:
            return
        point, _ = transform
        nearest, distance = None, 12.0
        for row in self.wells:
            candidate = point(float(row["x"]), float(row["y"]))
            current = math.hypot(candidate.x() - event.position().x(), candidate.y() - event.position().y())
            if current < distance:
                nearest, distance = row, current
        if nearest:
            self.selected_key = nearest["project_key"]
            self.update()
            self.well_selected.emit(nearest["project_key"])


class DashboardPage(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)
        heading = QLabel("项目驾驶舱")
        heading.setObjectName("pageHeading")
        self.subtitle = QLabel("打开 .nvt 工区后，统计直接来自本地分析数据库。")
        self.subtitle.setObjectName("pageSubtitle")
        layout.addWidget(heading)
        layout.addWidget(self.subtitle)
        cards = QGridLayout()
        cards.setHorizontalSpacing(12)
        cards.setVerticalSpacing(12)
        self.cards = {
            "files": MetricCard("完整目录索引"), "wells": MetricCard("统一井数"),
            "coordinates": MetricCard("有坐标井"), "las": MetricCard("LAS 覆盖"),
            "dev": MetricCard("DEV 覆盖"), "seismic": MetricCard("地震对象"),
        }
        for index, card in enumerate(self.cards.values()):
            cards.addWidget(card, index // 3, index % 3)
        layout.addLayout(cards)
        self.notes = QLabel("原始资料保持在原目录；桌面端仅维护 `.nvt` 中的数据库、索引和人工判定。")
        self.notes.setWordWrap(True)
        self.notes.setObjectName("callout")
        layout.addWidget(self.notes)
        layout.addStretch(1)

    def reload(self, workspace: WorkspaceData) -> None:
        summary = workspace.summary()
        values = {
            "files": (format_number(summary["files"]), "目录物理文件"),
            "wells": (format_number(summary["wells"]), "Well Head / LAS / DEV 等统一"),
            "coordinates": (format_number(summary["coordinates"]), "可进行二维投影"),
            "las": (format_number(summary["las"]), "口井具备 LAS 证据"),
            "dev": (format_number(summary["dev"]), "口井具备 DEV 证据"),
            "seismic": (format_number(summary["seismic"]), "2D / 3D 地震文件"),
        }
        for key, (value, note) in values.items():
            self.cards[key].set_values(value, note)
        self.subtitle.setText(str(workspace.folder) if workspace.folder else "尚未打开工区")


class WellsPage(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.workspace: WorkspaceData | None = None
        self.rows: list[dict[str, Any]] = []
        self.filtered_rows: list[dict[str, Any]] = []
        self.current_key = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(10)
        heading = QLabel("井与井名")
        heading.setObjectName("pageHeading")
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标准井名、UWI、来源或文件名")
        self.search.textChanged.connect(self.apply_filter)
        self.show_trajectory = QCheckBox("显示所选井 DEV 轨迹")
        self.show_trajectory.toggled.connect(self.update_selected_trajectory)
        self.point_size = QSpinBox()
        self.point_size.setRange(1, 14)
        self.point_size.setValue(4)
        self.point_size.valueChanged.connect(lambda value: self.map.set_point_size(float(value)))
        reset = QPushButton("恢复 100%")
        reset.clicked.connect(self.map.reset_view)
        controls.addWidget(self.search, 1)
        controls.addWidget(self.show_trajectory)
        controls.addWidget(QLabel("井点大小"))
        controls.addWidget(self.point_size)
        controls.addWidget(reset)
        layout.addLayout(controls)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["标准井名", "UWI", "来源", "曲线", "坐标 X", "坐标 Y", "硬证据"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.table_selected)
        splitter.addWidget(self.table)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.map = PlanMap()
        self.map.well_selected.connect(self.select_key)
        self.details = QLabel("选择一口井可查看资料来源；勾选后只按需读取该井的一份 DEV。")
        self.details.setObjectName("wellDetails")
        self.details.setWordWrap(True)
        right_layout.addWidget(self.map, 1)
        right_layout.addWidget(self.details)
        splitter.addWidget(right)
        splitter.setSizes([640, 760])
        layout.addWidget(splitter, 1)

    def reload(self, workspace: WorkspaceData) -> None:
        self.workspace = workspace
        self.rows = workspace.wells()
        self.apply_filter()

    def apply_filter(self) -> None:
        query = self.search.text().strip().upper()
        self.filtered_rows = [
            row for row in self.rows
            if not query or query in " ".join([str(row.get("canonical_name", "")), str(row.get("uwi", "")), str(row.get("preferred_filename", "")), *row.get("source_types", [])]).upper()
        ]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.filtered_rows))
        for index, row in enumerate(self.filtered_rows):
            values = [
                row.get("canonical_name", "—"), row.get("uwi") or "—",
                " / ".join(row.get("source_types", [])) or "—",
                f"{format_number(row.get('curve_count'))} 种",
                format_number(row.get("x"), 2), format_number(row.get("y"), 2),
                row.get("identity_status", "—"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, row.get("project_key"))
                self.table.setItem(index, column, item)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.map.set_data(self.filtered_rows)
        if self.current_key:
            self.select_key(self.current_key, ensure_row=False)

    def table_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            key = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
            self.select_key(str(key), ensure_row=False)

    def select_key(self, key: str, ensure_row: bool = True) -> None:
        self.current_key = key
        selected = next((row for row in self.rows if row.get("project_key") == key), None)
        if not selected:
            return
        self.map.selected_key = key
        self.update_selected_trajectory()
        sources = selected.get("_sources", [])
        source_text = " · ".join(dict.fromkeys(str(item.get("filename") or "") for item in sources if item.get("filename")))
        self.details.setText(
            f"<b>{selected.get('canonical_name')}</b>　{selected.get('identity_status', '—')}<br>"
            f"坐标：{format_number(selected.get('x'), 2)}, {format_number(selected.get('y'), 2)}　"
            f"曲线：{format_number(selected.get('curve_count'))} 种<br>"
            f"来源文件：{source_text or '—'}"
        )
        if ensure_row:
            for index, row in enumerate(self.filtered_rows):
                if row.get("project_key") == key:
                    self.table.selectRow(index)
                    break

    def update_selected_trajectory(self) -> None:
        if not self.workspace or not self.current_key or not self.show_trajectory.isChecked():
            self.map.set_trajectory([])
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            points = self.workspace.trajectory(self.current_key)
            self.map.set_trajectory(points)
            if not points:
                self.details.setText(self.details.text() + "<br><span style='color:#dca46c'>当前井没有可读取的 DEV 测点。</span>")
        finally:
            QApplication.restoreOverrideCursor()


class CatalogPage(QWidget):
    labels = {
        "": "全部资料", "well_heads": "Well Head", "well_logs": "Well LAS", "well_paths": "Well DEV",
        "well_tops": "Well Top", "production": "生产动态", "seismic_3d": "3D 地震",
        "seismic_2d": "2D 地震", "horizons": "层位", "faults": "断层", "polygons": "Polygon",
    }

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.workspace: WorkspaceData | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(10)
        heading = QLabel("资料数据库")
        heading.setObjectName("pageHeading")
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.category = QComboBox()
        for key, label in self.labels.items():
            self.category.addItem(label, key)
        self.category.currentIndexChanged.connect(self.reload_table)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文件名、对象名或原始路径")
        self.search.returnPressed.connect(self.reload_table)
        refresh = QPushButton("检索")
        refresh.clicked.connect(self.reload_table)
        controls.addWidget(QLabel("资料类型"))
        controls.addWidget(self.category)
        controls.addWidget(self.search, 1)
        controls.addWidget(refresh)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["文件 / 数据对象", "类型", "原始目录", "扩展名", "大小", "状态"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.open_selected)
        layout.addWidget(self.table, 1)
        self.note = QLabel("双击文件行可在本机资源管理器中定位源文件。")
        self.note.setObjectName("pageSubtitle")
        layout.addWidget(self.note)

    def reload(self, workspace: WorkspaceData) -> None:
        self.workspace = workspace
        self.reload_table()

    def reload_table(self) -> None:
        if not self.workspace:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            data = self.workspace.catalog(self.category.currentData(), self.search.text().strip())
        finally:
            QApplication.restoreOverrideCursor()
        items = data.get("items", [])
        self.table.setRowCount(len(items))
        for index, row in enumerate(items):
            values = [row.get("filename"), row.get("category_key"), row.get("relative_path"), row.get("extension"), format_number(row.get("bytes")), row.get("status")]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or "—"))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row.get("file_path"))
                self.table.setItem(index, column, item)
        self.table.resizeColumnsToContents()
        self.note.setText(f"当前显示 {format_number(len(items))} / {format_number(data.get('total'))} 个对象；双击可定位源文件。")

    def open_selected(self, row: int, column: int) -> None:
        del column
        item = self.table.item(row, 0)
        if not item:
            return
        try:
            open_in_explorer(str(item.data(Qt.ItemDataRole.UserRole)))
        except OSError as error:
            QMessageBox.warning(self, APP_NAME, str(error))


class InformationPage(QWidget):
    def __init__(self, title: str, description: str, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        heading = QLabel(title)
        heading.setObjectName("pageHeading")
        body = QLabel(description)
        body.setObjectName("informationBody")
        body.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addStretch(1)

    def update_workspace(self, workspace: WorkspaceData) -> None:
        del workspace


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.workspace = WorkspaceData()
        self.settings = QSettings("Sinopec", "GeoInventoryDesktop")
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1080, 700)
        self.resize(self.settings.value("windowSize", self.size()))
        self._build_ui()
        self._set_theme(self.settings.value("theme", "dark"))
        candidate = self.settings.value("workspace", "")
        if candidate and Path(candidate).exists():
            self.open_workspace(candidate, quiet=True)
        else:
            self.try_default_workspace()

    def _build_ui(self) -> None:
        toolbar = self.addToolBar("主工具")
        toolbar.setMovable(False)
        open_action = QAction("打开工区", self)
        open_action.triggered.connect(self.choose_workspace)
        theme_action = QAction("切换深浅风格", self)
        theme_action.triggered.connect(self.toggle_theme)
        about_action = QAction("软件介绍", self)
        about_action.triggered.connect(self.show_about)
        toolbar.addAction(open_action)
        toolbar.addSeparator()
        toolbar.addAction(theme_action)
        toolbar.addSeparator()
        toolbar.addAction(about_action)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QFrame()
        header.setObjectName("appHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 13, 22, 13)
        brand = QLabel("地数镜")
        brand.setObjectName("brand")
        header_layout.addWidget(brand)
        header_layout.addWidget(QLabel("GeoInventory · 本地桌面版"), 1)
        self.workspace_label = QLabel("未打开工区")
        self.workspace_label.setObjectName("workspaceLabel")
        header_layout.addWidget(self.workspace_label)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        nav = QFrame()
        nav.setObjectName("navigation")
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(12, 16, 12, 12)
        nav_layout.setSpacing(5)
        nav_layout.addWidget(QLabel("项目功能"), 0)
        self.stack = QStackedWidget()
        self.dashboard = DashboardPage()
        self.wells = WellsPage()
        self.catalog = CatalogPage()
        self.curves = InformationPage("测井曲线", "桌面端已完成原生窗口、工区直接读取与井/资料数据库迁移。下一步会将网页端完整的曲线类型、覆盖、聚焦合并与数值分布工作台逐项迁入该页面。")
        self.horizons = InformationPage("层位指定", "Well Top、构造面与统一层位命名继续使用同一 `.nvt` 数据库。桌面端将以原生表格、版本差异卡片和二维投影承载这些统计。")
        self.production = InformationPage("生产动态", "OFM / MDB 的结构化导入和导出数据仍保存在工区数据库。此页面保留为原生生产曲线、射孔段和递减分析的迁移入口。")
        pages = [("项目驾驶舱", self.dashboard), ("井与井名", self.wells), ("资料数据库", self.catalog), ("测井曲线", self.curves), ("层位指定", self.horizons), ("生产动态", self.production)]
        for index, (label, page) in enumerate(pages):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.clicked.connect(lambda checked=False, value=index: self.set_page(value))
            nav_layout.addWidget(button)
            self.stack.addWidget(page)
            if index == 0:
                button.setChecked(True)
            setattr(self, f"nav_{index}", button)
        nav_layout.addStretch(1)
        permissions = QLabel("权限：科室内部测试\n制作：朱思成 · 海外重点项目中心")
        permissions.setObjectName("permissions")
        permissions.setWordWrap(True)
        nav_layout.addWidget(permissions)
        splitter.addWidget(nav)
        splitter.addWidget(self.stack)
        splitter.setSizes([210, 1330])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.statusBar().showMessage("本地模式：未启动浏览器或 Web 服务")

    def set_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for item in range(self.stack.count()):
            getattr(self, f"nav_{item}").setChecked(item == index)

    def try_default_workspace(self) -> None:
        for candidate in (PROJECT_ROOT / "data", PROJECT_ROOT):
            try:
                self.open_workspace(candidate, quiet=True)
                return
            except (OSError, ValueError):
                continue

    def choose_workspace(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 .nvt 分析工区", str(self.workspace.folder or PROJECT_ROOT))
        if folder:
            self.open_workspace(folder)

    def open_workspace(self, folder: str | Path, quiet: bool = False) -> None:
        try:
            self.workspace.open(folder)
            self.settings.setValue("workspace", str(self.workspace.folder))
            self.workspace_label.setText(str(self.workspace.folder))
            self.dashboard.reload(self.workspace)
            self.wells.reload(self.workspace)
            self.catalog.reload(self.workspace)
            self.statusBar().showMessage("工区已打开：所有统计直接读取本地 SQLite 与快照", 6000)
        except (OSError, ValueError, KeyError) as error:
            if not quiet:
                QMessageBox.warning(self, APP_NAME, f"打开工区失败：\n{error}")
            raise

    def toggle_theme(self) -> None:
        current = self.settings.value("theme", "dark")
        self._set_theme("light" if current == "dark" else "dark")

    def _set_theme(self, theme: str) -> None:
        self.settings.setValue("theme", theme)
        self.setStyleSheet(LIGHT_STYLE if theme == "light" else DARK_STYLE)

    def show_about(self) -> None:
        QMessageBox.information(self, "关于地数镜", "<b>地数镜 GeoInventory · 本地桌面版</b><br><br>"
                                "不使用浏览器；所有窗口为原生 Qt 控件。<br>"
                                "原始资料仅本地读取，分析结果保存在 `.nvt` 工区。<br><br>"
                                "权限：现阶段 - 用于科室内部测试<br>制作：朱思成 - 海外重点项目中心")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.settings.setValue("windowSize", self.size())
        event.accept()


BASE_STYLE = """
* { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QMainWindow { background: %(window)s; color: %(text)s; }
QToolBar { background: %(panel)s; border: 0; border-bottom: 1px solid %(line)s; spacing: 7px; padding: 5px 10px; }
QToolButton, QPushButton { color: %(text)s; background: %(button)s; border: 1px solid %(line)s; border-radius: 7px; padding: 7px 11px; }
QToolButton:hover, QPushButton:hover { border-color: %(accent)s; background: %(hover)s; }
QToolButton:checked { color: %(accent)s; background: %(selected)s; border-color: %(accent)s; }
QLineEdit, QComboBox, QSpinBox { min-height: 30px; padding: 2px 8px; color: %(text)s; background: %(input)s; border: 1px solid %(line)s; border-radius: 6px; }
QTableWidget { background: %(panel)s; color: %(text)s; gridline-color: %(line)s; border: 1px solid %(line)s; border-radius: 8px; }
QHeaderView::section { background: %(header)s; color: %(muted)s; border: 0; border-bottom: 1px solid %(line)s; padding: 8px; font-weight: 600; }
QTableWidget::item:selected { background: %(selected)s; color: %(text)s; }
QScrollBar:vertical { width: 9px; background: transparent; margin: 4px; }
QScrollBar::handle:vertical { min-height: 28px; border-radius: 4px; background: %(scroll)s; }
#appHeader { background: %(panel)s; border-bottom: 1px solid %(line)s; }
#brand { color: %(accent)s; font-size: 22px; font-weight: 800; }
#workspaceLabel { color: %(muted)s; max-width: 470px; }
#navigation { background: %(navigation)s; border-right: 1px solid %(line)s; }
#navigation QToolButton { text-align: left; padding: 10px 12px; }
#permissions { color: %(muted)s; font-size: 11px; padding: 9px; border-top: 1px solid %(line)s; }
#pageHeading { font-size: 24px; font-weight: 800; color: %(text)s; }
#pageSubtitle { color: %(muted)s; }
#metricCard { background: %(panel)s; border: 1px solid %(line)s; border-radius: 10px; min-height: 116px; }
#metricTitle { color: %(muted)s; font-size: 11px; }
#metricValue { color: %(accent)s; font-size: 28px; font-weight: 800; }
#metricNote { color: %(muted)s; font-size: 11px; }
#callout, #wellDetails, #informationBody { background: %(callout)s; border: 1px solid %(line)s; border-radius: 9px; color: %(muted)s; padding: 14px; line-height: 1.55; }
QStatusBar { background: %(panel)s; color: %(muted)s; border-top: 1px solid %(line)s; }
"""

DARK_STYLE = BASE_STYLE % {
    "window": "#071510", "panel": "#0c2018", "navigation": "#091a14", "button": "#102a20",
    "input": "#0d211a", "header": "#10271e", "line": "#23473a", "text": "#e3eee9",
    "muted": "#8ca69a", "accent": "#b9e94b", "hover": "#173a2c", "selected": "#173a2c",
    "scroll": "#245f4d", "callout": "#10271f",
}
LIGHT_STYLE = BASE_STYLE % {
    "window": "#f3f7f4", "panel": "#ffffff", "navigation": "#edf4ef", "button": "#ffffff",
    "input": "#ffffff", "header": "#f1f6f3", "line": "#cfdfd6", "text": "#1d3229",
    "muted": "#62796f", "accent": "#176e57", "hover": "#e4f1e9", "selected": "#dceee4",
    "scroll": "#26735b", "callout": "#f0f7f2",
}


def main() -> int:
    QCoreApplication.setOrganizationName("Sinopec")
    QCoreApplication.setApplicationName("GeoInventoryDesktop")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
