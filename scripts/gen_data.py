"""生成 TeleOps 中【自造】的数据集（平台特有、无公开对应物）。

分工：本脚本只生成自造部分；真实公开数据由 scripts/ingest_public.py 负责。
  - topology.json  CMDB 拓扑（节点 + 依赖边）—— 自造
  - feedback.json  运维反馈工单（含一条"无工具"触发闭环）—— 自造
  - tools.json     工具库注册表（引用 tools/ 下脚本）—— 自造

真实公开数据（由 ingest_public.py 另行生成，不要本脚本覆盖）：
  - alerts.json   从 LogHub 真实日志转换
  - kb/*.md       从 MITRE ATT&CK / SRE 公开知识生成

纯标准库，无需任何第三方依赖。
"""
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def gen_topology():
    nodes = [
        {"id": "svc-order", "type": "service", "name": "订单服务", "domain": "B域"},
        {"id": "svc-billing", "type": "service", "name": "计费服务", "domain": "B域"},
        {"id": "db-order", "type": "database", "name": "订单库"},
        {"id": "db-billing", "type": "database", "name": "计费库"},
        {"id": "mq-1", "type": "middleware", "name": "RabbitMQ"},
        {"id": "sw-core", "type": "network", "name": "核心交换机"},
        {"id": "sw-access", "type": "network", "name": "接入交换机"},
        {"id": "host-1", "type": "host", "name": "物理机A"},
        {"id": "host-2", "type": "host", "name": "物理机B"},
        {"id": "olt-1", "type": "network", "name": "OLT设备"},
        {"id": "onu-1", "type": "network", "name": "ONU终端"},
    ]
    edges = [
        {"from": "svc-order", "to": "db-order", "rel": "读写"},
        {"from": "svc-order", "to": "mq-1", "rel": "依赖"},
        {"from": "svc-billing", "to": "db-billing", "rel": "读写"},
        {"from": "svc-billing", "to": "mq-1", "rel": "依赖"},
        {"from": "host-1", "to": "sw-access", "rel": "上联"},
        {"from": "host-2", "to": "sw-access", "rel": "上联"},
        {"from": "sw-access", "to": "sw-core", "rel": "上联"},
        {"from": "db-order", "to": "host-1", "rel": "部署于"},
        {"from": "db-billing", "to": "host-2", "rel": "部署于"},
        {"from": "olt-1", "to": "sw-core", "rel": "上联"},
        {"from": "onu-1", "to": "olt-1", "rel": "上联"},
    ]
    return {"nodes": nodes, "edges": edges}


def gen_feedback():
    return {"feedbacks": [
        {
            "feedback_id": "F-001", "from": "ops-agent", "type": "new_failure_pattern",
            "summary": "出现新型告警:光模块发光功率异常(onu-1),现有工具库无对应探测工具,需研发补充 optical_power 探测工具",
            "assigned_to": "dev-agent", "status": "todo",
        },
        {
            "feedback_id": "F-002", "from": "ops-agent", "type": "knowledge_gap",
            "summary": "核心交换机端口拥塞引发级联,处置 SOP 缺失,需补充网络拥塞应急手册",
            "assigned_to": "dev-agent", "status": "todo",
        },
    ]}


def gen_tools():
    return {"tools": [
        {
            "name": "ping_host",
            "description": "对指定主机执行 ping 探测连通性",
            "params": {"host": {"type": "string"}},
            "executor": "tools/net_ping.py",
            "owner_agent": "dev",
            "risk": "low",
        },
        {
            "name": "restart_service",
            "description": "重启指定服务(高危)",
            "params": {"service": {"type": "string"}},
            "executor": "tools/svc_restart.py",
            "owner_agent": "dev",
            "risk": "high",
            "require_human_approval": True,
        },
    ]}


def main():
    (DATA_DIR / "topology.json").write_text(json.dumps(gen_topology(), ensure_ascii=False, indent=2))
    (DATA_DIR / "feedback.json").write_text(json.dumps(gen_feedback(), ensure_ascii=False, indent=2))
    (DATA_DIR / "tools.json").write_text(json.dumps(gen_tools(), ensure_ascii=False, indent=2))
    print("已生成【自造】数据到", DATA_DIR)
    print("  - topology.json (CMDB 拓扑, 自造)")
    print("  - feedback.json (运维反馈工单, 自造)")
    print("  - tools.json    (工具库注册表, 自造)")
    print("提示: alerts.json 与 kb/*.md 由 scripts/ingest_public.py 用真实公开数据生成")


if __name__ == "__main__":
    main()
