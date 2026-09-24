"""核心数据端点：CMDB 拓扑 / 工具库 / 工具调用 / 知识库检索。

从 server.py 抽出（D2 演进式拆分 R1）。tools/kb 等单例经 `s.` 引用，
始终取到当前实例（含闭环后热重载的新对象）。
"""
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.core.data_files import load_topology
from src.api.context import ctx as s

router = APIRouter()

ToolCallReq = s.ToolCallReq


@router.get("/topology")
def topology():
    return load_topology()


@router.get("/tools")
def list_tools():
    return {"tools": [s.tools.get(t) for t in s.tools.list_tools()]}


@router.post("/tools/call")
def call_tool(req: ToolCallReq):
    try:
        return {"result": s.tools.call(req.name, req.params)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/knowledge")
def knowledge(q: str, top_k: int = 3):
    hits = s.kb.retrieve(q, top_k=top_k)
    return {"query": q, "hits": hits}
