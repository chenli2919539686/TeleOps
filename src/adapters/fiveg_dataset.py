"""5G 公开数据集（uccmisl/5Gdataset）加载器 —— 真实电信数据接入的落地点（P0①）。

uccmisl/5Gdataset 是从爱尔兰某运营商真实 5G 网用 G-NetTrack Pro 采集的**客户端侧
被动 KPI 时序**（166 个 CSV，按 应用×移动性 分目录）。CSV 列（实测）：

  Timestamp,Longitude,Latitude,Speed,Operatorname,CellID,NetworkMode,
  RSRP,RSRQ,SNR,CQI,RSSI,DL_bitrate,UL_bitrate,State,
  PINGAVG,PINGMIN,PINGMAX,PINGSTDEV,PINGLOSS,
  CELLHEX,NODEHEX,LACHEX,RAWCELLID,NRxRSRP,NRxRSRQ

关键点（决定映射正确性）：
  - 这是 **UE 侧 KPI**，不是带根因标签的告警事件——所以它用于验证
    「真实 5G KPI → 统一 Alert → 运维 Agent 根因」整条管线；根因 ground truth
    需我们合成注入故障（见 backlog ② 量化指标）。
  - RSRP/RSRQ/SNR/RSSI/CQI/吞吐 都是 **越低（越负）越差**，而通用告警阈值逻辑
    默认「越高越差」（如丢包率）。故本加载器按指标方向分别定义阈值边界。

本模块只负责「数据集行 → 适配器原始 payload（含已算好的 severity）」，
severity 映射与统一 Alert 封装仍由 FiveGKpiAdapter 完成，保持单一职责。
"""
import csv
import datetime
import glob as _glob
import os
from typing import Any, Dict, Iterator, List, Optional

# ---------------------------------------------------------------------------
# 指标规格表：列名容错 + 方向 + 阈值边界
#   direction:
#     lower_worse  -> 值越低越差（RSRP/RSRQ/SNR/吞吐/CQI/RSSI，负值或越小越糟）
#     higher_worse -> 值越高越差（丢包率等）
#   边界 good/warn/major/critical 为经验默认值，可被 adapters.json[alert-5g].thresholds 覆盖。
# ---------------------------------------------------------------------------
METRIC_SPECS: Dict[str, Dict[str, Any]] = {
    "rsrp_dbm":          {"col": "RSRP",        "direction": "lower_worse",
                          "good": -100.0, "warn": -110.0, "major": -115.0, "critical": -120.0, "unit": "dBm"},
    "rsrq_db":           {"col": "RSRQ",        "direction": "lower_worse",
                          "good": -12.0,  "warn": -15.0,  "major": -18.0,  "critical": -21.0,  "unit": "dB"},
    "sinr_db":           {"col": "SNR",         "direction": "lower_worse",
                          "good": 5.0,    "warn": 0.0,    "major": -3.0,   "critical": -6.0,   "unit": "dB"},
    "dl_throughput_mbps":{"col": "DL_bitrate",  "direction": "lower_worse",
                          "good": 50.0,   "warn": 30.0,   "major": 15.0,   "critical": 5.0,    "unit": "Mbps"},
    "ul_throughput_mbps":{"col": "UL_bitrate",  "direction": "lower_worse",
                          "good": 10.0,   "warn": 5.0,    "major": 2.0,    "critical": 1.0,    "unit": "Mbps"},
    "cqi":               {"col": "CQI",         "direction": "lower_worse",
                          "good": 12.0,   "warn": 8.0,    "major": 5.0,    "critical": 3.0,    "unit": ""},
    "rssi_dbm":          {"col": "RSSI",        "direction": "lower_worse",
                          "good": -90.0,  "warn": -95.0,  "major": -100.0, "critical": -105.0, "unit": "dBm"},
    "ping_loss_pct":     {"col": "PINGLOSS",    "direction": "higher_worse",
                          "good": 0.0,    "warn": 1.0,    "major": 3.0,    "critical": 5.0,    "unit": "%"},
}

# 严重度排序（用于 min_severity 过滤）
_SEV_RANK = {"info": 0, "warning": 1, "major": 2, "critical": 3}

# 列名别名容错（不同导出/大小写差异都能识别）
_COLUMN_ALIASES = {
    "rsrp": "RSRP", "rsrq": "RSRQ", "snr": "SNR", "sinr": "SNR",
    "cqi": "CQI", "rssi": "RSSI",
    "dl_bitrate": "DL_bitrate", "dl bitrate": "DL_bitrate", "downlink": "DL_bitrate",
    "ul_bitrate": "UL_bitrate", "ul bitrate": "UL_bitrate", "uplink": "UL_bitrate",
    "cellid": "CellID", "cell_id": "CellID", "cell": "CellID",
    "rawcellid": "RAWCELLID", "operatorname": "Operatorname", "operator": "Operatorname",
    "timestamp": "Timestamp", "ts": "Timestamp",
    "pingloss": "PINGLOSS", "packet_loss": "PINGLOSS",
}


def _norm_col(name: str) -> str:
    return _COLUMN_ALIASES.get(name.strip().lower(), name.strip())


