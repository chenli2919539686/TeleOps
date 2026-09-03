"""把公开数据集转换成 TeleOps 的 告警库(alerts.json) 与 知识库(kb/*.md)。

分工（与 gen_data.py 互补）：
  - gen_data.py  -> 自造数据：topology.json / tools.json / feedback.json（平台特有，无公开对应物）
  - ingest_public.py -> 真实公开数据：alerts.json（LogHub 日志）/ kb/*.md（MITRE ATT&CK + SRE 复盘）

已核实的真实数据源（logpai/logparser 仓库，main 分支，2k 样本日志）：
  HDFS   : https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/HDFS/HDFS_2k.log
  BGL    : https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/BGL/BGL_2k.log
  Apache : https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/Apache/Apache_2k.log
  Linux  : https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/Linux/Linux_2k.log
  OpenSSH: https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/OpenSSH/OpenSSH_2k.log

用法（在你自己联网的机器上）：
  # 1) 下载真实 2k 日志到 data/raw/
  python scripts/ingest_public.py --download

  # 2) 把真实日志转成 alerts.json（项目自带样本可直接跑，无需联网）
  python scripts/ingest_public.py --convert --dataset bgl --raw data/raw/bgl_sample.log
  python scripts/ingest_public.py --convert --dataset hdfs --raw data/raw/hdfs_sample.log

  # 3) 生成真实公开知识库（MITRE ATT&CK 摘要 + SRE 复盘范式）
  python scripts/ingest_public.py --make-kb

纯标准库，无需任何第三方依赖。
"""
import argparse
import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
KB_DIR = ROOT / "kb"
DATA_DIR.mkdir(exist_ok=True)
RAW_DIR.mkdir(exist_ok=True)
KB_DIR.mkdir(exist_ok=True)

# 已核实的真实数据源
SOURCES = {
    "hdfs": "https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/HDFS/HDFS_2k.log",
    "bgl": "https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/BGL/BGL_2k.log",
    "apache": "https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/Apache/Apache_2k.log",
    "linux": "https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/Linux/Linux_2k.log",
    "openssh": "https://raw.githubusercontent.com/logpai/logparser/main/data/loghub_2k/OpenSSH/OpenSSH_2k.log",
}

SEV_MAP = {
    "INFO": "info", "DEBUG": "info", "NOTICE": "info",
    "WARN": "warning", "WARNING": "warning",
    "ERROR": "critical", "ERR": "critical",
    "FATAL": "critical", "SEVERE": "critical", "CRITICAL": "critical",
}


# --------------------------------------------------------------------------- #
# 1) 下载真实 2k 日志
# --------------------------------------------------------------------------- #
def download():
    for name, url in SOURCES.items():
        out = RAW_DIR / f"{name}_2k.log"
        print(f"下载 {name} -> {out}")
        try:
            urllib.request.urlretrieve(url, out)
            print(f"  OK ({out.stat().st_size} bytes)")
        except Exception as e:
            print(f"  失败: {e}（请在联网环境运行，或手动下载放入 data/raw/）")


# --------------------------------------------------------------------------- #
# 2) 日志 -> alerts.json 转换
# --------------------------------------------------------------------------- #
def _hdfs_host(msg):
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", msg)
    return m.group(1) if m else None


def parse_hdfs(line):
    m = re.match(r"^(\d{6})\s+(\d{6})\s+(\d+)\s+(INFO|WARN|ERROR|FATAL)\s+([\w.$]+):\s+(.*)$", line)
    if not m:
        return None
    yymmdd, hhmmss, _ms, level, component, message = m.groups()
    yy, mm, dd = yymmdd[:2], yymmdd[2:4], yymmdd[4:6]
    ts = f"20{yy}-{mm}-{dd}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"
    host = _hdfs_host(message) or component
    metric = "hdfs_" + component.split(".")[-1].lower()
    return {
        "ts": ts,
        "source": "loghub_hdfs",
        "metric": metric,
        "host": host,
        "severity": SEV_MAP.get(level, "info"),
        "value": level,
        "message": f"[{component}] {message}",
        "tags": ["hdfs", "log"],
        "is_noise": level == "INFO",
    }


