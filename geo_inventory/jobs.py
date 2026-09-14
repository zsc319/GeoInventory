from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database
from .importers import ImportService


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ImportJobManager:
    def __init__(self):
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        paths: list[str | Path],
        database_path: str | Path,
        options: dict[str, Any],
        process_directory: str | Path | None = None,
    ) -> dict[str, Any]:
        resolved = [Path(path).resolve() for path in paths]
        if not resolved:
            raise ValueError("没有待导入文件")
        missing = [str(path) for path in resolved if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"文件不存在：{missing[0]}")
        job_id = uuid.uuid4().hex
        files = [{"path": str(path), "name": path.name, "bytes": path.stat().st_size, "status": "pending", "progress": 0.0} for path in resolved]
        job = {
            "id": job_id, "kind": "import", "status": "queued", "stage": "等待后台线程",
            "progress": 0.0, "percent": 0, "eta_seconds": None, "current_file": None,
            "created_at": utcnow(), "started_at": None, "finished_at": None,
            "files": files, "results": [], "errors": [], "options": options,
            "_started_monotonic": None, "_process_directory": str(process_directory) if process_directory else None,
        }
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run_import, args=(job_id, Path(database_path), resolved, options), daemon=True, name=f"geo-import-{job_id[:8]}")
        thread.start()
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError("任务不存在或服务已经重启")
            return self._public(job)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = sorted(self._jobs.values(), key=lambda row: row["created_at"], reverse=True)[:limit]
            return [self._public(row) for row in rows]

    def _run_import(self, job_id: str, database_path: Path, paths: list[Path], options: dict[str, Any]) -> None:
        database = Database(database_path)
        importer = ImportService(database)
        weights = [max(path.stat().st_size, 1024 * 1024) for path in paths]
        total_weight = max(1, sum(weights))
        completed_weight = 0
        self._update(job_id, status="running", stage="开始解析", started_at=utcnow(), _started_monotonic=time.monotonic())
        for index, (path, weight) in enumerate(zip(paths, weights)):
            self._update_file(job_id, index, status="running", progress=0.0)
            self._update(job_id, current_file=path.name, stage=f"解析 {path.name}")

            def callback(fraction: float, stage: str) -> None:
                overall = (completed_weight + weight * fraction) / total_weight
                elapsed = max(0.001, time.monotonic() - self._jobs[job_id]["_started_monotonic"])
                eta = elapsed * (1 - overall) / overall if overall > 0.005 else None
                self._update_file(job_id, index, progress=round(fraction, 4))
                self._update(job_id, progress=round(overall, 5), percent=min(99, round(overall * 100)), eta_seconds=round(eta) if eta is not None else None, stage=stage)
                self._persist(job_id)

            try:
                result = importer.import_path(path, progress_callback=callback, **options)
                with self._lock:
                    self._jobs[job_id]["results"].append(result.__dict__)
                self._update_file(job_id, index, status="complete", progress=1.0)
            except Exception as exc:
                with self._lock:
                    self._jobs[job_id]["errors"].append({"filename": path.name, "error": str(exc)})
                self._update_file(job_id, index, status="failed", progress=1.0, error=str(exc))
            completed_weight += weight
            overall = completed_weight / total_weight
            self._update(job_id, progress=overall, percent=round(overall * 100))
        with self._lock:
            errors = self._jobs[job_id]["errors"]
            results = self._jobs[job_id]["results"]
        status = "complete" if results and not errors else "partial" if results else "failed"
        self._update(job_id, status=status, stage="导入完成" if results else "导入失败", progress=1.0, percent=100, eta_seconds=0, current_file=None, finished_at=utcnow())
        self._persist(job_id)

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _update_file(self, job_id: str, index: int, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id]["files"][index].update(values)

    def _persist(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            directory = job.get("_process_directory")
            payload = self._public(job)
        if not directory:
            return
        try:
            target = Path(directory) / "jobs"
            target.mkdir(parents=True, exist_ok=True)
            temporary = target / f"{job_id}.json.tmp"
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target / f"{job_id}.json")
        except OSError:
            pass

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in job.items() if not key.startswith("_")}

