"""TeleOps W4：Gradio 三 Tab 前台。

直接复用底层能力 + 双 Agent（standalone 模式，不依赖 FastAPI 进程），
可本地 `python app.py` 运行，也可直接部署到 Hugging Face Spaces
（HF 会自动用 `python app.py` 启动，并注入 PORT 环境变量）。

设计要点：
  - 三 Tab：① 运维 Agent 告警根因  ② 研发 Agent 造工具  ③ 闭环看板
  - ensure_data()：若 data/ 缺失则自动造数据，保证 HF 干净 clone 也能跑
  - 离线 Mock 模式无需 API Key；配置 DEEPSEEK_API_KEY 后切换真实大模型
"""
import sys
import os
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gradio as gr

from src.core.cmdb_graph import CMDBGraph
from src.core.kb_store import KBStore
from src.core.tool_registry import ToolRegistry
from src.llm_client import LLMClient
from src.agents.ops_agent import OpsAgent
from src.agents.dev_agent import DevAgent


# ---------------- 确保数据就绪（HF 干净环境也能跑） ----------------
def ensure_data():
    if (ROOT / "data" / "alerts.json").exists():
        return
    try:
        subprocess.run([sys.executable, "scripts/gen_data.py"], cwd=ROOT, check=False)
        subprocess.run([sys.executable, "scripts/ingest_public.py", "--convert",
                        "--dataset", "bgl", "--raw", "data/raw/bgl_sample.log"],
                       cwd=ROOT, check=False)
        subprocess.run([sys.executable, "scripts/ingest_public.py", "--make-kb"],
                       cwd=ROOT, check=False)
    except Exception as e:
        print("ensure_data failed:", e)


ensure_data()

# ---------------- 全局状态（单例） ----------------
cmdb = CMDBGraph()
kb = KBStore()
tools = ToolRegistry()
llm = LLMClient()
llm._ensure_client()  # 启动时即确定真实模式（有 Key→live，无 Key→mock），供 UI 顶部准确显示
ops = OpsAgent(cmdb, kb, tools, llm)
dev = DevAgent(cmdb, kb, llm)


def reload_state():
    """研发造工具/沉淀 SOP 后，重新加载工具库与知识库，让运维 Agent 指向最新实例。"""
    global tools, kb
    tools = ToolRegistry()
    kb = KBStore()
    ops.tools = tools
    ops.kb = kb
    dev.kb = kb


# ---------------- 示例 ----------------
SAMPLE_ALERT = {
    "alert_id": "A-TEMP-DEMO", "ts": "", "source": "zabbix",
    "metric": "temperature", "host": "host-1", "severity": "critical",
    "value": "88C", "message": "物理机 host-1 核心温度过热告警，疑似散热故障",
    "tags": ["compute", "temperature"], "is_noise": False,
}
SAMPLE_FEEDBACK = {
    "feedback_id": "F-AUTO",
    "summary": "运维根因推理需要工具 temperature_probe，但工具库缺失，请研发生成",
}


# ---------------- 渲染工具 ----------------
def md_alert(out: dict) -> str:
    noise = out.get("is_noise", False)
    status = "🔇 噪声告警，已抑制" if noise else "✅ 有效告警"
    lines = [f"### 降噪结果：{status}", ""]
    if not noise:
        diag = out.get("diagnosis", {})
        lines.append("### 根因推理（Top-k 假设）")
        for h in diag.get("hypotheses", []):
            lines.append(f"- **{h.get('cause')}**  （置信度 {h.get('confidence')}）")
            lines.append(f"  - 证据：{h.get('evidence', '')}")
            lines.append(f"  - 建议工具：`{h.get('recommended_tool', '')}`")
            lines.append(f"  - 处置：{h.get('recommended_action', '')}")
        lines.append(f"\n**结论**：{diag.get('conclusion', '')}")
        lines.append("\n### 工具执行")
        for r in out.get("tool_results", []):
            lines.append(f"- `{r.get('tool')}`：{r.get('status')} "
                         f"{r.get('result', r.get('reason', ''))}")
        miss = out.get("missing_tool", "")
        if miss:
            lines.append(f"\n⚠️ **缺失工具 `{miss}`，已触发研发闭环**")
        lines.append("\n### 处置建议")
        for a in out.get("plan", {}).get("actions", []):
            lines.append(f"- {a}")
    return "\n".join(lines)