def _parse_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip().replace(",", ".")
    if s in ("", "-", "nan", "na", "n/a", "none"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_ts(val: Any) -> str:
    """把 G-NetTrack 的 '2019.12.16_13.40.04' 规整为 ISO 字符串。"""
    s = str(val or "").strip()
    if not s or s in ("-",):
        return ""
    for fmt in ("%Y.%m.%d_%H.%M.%S", "%Y-%m-%d_%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return s  # 规整失败就原样返回


def _severity_for(value: float, spec: Dict[str, Any]) -> Optional[str]:
    """按方向返回 severity；value 在 good 边界内返回 None（不告警）。"""
    if value is None:
        return None
    d = spec["direction"]
    if d == "lower_worse":
        if value <= spec["critical"]:
            return "critical"
        if value <= spec["major"]:
            return "major"
        if value <= spec["warn"]:
            return "warning"
        return None
    # higher_worse
    if value >= spec["critical"]:
        return "critical"
    if value >= spec["major"]:
        return "major"
    if value >= spec["warn"]:
        return "warning"
    return None


def iter_rows(path: str, limit: Optional[int] = None,
              aliases: Optional[Dict[str, str]] = None) -> Iterator[Dict[str, str]]:
    """遍历数据集（文件 / 目录 / glob）下的 CSV 行。自动跳过 __MACOSX 与 .DS_Store。

    path 支持：单个 .csv、一个目录（递归 **/*.csv）、或 glob 表达式。
    返回每行原始 dict（键已规整为规范列名）。

    aliases：可选，列名映射覆盖（raw 小写 → 规范列名），用于接不同厂商 OSS 导出
    （如华为/中兴/爱立信的列名差异）。与内置 _COLUMN_ALIASES 合并，覆盖优先。
    例：{"lte_rsrp": "RSRP", "enodeb_id": "CellID"}。
    """
    amap = dict(_COLUMN_ALIASES)
    if aliases:
        amap.update({str(k).strip().lower(): str(v).strip() for k, v in aliases.items()})

    files: List[str] = []
    if os.path.isdir(path):
        files = sorted(_glob.glob(os.path.join(path, "**", "*.csv"), recursive=True))
    elif os.path.isfile(path):
        files = [path]
    else:
        files = sorted(_glob.glob(path, recursive=True))
    files = [f for f in files if "__MACOSX" not in f and not f.endswith(".DS_Store")]
    count = 0
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                # 规整表头（用合并后的别名表）
                reader.fieldnames = [amap.get(c.strip().lower(), c.strip())
                                    for c in (reader.fieldnames or [])]
                for row in reader:
                    if limit is not None and count >= limit:
                        return
                    count += 1
                    yield row
        except Exception:
            # 单行/单文件解析失败不影响整体回放
            continue


def row_to_payloads(row: Dict[str, Any], specs: Optional[Dict[str, Dict[str, Any]]] = None,
                    min_severity: str = "info") -> List[Dict[str, Any]]:
    """单行 CSV → 0..n 条适配器原始 payload（含已算好的 severity 与 message）。

    payload 形状与 FiveGKpiAdapter.parse_webhook 兼容：
      {cell, metric, value, threshold, ts, severity, unit, source, message, tags}
    parse_webhook 会原样采用显式 severity/message，不再反比阈值。

    min_severity：严重度下限（默认 info=全部）。真实数据集噪声大，回放/接入时
    常用 "major" 过滤掉 warning，避免把健康波动当成告警刷屏（即「噪声抑制」）。
    """
    specs = specs or METRIC_SPECS
    floor = _SEV_RANK.get(min_severity, 0)
    cell = str(row.get("CellID") or row.get("RAWCELLID") or "").strip()
    if not cell:
        cell = "unknown-cell"
    ts = _parse_ts(row.get("Timestamp"))
    out: List[Dict[str, Any]] = []
    for metric, spec in specs.items():
        raw_val = row.get(spec["col"])
        value = _parse_float(raw_val)
        if value is None:
            continue
        severity = _severity_for(value, spec)
        if severity is None:
            continue  # 健康样本不进告警流
        if _SEV_RANK.get(severity, 0) < floor:
            continue  # 低于严重度下限，抑制
        # 触发边界（用于 message 措辞）
        breach = spec["warn"]
        if severity == "major":
            breach = spec["major"]
        elif severity == "critical":
            breach = spec["critical"]
        if spec["direction"] == "lower_worse":
            msg = (f"小区 {cell} 的 {metric}={value}{spec['unit']} 低于阈值 {breach}{spec['unit']}"
                   f"（{severity}）")
        else:
            msg = (f"小区 {cell} 的 {metric}={value}{spec['unit']} 超过阈值 {breach}{spec['unit']}"
                   f"（{severity}）")
        out.append({
            "alert_id": f"5g-{cell}-{metric}".replace(" ", "_"),
            "cell": cell,
            "host": cell,
            "metric": metric,
            "value": value,
            "threshold": breach,
            "ts": ts,
            "severity": severity,
            "unit": spec["unit"],
            "source": "5g-kpi",
            "message": msg,
            "tags": ["5g", "kpi", metric],
            "is_noise": False,
        })
    return out


def load_payloads(path: str, limit: Optional[int] = None,
                  specs: Optional[Dict[str, Dict[str, Any]]] = None,
                  min_severity: str = "info",
                  aliases: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """便利函数：整个数据集 → 全部告警 payload（按行序）。

    aliases：列名映射覆盖（见 iter_rows），用于接不同厂商 OSS 导出。
    """
    out: List[Dict[str, Any]] = []
    for row in iter_rows(path, limit=limit, aliases=aliases):
        out.extend(row_to_payloads(row, specs, min_severity=min_severity))
    return out
