from __future__ import annotations

import argparse
from pathlib import Path

from geo_inventory.project_scan import build_project_snapshot, save_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描地质项目目录并生成代表性数据快照")
    parser.add_argument("root", help="项目数据根目录")
    parser.add_argument("--output", default="data/project_snapshot.json", help="快照 JSON 输出路径")
    parser.add_argument("--seismic-3d", help="指定 3D SEG-Y 代表文件")
    parser.add_argument("--seismic-2d", help="指定 2D SEG-Y 代表文件")
    parser.add_argument("--surface", help="指定 Petrel PTD Surface 代表文件")
    parser.add_argument("--horizon-2d", help="指定 2D Horizon 代表文件")
    parser.add_argument("--fault", help="指定 Fault sticks 代表文件")
    parser.add_argument("--header-only-surface", action="store_true", help="Surface 只读头，不统计有效网格值")
    args = parser.parse_args()
    snapshot = build_project_snapshot(
        args.root,
        seismic_3d_path=args.seismic_3d,
        seismic_2d_path=args.seismic_2d,
        surface_path=args.surface,
        horizon_2d_path=args.horizon_2d,
        fault_path=args.fault,
        scan_surface_values=not args.header_only_surface,
    )
    output = save_snapshot(snapshot, Path(args.output).resolve())
    project = snapshot["project"]
    print(f"扫描完成：{project['total_files']} 个文件 / {project['total_gb']} GB")
    print(f"项目快照：{output}")


if __name__ == "__main__":
    main()

