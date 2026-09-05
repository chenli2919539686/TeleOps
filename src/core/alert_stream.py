"""模拟告警流水线：让演示像真实监控一样"持续运转，永不停摆"。

背景问题：data/alerts.json 是静态样本，工作台一次只人工分析一条；真实故障只有
3 条，处理完 Agent 就闲置——演示缺"告警持续涌入 → Agent 持续处置"的动态性。

本模块把静态样本变成一条「时间轴告警流」：
  - 剧本队列 = 真实机群样本（剔除预标，让降噪真正跑规则 + LLM）＋ 间隔穿插的
    「接入域故障剧本」（会触发 缺工具 → 研发造工具 → 复用 的纵向闭环）；
  - 后台线程按节拍逐条取出 → 交给注入的 process 回调处置（降噪/根因/工具）；
  - 处置中发现工具缺口 → 回调内部自动登记需求并派发研发 → 造好工具回流复用；
  - 全流程结果写入内存 feed（环形缓冲），前端轮询增量渲染，像监控大屏一样滚动。

循环播放时，第二轮同类故障到来时工具已由第一轮研发沉淀 → 直接复用，
正好把「沉淀 → 复用」的演化史演示出来。

与真实接入的关系：本模块只负责「造一条持续不断的告警输入」，处置链路与
webhook 接入（/adapters/alert/ingest）完全相同，后续替换成真实推送即可。
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

# 剧本里能循环编排的接入域故障：
# 这些告警的 message 会让根因推理（Mock/真实 LLM 一致）推荐库内缺失的工具，
# 从而演示 缺工具 → 研发造 → 运维复用 的完整闭环。
FAULT_ALERTS: List[dict] = [
    {
        "alert_id": "A-ONU", "ts": "", "source": "zabbix",
        "metric": "optical_power", "host": "onu-1", "severity": "major",
        "value": "-28dBm",
        "message": "ONU 光模块接收光功率低于阈值，疑似光路劣化",
        "tags": ["access", "optical"], "is_noise": False,
    },
    {
        "alert_id": "A-TEMP", "ts": "", "source": "zabbix",
        "metric": "temperature", "host": "host-1", "severity": "critical",
        "value": "88C",
        "message": "物理机 host-1 核心温度过热告警，疑似散热故障",
        "tags": ["compute", "temperature"], "is_noise": False,
    },
    {
        "alert_id": "A-PORT", "ts": "", "source": "zabbix",
        "metric": "ifInErrors", "host": "switch-3", "severity": "major",
        "value": "1200",
        "message": "上联端口入向错包激增，疑似光模块或链路问题",
        "tags": ["switch"], "is_noise": False,
    },
]

# 剧本编排参数
_MIX_BLOCK = 11          # mixed：每 N 条真实样本后穿插一条故障
_STORY_PAIR = 3          # story：故障数量（与 FAULT_ALERTS 对齐，短循环聚焦闭环）


def strip_preset(alerts: List[dict]) -> List[dict]:
    """剔除样本里的预标字段（is_noise / normalized / triage_by）。

    降噪改造后判定必须真实跑「规则 → LLM」分层，不能依赖数据自带的标签；
    此处把 55 条样本洗干净，让流水线上的每一次判定都是真判定。
    """
    out = []
    for a in alerts:
        a = {k: v for k, v in (a or {}).items()
             if k not in ("is_noise", "normalized", "triage_by")}
        out.append(a)
    return out


def build_playlist(alerts: List[dict], profile: str = "mixed",
                   seed: int = 42) -> List[dict]:
    """编排剧本队列。

    profile:
      - mixed：真实机群样本为主（乱序），每隔固定间隔插入一条故障剧本。
               贴近真实监控：大部分是噪声、偶发真实故障、故障会触发闭环。
      - story：少量噪声伴流 + 故障剧本，短循环聚焦「造工具 → 复用」故事线。
    """
    base = strip_preset(alerts or [])
    rng = random.Random(seed)
    rng.shuffle(base)
    # 故障剧本同样剔除预标：让每一次降噪都真实跑 规则→LLM，不依赖"已知真故障"标签
    faults = strip_preset(FAULT_ALERTS)

    if profile == "story":
        out: List[dict] = []
        # 故障之间穿插一两条噪声样本，制造"海量噪声里揪出真故障"的观感
        noise_pool = [a for a in base if not a.get("_real")]
        for i, f in enumerate(faults):
            out.append(f)
            if noise_pool:
                out.append(noise_pool[i % len(noise_pool)])
        return out

    # 默认 mixed：每 _MIX_BLOCK 条样本插一条故障，轮流取故障剧本
    out = []
    fi = 0
    for i in range(0, len(base), _MIX_BLOCK):
        out.extend(base[i:i + _MIX_BLOCK])
        out.append(faults[fi % len(faults)])
        fi += 1
    return out


class AlertStream:
    """时间轴驱动的告警流播放器（单例运行，后端持有）。

    处置逻辑由外部注入：process(alert) -> dict，返回条目字段：
      noise / triage_by / summary / hypothesis / missing_tool /
      loop(none|pending|created|reused) / tool_name / error
    本类负责节奏、循环、统计与 feed 缓冲，不关心具体处置细节。
    """

    FEED_MAX = 300          # feed 环形上限：只留最近 N 条，防内存膨胀
    POLL_INTERVAL = 0.05    # stop 等待唤醒粒度（秒）

    def __init__(self, process: Optional[Callable[[dict], dict]] = None):
        self._process = process
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._playlist: List[dict] = []
        self._feed: List[dict] = []
        self._tasks: List[dict] = []         # 作战室任务队列（告警→分配Agent→处置）
        self._seq = 0
        self._seq_task = 0
        self._rounds = 0
        self._idx = 0
        self._running = False
        self._started_at: Optional[float] = None
        self._profile = "mixed"
        self._interval_ms = 1200
        self._loop = True
        self._ops_id: Optional[str] = None    # 流水线默认归属的运维 Agent
        self._started_by: str = ""            # 启动者用户名（多人协作时提示由谁启动）
        self._current: Optional[dict] = None
        self._last_error: Optional[str] = None
        self.TASK_MAX = 200                    # 任务环形上限，防内存膨胀

    # ---------- 控制 ----------
    def start(self, playlist: List[dict], profile: str = "mixed",
              interval_ms: int = 1200, loop: bool = True,
              ops_agent_id: Optional[str] = None,
              started_by: str = "") -> bool:
        """启动播放器。已在运行则返回 False。

        ops_agent_id：本场流水线处置告警归属的运维 Agent（用于作战室任务队列展示）。
        started_by：启动者用户名（多租户隔离后前端展示「由谁启动」，便于多人协作）。
        """
        with self._lock:
            if self._running:
                return False
            self._playlist = list(playlist or [])
            self._feed = []
            self._tasks = []
            self._seq = 0
            self._seq_task = 0
            self._rounds = 0
            self._idx = 0
            self._profile = profile
            self._interval_ms = max(200, int(interval_ms))
            self._loop = bool(loop)
            self._ops_id = ops_agent_id
            self._started_by = started_by
            self._running = True
            self._started_at = time.time()
            self._current = None
            self._last_error = None
            self._stop_evt.clear()
            self._thread = threading.Thread(
                target=self._run, name="alert-stream", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        """停止播放器（幂等，可安全重复调用）。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._started_at = None   # 停止后清空启动时刻，避免 uptime_s 继续累计
        self._stop_evt.set()
        th = self._thread
        if th and th is not threading.current_thread():
            th.join(timeout=3)

    @property
    def running(self) -> bool:
        return bool(self._running)

    # ---------- 内部：播放主循环 ----------
    def _run(self):
        idx = 0
        n = len(self._playlist)
        while self._running and not self._stop_evt.is_set():
            if n == 0:
                self._last_error = "剧本为空"
                break
            alert = self._playlist[idx % n]
            self._set_current(alert, idx)
            # 作战室任务队列：每条告警入队 → 处理中 → 处置完成（状态由 entry 决定）
            task_id = self._enqueue_task(alert)
            self._update_task(task_id, status="processing")
            t0 = time.time()
            item: dict = {}
            try:
                if self._process:
                    item = self._process(alert) or {}
            except Exception as e:  # 单条失败不打断整条流水线
                item = {"error": f"{type(e).__name__}: {e}"}
            item.setdefault("alert", {
                "alert_id": alert.get("alert_id", "?"),
                "metric": alert.get("metric", ""),
                "host": alert.get("host", ""),
                "severity": alert.get("severity", ""),
                "message": str(alert.get("message", ""))[:100],
            })
            item.setdefault("noise", False)
            item.setdefault("loop", "none")
            item.setdefault("error", "")
            self._finish_task(task_id, item)
            with self._lock:
                self._seq += 1
                item["seq"] = self._seq
                item["at"] = datetime.now().isoformat(timespec="seconds")
                item["duration_ms"] = int((time.time() - t0) * 1000)
                self._feed.append(item)
                if len(self._feed) > self.FEED_MAX:
                    self._feed = self._feed[-self.FEED_MAX:]
            self._set_current(None, None)
            idx += 1
            # 播完一圈：循环则重置指针，否则收尾
            if idx >= n:
                if not self._loop:
                    break
                idx = 0
                with self._lock:
                    self._rounds += 1
            self._stop_evt.wait(self._interval_ms / 1000.0)
        self._running = False
        self._current = None

    def _set_current(self, alert: Optional[dict], idx: Optional[int]):
        cur = None
        if alert is not None:
            cur = {
                "alert_id": alert.get("alert_id", "?"),
                "metric": alert.get("metric", ""),
                "host": alert.get("host", ""),
                "severity": alert.get("severity", ""),
            }
        with self._lock:
            self._current = cur
            if idx is not None:
                self._idx = idx

    # ---------- 任务队列（作战室可见） ----------
    def _new_task_id(self) -> str:
        with self._lock:
            self._seq_task += 1
            return f"TA-{self._seq_task:03d}"

    def _enqueue_task(self, alert: dict) -> str:
        tid = self._new_task_id()
        now = datetime.now().isoformat(timespec="seconds")
        task = {
            "task_id": tid,
            "alert_id": alert.get("alert_id", "?"),
            "severity": alert.get("severity", ""),
            "host": alert.get("host", ""),
            "metric": alert.get("metric", ""),
            "message": str(alert.get("message", ""))[:100],
            "assigned_agent": self._ops_id,
            "status": "queued",
            "loop": "none",
            "tool_name": "",
            "summary": "",
            "error": "",
            "at": now,
            "updated_at": now,
        }
        with self._lock:
            self._tasks.append(task)
            if len(self._tasks) > self.TASK_MAX:
                self._tasks = self._tasks[-self.TASK_MAX:]
        return tid

    def _update_task(self, task_id: str, **fields) -> Optional[dict]:
        with self._lock:
            for t in reversed(self._tasks):
                if t["task_id"] == task_id:
                    t.update(fields)
                    t["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    return t
        return None

    def _finish_task(self, task_id: str, entry: dict) -> None:
        """根据处置 entry 收敛任务终态。"""
        if entry.get("error"):
            status = "failed"
        elif entry.get("noise"):
            status = "suppressed"      # 噪声：规则/LLM 降噪抑制，不进队列
        else:
            loop = entry.get("loop", "none")
            if loop == "created":
                status = "closed"      # 缺工具→研发造→运维复用，闭环完成
            elif loop == "reused":
                status = "closed"      # 复用沉淀工具，直接处置
            elif loop == "pending":
                status = "escalated"   # 缺口已登记，等待研发派发
            else:
                status = "done"        # 已有工具直接处置完成
        self._update_task(
            task_id, status=status,
            loop=entry.get("loop", "none"),
            tool_name=entry.get("tool_name", ""),
            summary=entry.get("summary", ""),
            error=entry.get("error", ""),
        )

    def tasks(self, limit: int = 50) -> List[dict]:
        """最近任务（按时间倒序），供作战室任务队列渲染。"""
        with self._lock:
            items = list(self._tasks)
        items.reverse()
        return items[:limit]

    # ---------- 只读视图 ----------
    def status(self) -> dict:
        with self._lock:
            stats = {"ingested": self._seq, "noise": 0, "real": 0,
                     "created": 0, "reused": 0, "pending": 0, "errors": 0}
            for it in self._feed:
                if it.get("error"):
                    stats["errors"] += 1
                if it.get("noise"):
                    stats["noise"] += 1
                else:
                    stats["real"] += 1
                loop = it.get("loop")
                if loop == "created":
                    stats["created"] += 1
                elif loop == "reused":
                    stats["reused"] += 1
                elif loop == "pending":
                    stats["pending"] += 1
            uptime = int(time.time() - self._started_at) \
                if self._started_at else 0
            remaining = max(0, len(self._playlist) - self._idx) \
                if self._running and self._playlist else 0
            return {
                "running": bool(self._running),
                "profile": self._profile,
                "interval_ms": self._interval_ms,
                "loop": bool(self._loop),
                "rounds": self._rounds,
                "queue_remaining": remaining,
                "started_by": self._started_by,
                "started_at": (datetime.fromtimestamp(self._started_at)
                               .isoformat(timespec="seconds")
                               if self._started_at else ""),
                "uptime_s": uptime,
                "stats": stats,
                "current": self._current,
                "last_error": self._last_error,
            }

    def feed(self, after: int = 0) -> List[dict]:
        """增量拉取 feed：返回 seq > after 的条目。"""
        with self._lock:
            return [it for it in self._feed if it.get("seq", 0) > after]
