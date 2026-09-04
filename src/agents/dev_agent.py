"""研发 Agent：把运维的反馈变成"可复用工具 + 知识沉淀"。

职责（对应岗位 B 第 3 条：推动数字员工/智能助手落地）：
  - CodeGen：读反馈工单，自动生成工具脚本（tools/*.py，含 run(params)）并注册进 tools.json
  - ChangeOrder：把处置方案生成变更单
  - KBWriter：把故障复盘沉淀为 SOP，写入 kb/ 供后续知识检索
"""
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import TOOLS_REGISTRY_FILE, KB_DIR, TOOLS_DIR
from src.llm_client import extract_json


class DevAgent:
    def __init__(self, cmdb, kb, llm):
        self.cmdb = cmdb
        self.kb = kb
        self.llm = llm

    # ---------- 1. 生成工具并落盘 + 注册 ----------
    def generate_tool(self, feedback: dict) -> dict:
        summary = feedback.get("summary", "")
        prompt = (
            "[TASK:CODEGEN]\n"
            f"你是研发数字员工。根据运维反馈生成一个新的运维探测工具。\n"
            f"反馈: {summary}\n"
            f"输出 JSON: {{name, description, risk, params, code}}，"
            f"code 必须是合法 Python，定义 def run(params: dict) 返回 dict，"
            f"整体代码控制在 30 行以内、只做探测与返回结果，不要额外依赖。"
        )
        raw = self.llm.complete(prompt)
        data = extract_json(raw)
        name = data.get("name", "auto_tool")
        # 过滤非法文件名
        name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        code = data.get("code", "def run(params):\n    return {'status':'ok'}\n")
        # 落盘工具脚本
        tool_path = TOOLS_DIR / f"{name}.py"
        tool_path.write_text(code, encoding="utf-8")
        tool = {
            "name": name,
            "description": data.get("description", "研发 Agent 自动生成工具"),
            "params": data.get("params", {"host": {"type": "string"}}),
            "executor": f"tools/{name}.py",
            "owner_agent": "dev",
            "risk": data.get("risk", "low"),
        }
        return tool

    def register_tool(self, tool: dict):
        # 元数据写入 SQLite（tools 表），executor 脚本仍落盘 tools/<name>.py
        from src.core.tool_registry import ToolRegistry
        ToolRegistry().add(tool)
        return tool

    # ---------- 2. 生成变更单 ----------
    def change_order(self, diagnosis: dict) -> str:
        prompt = (
            "[TASK:CHANGEORDER]\n"
            f"根据根因结论生成变更单（Markdown）：\n"
            f"{json.dumps(diagnosis, ensure_ascii=False)}"
        )
        return self.llm.complete(prompt)

    # ---------- 3. 沉淀 SOP 进知识库 ----------
    def _gen_sop_text(self, feedback: dict, tool_name: str) -> str:
        """只生成 SOP 文本（可与造工具并行，工具名取自反馈中的预期名）。"""
        summary = feedback.get("summary", "")
        prompt = (
            "[TASK:SOP]\n"
            f"根据运维反馈与已生成工具 {tool_name}，写一份故障处置 SOP（Markdown）。"
            f"全文控制在 600 字以内：只保留现象、排查步骤（编号列表）、升级条件三节。\n"
            f"反馈: {summary}"
        )
        return self.llm.complete(prompt)

    def _write_sop_file(self, sop_text: str, tool_name: str) -> str:
        KB_DIR.mkdir(exist_ok=True)
        sop_path = KB_DIR / f"sop_{tool_name or 'auto'}.md"
        sop_path.write_text(sop_text, encoding="utf-8")
        return str(sop_path)

    def write_sop(self, feedback: dict, tool: dict) -> str:
        """兼容入口：串行生成 + 落盘（新链路走 _gen_sop_text 并行版）。"""
        return self._write_sop_file(
            self._gen_sop_text(feedback, tool.get("name", "auto")),
            tool.get("name", "auto"))

    # ---------- 对外：消费一条反馈，端到端产出工具+知识 ----------
    def fulfill_feedback(self, feedback: dict) -> dict:
        # 造工具与写 SOP 并行：两次 LLM 调用互不依赖，SOP 用反馈中预期的
        # 工具名（派发层 raise_requirement 已明确 needed_tool），省一半等待。
        m = re.search(r"工具\s+([A-Za-z_][A-Za-z0-9_]*)", feedback.get("summary", ""))
        expected = m.group(1) if m else "auto"
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_tool = ex.submit(self.generate_tool, feedback)
            f_sop = ex.submit(self._gen_sop_text, feedback, expected)
            tool = f_tool.result()
            sop_text = f_sop.result()
        self.register_tool(tool)
        sop_path = self._write_sop_file(sop_text, tool.get("name"))
        return {"feedback_id": feedback.get("feedback_id"), "tool": tool, "sop": sop_path}