def md_tool(t: dict) -> str:
    return (f"- **{t.get('name')}** — {t.get('description')}\n"
            f"  - 风险：`{t.get('risk')}` | 参数：{list((t.get('params') or {}).keys())} "
            f"| 脚本：`{t.get('executor')}`")


# ---------------- 处理函数 ----------------
def ops_analyze(metric, host, severity, value, message):
    alert = {
        "alert_id": "UI-ALERT", "ts": "", "source": "ui",
        "metric": metric or "unknown", "host": host or "unknown",
        "severity": severity or "critical", "value": value or "",
        "message": message or "", "tags": [], "is_noise": False,
    }
    out = ops.handle_alert(alert)
    return md_alert(out), out


def ops_chat(question):
    hits = kb.retrieve(question, top_k=3)
    context = "\n".join(f"[{h['source']}] {h['text']}" for h in hits)
    prompt = (f"[TASK:KBQA]\n你是电信云网运维知识助手。仅基于知识库内容回答问题，不要编造。\n"
              f"问题: {question}\n知识库:\n{context}")
    answer = llm.complete(prompt)
    kb_md = "\n".join(f"- [{h['source']}] {h['text'][:160]}" for h in hits)
    return (f"**回答**\n\n{answer}\n\n---\n**检索到的知识片段**\n{kb_md}",
            {"answer": answer, "retrieved": hits})


def dev_build(fid, summary):
    fb = {"feedback_id": fid or "F-UI", "summary": summary or ""}
    if not fb["summary"].strip():
        return "请填写反馈描述", {}
    res = dev.fulfill_feedback(fb)
    reload_state()
    tool = res["tool"]
    md = (f"### 研发 Agent 已生成工具并注册\n"
          f"- 工具名：`{tool.get('name')}`\n"
          f"- 描述：{tool.get('description')}\n"
          f"- 风险：`{tool.get('risk')}`\n"
          f"- 脚本已落盘：`tools/{tool.get('name')}.py`\n"
          f"- SOP 已沉淀：`{Path(res['sop']).name}`\n"
          f"- 工具库现已含 **{len(tools.list_tools())}** 个工具")
    return md, res


def loop_run():
    alert = SAMPLE_ALERT
    out1 = ops.handle_alert(alert)
    missing = out1.get("missing_tool", "")
    md = ["### 闭环第 1 轮：运维处理", md_alert(out1)]
    dev_res = None
    out2 = None
    if missing:
        fb = {"feedback_id": "F-AUTO",
              "summary": f"运维根因推理需要工具 {missing}，但工具库缺失，请研发生成"}
        dev_res = dev.fulfill_feedback(fb)
        reload_state()
        out2 = ops.handle_alert(alert)
        md.append("\n### 闭环第 2 轮：研发造工具后，运维复用")
        md.append(md_alert(out2))
        md.append(f"\n✅ **闭环完成**：研发新增工具 `{missing}`，运维已成功调用。")
    else:
        md.append("\n（本轮未触发工具缺失，无闭环）")
    return "\n".join(md), {
        "round1": out1, "missing_tool": missing,
        "dev_result": dev_res, "round2": out2,
        "tools_now": tools.list_tools(),
    }


def load_alert_example():
    return (SAMPLE_ALERT["metric"], SAMPLE_ALERT["host"], SAMPLE_ALERT["severity"],
            SAMPLE_ALERT["value"], SAMPLE_ALERT["message"])