def parse_bgl(line):
    s = line.rstrip("\n")
    if not s.strip():
        return None
    fields = s.split()
    if "RAS" not in fields:
        return None
    r = fields.index("RAS")
    try:
        node = fields[r - 1]          # 节点（RAS 前紧邻的节点 token）
        sub = fields[r + 1]           # 子系统 KERNEL/APP
        lvl = fields[r + 2]           # 级别 INFO/FATAL...
        msg = " ".join(fields[r + 3:])
    except IndexError:
        return None
    # 时间：第一个 YYYY.MM.DD 字段 + 第一个 YYYY-MM-DD 字段
    date_f = next((f for f in fields if re.match(r"\d{4}\.\d{2}\.\d{2}", f)), None)
    ts_f = next((f for f in fields if re.match(r"\d{4}-\d{2}-\d{2}", f)), None)
    if date_f and ts_f:
        ts = ts_f.replace("-", "T", 1).split(".")[0]
    else:
        ts = datetime.now().isoformat(timespec="seconds")
    return {
        "ts": ts,
        "source": "loghub_bgl",
        "metric": f"bgl_{sub.lower()}_{lvl.lower()}",
        "host": node,
        "severity": SEV_MAP.get(lvl, "info"),
        "value": lvl,
        "message": msg,
        "tags": ["bgl", "ras", sub.lower()],
        "is_noise": lvl == "INFO",
    }


def convert(dataset, raw_path):
    raw_path = Path(raw_path)
    if not raw_path.exists():
        print(f"找不到原始日志: {raw_path}")
        return
    parser = parse_hdfs if dataset == "hdfs" else parse_bgl
    alerts, total, real = [], 0, 0
    seq = 1
    for line in raw_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        rec = parser(line)
        if not rec:
            continue
        total += 1
        rec["alert_id"] = f"A-{9000 + seq}"
        seq += 1
        if not rec["is_noise"]:
            real += 1
        alerts.append(rec)
    out = DATA_DIR / "alerts.json"
    out.write_text(json.dumps({"alerts": alerts}, ensure_ascii=False, indent=2))
    print(f"已生成 {out}：共 {total} 条，其中真实告警(非噪声) {real} 条，噪声 {total - real} 条")
    print(f"  数据源: {dataset} / {raw_path.name}")


# --------------------------------------------------------------------------- #
# 3) 真实公开知识库（MITRE ATT&CK + SRE 复盘范式）
# --------------------------------------------------------------------------- #
MITRE_MD = """# MITRE ATT&CK 威胁技术与检测要点（公开知识库摘要）

> 来源：MITRE ATT&CK® (https://attack.mitre.org)。公开知识，用于安全运营知识库检索与告警解读。
> 注：以下为各技术的公开描述与通用检测/缓解思路，非运营商内部规则。

## T1078 Valid Accounts（有效账户）
攻击者使用合法凭证登录系统，绕过认证机制。
- 检测：同一账户异常时段/异地登录、特权账户执行非常用操作、登录后无后续业务行为。
- 缓解：强制 MFA、最小权限、凭证泄露监测、定期轮换。

## T1190 Exploit Public-Facing Application（利用面向公网的应用）
攻击者利用暴露在公网的应用/服务漏洞（如 Web 漏洞、反序列化）进行初始入侵。
- 检测：WAF/IPS 告警、异常请求路径与载荷、非常用 UA 与扫描特征。
- 缓解：及时打补丁、边界防护、输入输出校验、RASP。

## T1059 Command and Scripting Interpreter（命令与脚本解释器）
攻击者借助系统解释器（PowerShell、Shell、Python）执行恶意逻辑。
- 检测：可疑父子进程链、无文件执行、命令行含编码/下载行为。
- 缓解：限制解释器权限、启用脚本块日志、EDR。

## T1071 Application Layer Protocol（应用层协议）
攻击者借用 HTTP/DNS/TLS 等合法协议与 C2 通信，隐匿流量。
- 检测： beaconing 周期性外联、DNS 隧道特征、异常 JA3/JA4 指纹。
- 缓解：出网白名单、TLS 解密审计、NDR 监测。

## T1486 Data Encrypted for Impact（数据加密以造成影响 / 勒索）
攻击者加密数据以勒索或破坏可用性。
- 检测：批量文件重命名/加密、大量读写、影子副本删除。
- 缓解：离线备份、段隔离、权限收敛。

## T1055 Process Injection（进程注入）
攻击者将代码注入合法进程以隐藏并提权。
- 检测：异常内存分配、合法进程产生异常子进程、跨进程句柄。
- 缓解：启用攻击面防护（ASR）、EDR、最小权限。

## T1070 Indicator Removal（指标清除 / 日志篡改）
攻击者清理日志与痕迹以阻碍溯源。
- 检测：日志突然中断、审计服务异常停止、时间被回拨。
- 缓解：日志集中转发（SIEM）、只读归档、WORM 存储。

## T1566 Phishing（钓鱼）
攻击者通过伪造邮件/页面诱导凭据或载荷。
- 检测：可疑发件域、含宏文档、仿冒登录页。
- 缓解：邮件网关、员工意识、条件访问。
"""

