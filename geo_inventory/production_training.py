from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable


Progress = Callable[[float, str], None]
Runner = Callable[[Progress], dict[str, Any]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProductionTrainingManager:
    """Run one CPU-heavy production model away from Flask request handling."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, runner: Runner) -> dict[str, Any]:
        with self._lock:
            active = next((job for job in self._jobs.values() if job["status"] in {"queued", "running"}), None)
            if active:
                return self._public(active)
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id, "kind": "production-clustering", "status": "queued",
                "stage": "等待后台计算服务", "progress": 0.0, "percent": 0,
                "created_at": _utcnow(), "started_at": None, "finished_at": None,
                "elapsed_seconds": 0.0, "result": None, "error": None,
                "_started_monotonic": None,
            }
            self._jobs[job_id] = job
        threading.Thread(
            target=self._run, args=(job_id, runner), daemon=True,
            name=f"production-training-{job_id[:8]}",
        ).start()
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return self._public(job)

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _run(self, job_id: str, runner: Runner) -> None:
        started = time.monotonic()
        self._update(
            job_id, status="running", stage="读取井特征缓存", percent=2, progress=0.02,
            started_at=_utcnow(), _started_monotonic=started,
        )

        def progress(fraction: float, stage: str) -> None:
            fraction = max(0.02, min(0.99, float(fraction)))
            self._update(
                job_id, stage=str(stage), progress=round(fraction, 4),
                percent=round(fraction * 100), elapsed_seconds=round(time.monotonic() - started, 2),
            )

        try:
            result = runner(progress)
            self._update(
                job_id, status="complete", stage="训练与质量评价完成", progress=1.0,
                percent=100, result=result, finished_at=_utcnow(),
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
        except Exception as exc:
            self._update(
                job_id, status="failed", stage="训练失败", error=str(exc),
                finished_at=_utcnow(), elapsed_seconds=round(time.monotonic() - started, 3),
            )

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in job.items() if not key.startswith("_")}
