"""Zero-dependency native desktop fallback for GeoInventory.

Tk is shipped with the supported Python runtime, so this file provides a real
Windows application window even on a colleague's computer before the optional
Qt visual runtime is bundled.  It never starts Flask or a browser.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any


DESKTOP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DESKTOP_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

from geo_inventory.db import Database
from geo_inventory.project_catalog import catalog_payload, parse_dev_stations, project_wells
from geo_inventory.project_scan import load_snapshot


def number(value: Any, digits: int = 0) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}" if digits else f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def reveal_file(path: str | Path) -> None:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"原始文件不存在：{source}")
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", "/select,", str(source.resolve())])
    else:
        subprocess.Popen(["xdg-open", str(source.parent.resolve())])


@dataclass
class LocalWorkspace:
    folder: Path | None = None
    db: Database | None = None
    snapshot: dict[str, Any] | None = None

    def open(self, location: str | Path) -> None:
        source = Path(location).expanduser().resolve()
        candidates = [source]
        if source.name.lower() == "data":
            candidates.append(source.parent)
        if source.is_dir():
            candidates.extend(path for path in source.glob("*.nvt") if path.is_dir())
        for folder in candidates:
            db_path, snapshot_path = folder / "inventory.sqlite", folder / "project_snapshot.json"
            if db_path.is_file() and snapshot_path.is_file():
                self.folder, self.db, self.snapshot = folder, Database(db_path), load_snapshot(snapshot_path)
                return
        raise ValueError("请选择包含 inventory.sqlite 与 project_snapshot.json 的 .nvt 分析工区。")

    @property
    def ready(self) -> bool:
        return bool(self.db and self.snapshot)

    @property
    def project_root(self) -> str:
        return str(Path(self.snapshot["project"]["root"]).resolve()) if self.snapshot else ""

    def wells(self) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        assert self.db and self.snapshot
        with self.db.connect() as connection:
            return project_wells(connection, self.snapshot)

    def summary(self) -> dict[str, int]:
        wells = self.wells()
        categories = {row["key"]: row for row in (self.snapshot or {}).get("categories", [])}
        return {
            "files": int((self.snapshot or {}).get("project", {}).get("total_files", (self.snapshot or {}).get("project", {}).get("file_count", 0))),
            "wells": len(wells),
            "coordinates": sum(row.get("x") is not None and row.get("y") is not None for row in wells),
            "las": sum("las" in row.get("source_types", []) for row in wells),
            "dev": sum("deviation" in row.get("source_types", []) for row in wells),
            "seismic": int(categories.get("seismic_2d", {}).get("files", 0)) + int(categories.get("seismic_3d", {}).get("files", 0)),
        }

    def catalog(self, category: str = "", query: str = "") -> dict[str, Any]:
        if not self.ready:
            return {"items": [], "total": 0}
        assert self.db
        with self.db.connect() as connection:
            return catalog_payload(connection, self.project_root, category or None, query, page=1, per_page=400)

    def trajectory(self, key: str) -> list[dict[str, float]]:
        for well in self.wells():
            if well.get("project_key") == key:
                paths = sorted(well.get("_dev_paths") or [])
                return parse_dev_stations(paths[0]) if paths else []
        return []


class NativeMap(tk.Canvas):
    def __init__(self, parent: tk.Misc, **kwargs):
        super().__init__(parent, background="#081812", highlightthickness=0, **kwargs)
        self.wells: list[dict[str, Any]] = []
        self.trajectory: list[dict[str, float]] = []
        self.selected_key = ""
        self.point_size = 4
        self.zoom = 1.0
        self.pan_x = self.pan_y = 0.0
        self.drag_start: tuple[float, float] | None = None
        self.bind("<Configure>", lambda _: self.draw())
        self.bind("<MouseWheel>", self.wheel)
        self.bind("<ButtonPress-1>", self.press)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<ButtonRelease-1>", self.release)

    def set_wells(self, wells: list[dict[str, Any]]) -> None:
        self.wells = [row for row in wells if row.get("x") is not None and row.get("y") is not None]
        self.reset()

    def set_trajectory(self, points: list[dict[str, float]]) -> None:
        self.trajectory = points
        self.draw()

    def reset(self) -> None:
        self.zoom, self.pan_x, self.pan_y = 1.0, 0.0, 0.0
        self.draw()

    def bounds(self):
        points = [(float(row["x"]), float(row["y"])) for row in self.wells]
        points += [(float(row["x"]), float(row["y"])) for row in self.trajectory]
        if not points:
            return None
        xs, ys = [point[0] for point in points], [point[1] for point in points]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        return (xmin - 1 if xmin == xmax else xmin, xmax + 1 if xmin == xmax else xmax,
                ymin - 1 if ymin == ymax else ymin, ymax + 1 if ymin == ymax else ymax)

    def transform(self):
        bounds = self.bounds()
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        if not bounds or width < 20 or height < 20:
            return None
        xmin, xmax, ymin, ymax = bounds
        pad = 48
        scale = min((width - pad * 2) / (xmax - xmin), (height - pad * 2) / (ymax - ymin))
        offset_x = pad + (width - pad * 2 - (xmax - xmin) * scale) / 2
        offset_y = height - pad - (height - pad * 2 - (ymax - ymin) * scale) / 2
        def point(x: float, y: float) -> tuple[float, float]:
            base_x, base_y = offset_x + (x - xmin) * scale, offset_y - (y - ymin) * scale
            return width / 2 + (base_x - width / 2) * self.zoom + self.pan_x, height / 2 + (base_y - height / 2) * self.zoom + self.pan_y
        return point, bounds

    def draw(self) -> None:
        self.delete("all")
        transform = self.transform()
        if not transform:
            self.create_text(self.winfo_width() / 2, self.winfo_height() / 2, text="没有可投影的井坐标", fill="#9bb3aa", font=("Microsoft YaHei UI", 11))
            return
        point, bounds = transform
        width, height = self.winfo_width(), self.winfo_height()
        for index in range(6):
            x, y = 48 + (width - 72) * index / 5, 36 + (height - 74) * index / 5
            self.create_line(x, 36, x, height - 38, fill="#1f4035")
            self.create_line(48, y, width - 24, y, fill="#1f4035")
        if len(self.trajectory) > 1:
            vertices = [coordinate for row in self.trajectory for coordinate in point(row["x"], row["y"])]
            self.create_line(*vertices, fill="#020b08", width=6, smooth=True)
            self.create_line(*vertices, fill="#53c9b9", width=2.2, smooth=True)
        for row in self.wells:
            x, y = point(float(row["x"]), float(row["y"]))
            selected = row.get("project_key") == self.selected_key
            radius = self.point_size + (2 if selected else 0)
            self.create_oval(x - radius, y - radius, x + radius, y + radius, fill="#ffb24a" if selected else "#b9e94b", outline="#f2fff7" if selected else "#203d31")
            if selected:
                self.create_text(x + radius + 7, y - 8, text=row.get("canonical_name", ""), fill="#e5f4ec", anchor="w", font=("Microsoft YaHei UI", 9, "bold"))
        self.create_text(12, 15, text=f"{number(len(self.wells))} 口坐标井 · 滚轮缩放 · 左键拖动平移 · 单击井点查看", fill="#9cb6ab", anchor="w", font=("Microsoft YaHei UI", 8))
        self.create_text(50, height - 15, text=f"X {number(bounds[0])} – {number(bounds[1])}", fill="#759084", anchor="w", font=("Segoe UI", 8))
        self.create_text(width - 16, height - 15, text=f"Y {number(bounds[2])} – {number(bounds[3])}", fill="#759084", anchor="e", font=("Segoe UI", 8))

    def wheel(self, event) -> None:
        self.zoom = max(0.35, min(9, self.zoom * (1.15 if event.delta > 0 else 1 / 1.15)))
        self.draw()

    def press(self, event) -> None:
        self.drag_start = (event.x, event.y)

    def drag(self, event) -> None:
        if self.drag_start:
            self.pan_x += event.x - self.drag_start[0]
            self.pan_y += event.y - self.drag_start[1]
            self.drag_start = (event.x, event.y)
            self.draw()

    def release(self, event) -> None:
        transform = self.transform()
        if not transform:
            return
        point, _ = transform
        nearest, distance = None, 12
        for row in self.wells:
            x, y = point(float(row["x"]), float(row["y"]))
            current = math.hypot(x - event.x, y - event.y)
            if current < distance:
                nearest, distance = row, current
        if nearest and distance < 12:
            self.selected_key = nearest["project_key"]
            self.event_generate("<<WellSelected>>", data=nearest["project_key"])
            self.draw()


class DesktopApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("地数镜 GeoInventory · 本地桌面版")
        self.minsize(1080, 700)
        self.geometry("1440x880")
        self.configure(background="#071510")
        self.workspace = LocalWorkspace()
        self.wells: list[dict[str, Any]] = []
        self.current_key = ""
        self._configure_style()
        self._build()
        self.try_default_workspace()

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#071510")
        style.configure("Panel.TFrame", background="#0c2018")
        style.configure("TLabel", background="#071510", foreground="#e3eee9", font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground="#8ca69a")
        style.configure("Heading.TLabel", foreground="#e8f3ed", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("TButton", background="#102a20", foreground="#e3eee9", borderwidth=0, padding=(10, 7))
        style.map("TButton", background=[("active", "#173a2c")])
        style.configure("Treeview", background="#0c2018", fieldbackground="#0c2018", foreground="#e3eee9", rowheight=29, bordercolor="#23473a")
        style.configure("Treeview.Heading", background="#10271e", foreground="#9ab3a8", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#1b4938")], foreground=[("selected", "#ffffff")])
        style.configure("TEntry", fieldbackground="#0d211a", foreground="#e3eee9")
        style.configure("TCombobox", fieldbackground="#0d211a", foreground="#e3eee9")

    def _build(self) -> None:
        header = ttk.Frame(self, style="Panel.TFrame", padding=(20, 12))
        header.pack(fill="x")
        ttk.Label(header, text="地数镜", foreground="#b9e94b", font=("Microsoft YaHei UI", 20, "bold"), background="#0c2018").pack(side="left")
        ttk.Label(header, text="GeoInventory · 本地桌面版", style="Muted.TLabel", background="#0c2018").pack(side="left", padx=12)
        self.workspace_label = ttk.Label(header, text="未打开工区", style="Muted.TLabel", background="#0c2018")
        self.workspace_label.pack(side="right")
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True)
        navigation = ttk.Frame(body, style="Panel.TFrame", padding=12, width=210)
        content = ttk.Frame(body)
        body.add(navigation, weight=0)
        body.add(content, weight=1)
        ttk.Label(navigation, text="项目功能", style="Muted.TLabel", background="#0c2018").pack(anchor="w", pady=(3, 9))
        self.pages: dict[str, ttk.Frame] = {}
        for name in ("项目驾驶舱", "井与井名", "资料数据库", "测井曲线", "层位指定", "生产动态"):
            button = ttk.Button(navigation, text=name, command=lambda value=name: self.show_page(value))
            button.pack(fill="x", pady=3)
        ttk.Button(navigation, text="打开 .nvt 工区", command=self.choose_workspace).pack(fill="x", side="bottom", pady=(10, 2))
        ttk.Label(navigation, text="权限：科室内部测试\n制作：朱思成 · 海外重点项目中心", style="Muted.TLabel", background="#0c2018", justify="left").pack(fill="x", side="bottom", pady=8)
        self.content = content
        self.build_dashboard()
        self.build_wells()
        self.build_catalog()
        for name, text in {
            "测井曲线": "原生迁移入口：曲线覆盖、曲线类型、聚焦合并、采样评估与数值分布将直接复用同一 `.nvt` 分析数据库。",
            "层位指定": "原生迁移入口：Well Top、构造面、统一层位名称、版本差异和钻遇统计不会更改原始资料。",
            "生产动态": "原生迁移入口：OFM/MDB、射孔段、生产曲线、递减分析及导出继续保存在同一工区。",
        }.items():
            page = ttk.Frame(content, padding=32)
            ttk.Label(page, text=name, style="Heading.TLabel").pack(anchor="w")
            ttk.Label(page, text=text, style="Muted.TLabel", wraplength=840, justify="left").pack(anchor="w", pady=16)
            self.pages[name] = page
        self.status = ttk.Label(self, text="本地模式：没有启动浏览器或网页服务", style="Muted.TLabel", padding=(14, 7))
        self.status.pack(fill="x")
        self.show_page("项目驾驶舱")

    def make_card(self, parent, title: str):
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        ttk.Label(frame, text=title, style="Muted.TLabel", background="#0c2018").pack(anchor="w")
        value = ttk.Label(frame, text="—", foreground="#b9e94b", font=("Microsoft YaHei UI", 23, "bold"), background="#0c2018")
        value.pack(anchor="w", pady=(6, 3))
        note = ttk.Label(frame, text="等待工区", style="Muted.TLabel", background="#0c2018", wraplength=210)
        note.pack(anchor="w")
        return frame, value, note

    def build_dashboard(self) -> None:
        page = ttk.Frame(self.content, padding=22)
        ttk.Label(page, text="项目驾驶舱", style="Heading.TLabel").pack(anchor="w")
        self.dashboard_path = ttk.Label(page, text="打开工区后读取本地数据库", style="Muted.TLabel")
        self.dashboard_path.pack(anchor="w", pady=(3, 16))
        cards = ttk.Frame(page)
        cards.pack(fill="x")
        self.cards = {}
        labels = [("files", "完整目录索引"), ("wells", "统一井数"), ("coordinates", "有坐标井"), ("las", "LAS 覆盖"), ("dev", "DEV 覆盖"), ("seismic", "地震对象")]
        for index, (key, label) in enumerate(labels):
            frame, value, note = self.make_card(cards, label)
            frame.grid(row=index // 3, column=index % 3, sticky="nsew", padx=6, pady=6)
            cards.columnconfigure(index % 3, weight=1, uniform="cards")
            self.cards[key] = (value, note)
        note = ttk.Label(page, text="原始文件仍停留在原目录；桌面端只使用 `.nvt` 中的索引、数据库与人工判定。", style="Muted.TLabel", wraplength=900)
        note.pack(anchor="w", pady=18)
        self.pages["项目驾驶舱"] = page

    def build_wells(self) -> None:
        page = ttk.Frame(self.content, padding=18)
        top = ttk.Frame(page)
        top.pack(fill="x")
        ttk.Label(top, text="井与井名", style="Heading.TLabel").pack(side="left")
        self.well_search = ttk.Entry(top)
        self.well_search.pack(side="left", fill="x", expand=True, padx=16)
        self.well_search.bind("<KeyRelease>", lambda _: self.filter_wells())
        self.show_dev = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="显示所选井 DEV", variable=self.show_dev, command=self.update_dev).pack(side="left", padx=5)
        ttk.Label(top, text="井点大小").pack(side="left", padx=(8, 3))
        self.size_var = tk.IntVar(value=4)
        tk.Spinbox(top, from_=1, to=14, width=4, textvariable=self.size_var, command=self.update_point_size, background="#0d211a", foreground="#e3eee9", buttonbackground="#173a2c").pack(side="left")
        ttk.Button(top, text="恢复 100%", command=lambda: self.map.reset()).pack(side="left", padx=8)
        panes = ttk.Panedwindow(page, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(12, 0))
        left = ttk.Frame(panes, style="Panel.TFrame", padding=8)
        right = ttk.Frame(panes, style="Panel.TFrame", padding=8)
        panes.add(left, weight=1)
        panes.add(right, weight=1)
        columns = ("name", "uwi", "source", "curves", "x", "y", "status")
        self.well_table = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        headings = {"name": "标准井名", "uwi": "UWI", "source": "来源", "curves": "曲线", "x": "坐标 X", "y": "坐标 Y", "status": "核验"}
        for key in columns:
            self.well_table.heading(key, text=headings[key])
            self.well_table.column(key, width=115 if key not in {"name", "source"} else 170, stretch=True)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.well_table.yview)
        self.well_table.configure(yscrollcommand=scroll.set)
        self.well_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.well_table.bind("<<TreeviewSelect>>", self.table_selected)
        self.map = NativeMap(right)
        self.map.pack(fill="both", expand=True)
        self.map.bind("<<WellSelected>>", self.map_selected)
        self.well_detail = ttk.Label(right, text="选择井后显示资料来源；DEV 仅在勾选时按需读取。", style="Muted.TLabel", wraplength=500, justify="left")
        self.well_detail.pack(fill="x", pady=(8, 1))
        self.pages["井与井名"] = page

    def build_catalog(self) -> None:
        page = ttk.Frame(self.content, padding=18)
        ttk.Label(page, text="资料数据库", style="Heading.TLabel").pack(anchor="w")
        top = ttk.Frame(page)
        top.pack(fill="x", pady=12)
        categories = {"": "全部资料", "well_heads": "Well Head", "well_logs": "Well LAS", "well_paths": "Well DEV", "well_tops": "Well Top", "production": "生产动态", "seismic_3d": "3D 地震", "seismic_2d": "2D 地震", "horizons": "层位", "faults": "断层", "polygons": "Polygon"}
        self.catalog_key = tk.StringVar(value="")
        self.catalog_combo = ttk.Combobox(top, state="readonly", values=list(categories.values()), width=18)
        self.catalog_combo.set("全部资料")
        self.catalog_reverse = {label: key for key, label in categories.items()}
        self.catalog_combo.pack(side="left")
        self.catalog_combo.bind("<<ComboboxSelected>>", lambda _: self.reload_catalog())
        self.catalog_search = ttk.Entry(top)
        self.catalog_search.pack(side="left", fill="x", expand=True, padx=8)
        self.catalog_search.bind("<Return>", lambda _: self.reload_catalog())
        ttk.Button(top, text="检索", command=self.reload_catalog).pack(side="left")
        columns = ("file", "type", "path", "extension", "size", "status")
        self.catalog_table = ttk.Treeview(page, columns=columns, show="headings")
        for key, title, width in zip(columns, ("文件 / 数据对象", "类型", "原始目录", "扩展名", "大小", "状态"), (280, 120, 360, 90, 90, 100)):
            self.catalog_table.heading(key, text=title)
            self.catalog_table.column(key, width=width, stretch=True)
        self.catalog_table.pack(fill="both", expand=True)
        self.catalog_table.bind("<Double-1>", self.open_catalog_file)
        self.catalog_note = ttk.Label(page, text="双击文件可在资源管理器定位本地源文件。", style="Muted.TLabel")
        self.catalog_note.pack(anchor="w", pady=(7, 0))
        self.pages["资料数据库"] = page

    def show_page(self, name: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)

    def try_default_workspace(self) -> None:
        for location in (PROJECT_ROOT / "data", PROJECT_ROOT):
            try:
                self.open_workspace(location, quiet=True)
                return
            except (OSError, ValueError):
                continue

    def choose_workspace(self) -> None:
        location = filedialog.askdirectory(title="选择 .nvt 分析工区", initialdir=str(self.workspace.folder or PROJECT_ROOT))
        if location:
            self.open_workspace(location)

    def open_workspace(self, location: str | Path, quiet: bool = False) -> None:
        try:
            self.workspace.open(location)
            self.workspace_label.configure(text=str(self.workspace.folder))
            self.dashboard_path.configure(text=str(self.workspace.folder))
            self.refresh_dashboard()
            self.load_wells()
            self.reload_catalog()
            self.status.configure(text="工区已打开：原生窗口正直接读取本地 SQLite 与项目快照")
        except (OSError, ValueError, KeyError) as error:
            if not quiet:
                messagebox.showwarning("地数镜", f"打开工区失败：\n{error}")
            raise

    def refresh_dashboard(self) -> None:
        summary = self.workspace.summary()
        notes = {"files": "目录物理文件", "wells": "Well Head / LAS / DEV 等统一", "coordinates": "可二维投影", "las": "口井具备 LAS", "dev": "口井具备 DEV", "seismic": "2D / 3D 地震文件"}
        for key, (value, note) in self.cards.items():
            value.configure(text=number(summary[key]))
            note.configure(text=notes[key])

    def load_wells(self) -> None:
        self.wells = self.workspace.wells()
        self.filter_wells()

    def filter_wells(self) -> None:
        query = self.well_search.get().strip().upper()
        rows = [row for row in self.wells if not query or query in " ".join([str(row.get("canonical_name", "")), str(row.get("uwi", "")), str(row.get("preferred_filename", "")), *row.get("source_types", [])]).upper()]
        self.filtered_wells = rows
        self.well_table.delete(*self.well_table.get_children())
        for row in rows:
            self.well_table.insert("", "end", iid=row["project_key"], values=(row.get("canonical_name"), row.get("uwi") or "—", " / ".join(row.get("source_types", [])), f"{number(row.get('curve_count'))} 种", number(row.get("x"), 2), number(row.get("y"), 2), row.get("identity_status", "—")))
        self.map.set_wells(rows)
        if self.current_key and self.current_key in self.well_table.get_children():
            self.well_table.selection_set(self.current_key)
            self.select_well(self.current_key)

    def table_selected(self, event=None) -> None:
        del event
        selected = self.well_table.selection()
        if selected:
            self.select_well(selected[0])

    def map_selected(self, event=None) -> None:
        del event
        if self.map.selected_key in self.well_table.get_children():
            self.well_table.selection_set(self.map.selected_key)
            self.well_table.see(self.map.selected_key)
        self.select_well(self.map.selected_key)

    def select_well(self, key: str) -> None:
        self.current_key = key
        item = next((row for row in self.wells if row.get("project_key") == key), None)
        if not item:
            return
        self.map.selected_key = key
        source_names = " · ".join(dict.fromkeys(str(row.get("filename") or "") for row in item.get("_sources", []) if row.get("filename")))
        self.well_detail.configure(text=f"{item.get('canonical_name')}　{item.get('identity_status', '—')}\n坐标：{number(item.get('x'), 2)}, {number(item.get('y'), 2)}　曲线：{number(item.get('curve_count'))} 种\n来源文件：{source_names or '—'}")
        self.update_dev()
        self.map.draw()

    def update_dev(self) -> None:
        if not self.show_dev.get() or not self.current_key:
            self.map.set_trajectory([])
            return
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            self.map.set_trajectory(self.workspace.trajectory(self.current_key))
        finally:
            self.configure(cursor="")

    def update_point_size(self) -> None:
        self.map.point_size = int(self.size_var.get())
        self.map.draw()

    def reload_catalog(self) -> None:
        if not self.workspace.ready:
            return
        category = self.catalog_reverse.get(self.catalog_combo.get(), "")
        data = self.workspace.catalog(category, self.catalog_search.get().strip())
        self.catalog_table.delete(*self.catalog_table.get_children())
        for row in data.get("items", []):
            self.catalog_table.insert("", "end", iid=str(row["id"]), values=(row.get("filename"), row.get("category_key"), row.get("relative_path"), row.get("extension"), number(row.get("bytes")), row.get("status")), tags=(row.get("file_path") or "",))
        self.catalog_note.configure(text=f"当前显示 {number(len(data.get('items', [])))} / {number(data.get('total'))} 个对象；双击文件可定位源文件。")

    def open_catalog_file(self, event=None) -> None:
        del event
        selected = self.catalog_table.selection()
        if not selected:
            return
        path = (self.catalog_table.item(selected[0], "tags") or [""])[0]
        try:
            reveal_file(path)
        except OSError as error:
            messagebox.showwarning("地数镜", str(error))


def main() -> int:
    app = DesktopApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