def load_feedback_example():
    return SAMPLE_FEEDBACK["feedback_id"], SAMPLE_FEEDBACK["summary"]


def list_tools_md():
    ts = tools.list_tools()
    if not ts:
        return "_（工具库为空）_"
    return "\n".join(md_tool(tools.get(t)) for t in ts)


# ---------------- UI ----------------
with gr.Blocks(title="TeleOps 智能体平台") as demo:
    gr.Markdown(f"# 🛰️ TeleOps 智能体平台\n"
                f"**研发数字员工 × 运维 Agent 纵向闭环**（策略 B）  ·  "
                f"当前 LLM 模式：<code>{llm.mode}</code>")
    gr.Markdown("无 GPU、无需 API Key 即可演示（离线 Mock 模式）；配置 `DEEPSEEK_API_KEY` "
                "后自动切换真实大模型，且可无缝替换为 Qwen / GLM（信创叙事点）。")

    with gr.Tabs():
        # ===== Tab 1: 运维 Agent =====
        with gr.Tab("① 运维 Agent · 告警根因"):
            with gr.Row():
                with gr.Column(scale=1):
                    btn_ex = gr.Button("载入示例告警", variant="secondary")
                    metric = gr.Textbox(label="指标 metric", placeholder="如 temperature")
                    host = gr.Textbox(label="主机 host", placeholder="如 host-1")
                    severity = gr.Textbox(label="级别 severity", placeholder="critical/warning/info")
                    value = gr.Textbox(label="数值 value", placeholder="如 88C")
                    message = gr.Textbox(label="告警内容 message", lines=2)
                    btn_run = gr.Button("运行根因分析", variant="primary")
                with gr.Column(scale=2):
                    out_ops_md = gr.Markdown()
                    out_ops_json = gr.JSON(label="原始结构（技术细节）")
            with gr.Accordion("知识库问答（RAG）", open=False):
                q = gr.Textbox(label="问题", placeholder="如 光模块功率异常怎么排查")
                qbtn = gr.Button("问答")
                qout_md = gr.Markdown()
                qout_json = gr.JSON()
            btn_ex.click(load_alert_example, [], [metric, host, severity, value, message])
            btn_run.click(ops_analyze, [metric, host, severity, value, message],
                          [out_ops_md, out_ops_json])
            qbtn.click(ops_chat, [q], [qout_md, qout_json])

        # ===== Tab 2: 研发 Agent =====
        with gr.Tab("② 研发 Agent · 造工具"):
            with gr.Row():
                with gr.Column(scale=1):
                    btn_fex = gr.Button("载入示例反馈", variant="secondary")
                    fid = gr.Textbox(label="反馈工单 ID", placeholder="F-001")
                    fsum = gr.Textbox(label="反馈描述", lines=3,
                                     placeholder="运维根因推理需要工具 xxx，但工具库缺失，请研发生成")
                    btn_frun = gr.Button("提交反馈并造工具", variant="primary")
                with gr.Column(scale=2):
                    out_dev_md = gr.Markdown()
                    out_dev_json = gr.JSON(label="原始结构（技术细节）")
            btn_fex.click(load_feedback_example, [], [fid, fsum])
            btn_frun.click(dev_build, [fid, fsum], [out_dev_md, out_dev_json])

        # ===== Tab 3: 闭环看板 =====
        with gr.Tab("③ 闭环看板"):
            gr.Markdown("一键演示「运维缺工具 → 研发造工具并注册 → 运维复用」完整纵向闭环。")
            btn_loop = gr.Button("▶ 运行完整闭环", variant="primary")
            out_loop_md = gr.Markdown()
            out_loop_json = gr.JSON(label="闭环原始数据")
            gr.Markdown("#### 当前工具库")
            out_tools = gr.Markdown(value=list_tools_md())
            btn_loop.click(loop_run, [], [out_loop_md, out_loop_json]).then(
                lambda: list_tools_md(), [], [out_tools])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
