"""可观测性端点：trace 列表 / 异步任务轮询。

从 server.py 抽出（D2 演进式拆分 R1）。任务状态（_jobs / _gc_jobs）在 server 模块，
经 `s.` 引用。
"""
from pathlib import Path

from fastapi import APIRouter

from src.config import TRACE_DIR
from src.api.context import ctx as s

router = APIRouter()


@router.get("/traces")
def traces():
    files = sorted(Path(TRACE_DIR).glob("*.json"))
    return {"traces": [f.name for f in files]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    """轮询异步任务进度（前端据此让作战室状态灯实时刷新）。"""
    s._gc_jobs()   # 顺带清理过期任务，避免内存泄漏
    return s._jobs.get(job_id, {"status": "not_found"})