SRE_MD = """# SRE 故障复盘（Postmortem）范式与公开案例参考

> 来源：Google SRE Book 公开方法论 + 公开披露的厂商故障（用于运维知识库检索）。
> 用于指导"告警根因分析 → 复盘 → SOP 沉淀"的闭环。

## 标准复盘文档结构
1. 摘要（Summary）：一句话说明发生了什么、影响面。
2. 影响（Impact）：受影响服务、用户数、时长、SLA 损失。
3. 时间线（Timeline）：从最早征兆到恢复的关键节点。
4. 根因（Root Cause）：直接原因 + 深层原因（5 Whys）。
5. 修复（Resolution）：临时止血与永久修复。
6. 预防（Action Items）：监控补全、容量/限流、演练、架构改进。

## 公开案例参考（用于理解"故障如何发生"）
- AWS S3 2017-02-28 大范围不可用：一次维护命令中输错参数，移除的服务器数量超出预期，
  导致索引/子系统受损，连带大量依赖 S3 的服务受损。教训：高风险运维命令需限速与校验。
- GitHub 2018-10-21 大规模 DDoS：峰值约 1.35 Tbps 的 memcached 放大攻击，
  为当时公开记录中最大规模之一。教训：暴露的 UDP 服务需禁用或限源。

## 与本项目闭环的映射
- 运维 Agent 收到告警 → 查 CMDB 拓扑定位爆炸半径 → 检索本知识库匹配处置 SOP。
- 遇到"无 SOP/新模式" → 写 feedback.json → 研发 Agent 生成新工具/补充本知识库。
"""


def make_kb():
    (KB_DIR / "mitre_attack_techniques.md").write_text(MITRE_MD, encoding="utf-8")
    (KB_DIR / "sre_postmortem_patterns.md").write_text(SRE_MD, encoding="utf-8")
    print(f"已生成真实公开知识库到 {KB_DIR}/")
    print("  - mitre_attack_techniques.md (MITRE ATT&CK 技术摘要)")
    print("  - sre_postmortem_patterns.md (SRE 复盘范式与公开案例)")


def main():
    ap = argparse.ArgumentParser(description="TeleOps 公开数据接入")
    ap.add_argument("--download", action="store_true", help="下载真实 2k 日志到 data/raw/")
    ap.add_argument("--convert", action="store_true", help="把真实日志转成 alerts.json")
    ap.add_argument("--dataset", default="bgl", choices=["hdfs", "bgl", "apache", "linux", "openssh"])
    ap.add_argument("--raw", default=None, help="原始日志路径（默认 data/raw/<dataset>_sample.log）")
    ap.add_argument("--make-kb", action="store_true", help="生成真实公开知识库")
    args = ap.parse_args()

    if args.download:
        download()
    if args.convert:
        raw = args.raw or str(RAW_DIR / f"{args.dataset}_sample.log")
        convert(args.dataset, raw)
    if args.make_kb:
        make_kb()
    if not (args.download or args.convert or args.make_kb):
        ap.print_help()


if __name__ == "__main__":
    main()
