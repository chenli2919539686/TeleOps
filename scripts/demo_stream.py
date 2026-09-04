"""TeleOps 模拟告警流水线 · 命令行持续演示（与前端「实时告警流」同一引擎）。

解决的问题：静态样本处理一轮就停、Agent 没有持续输入——演示缺乏动态性。
本脚本把 55 条真实样本 + 故障剧本变成一条「时间轴告警流」：告警按节拍持续
进来，Agent 不停处置；发现工具缺口自动登记并派发研发造工具，后续同类故障
直接复用——像真实监控一样永不停摆。

与前端面板的关系：都走 server 的 AlertStream 单例与同一处置回调；本脚本
适合无界面 / 录屏 / CI 场景。

用法：
  python demo_stream.py                         # mixed 剧本 · 1.2s 节拍 · 循环
  python demo_stream.py --profile story         # 聚焦闭环故事（故障+噪声短循环）
  python demo_stream.py --profile story --limit 12 --interval 500
  python demo_stream.py --reset                 # 先清掉沉淀工具，回到"缺工具"初始态
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.alert_stream import build_playlist  # noqa: E402
import src.api.server as S                       # noqa: E402  复用 server 单例与闭环逻辑


def _fmt(it: dict) -> str:
    a = it["alert"]
    loop_ico = {"created": "🛠️ 造工具", "reused": "♻️ 复用",
                "pending": "📥 待派发", "none": ""}.get(it.get("loop"), "")
    tag = "🔕 噪声" if it.get("noise") else "🚨 真实"
    tri = {"rule": "规则", "llm": "LLM"}.get(it.get("triage_by"), "")
    head = (f"#{it['seq']:02d} {a.get('alert_id','?'):<8} {tag}({tri})"
            f" {loop_ico:<12}")
    body = (it.get("summary") or "")[:76]
    return f"{head} {body}"


def main():
    ap = argparse.ArgumentParser(description="TeleOps 模拟告警流水线 CLI 演示")
    ap.add_argument("--profile", default="mixed", choices=["mixed", "story"])
    ap.add_argument("--interval", type=int, default=1200, help="节拍毫秒")
    ap.add_argument("--loop", action="store_true", help="播完循环（默认开启）")
    ap.add_argument("--no-loop", action="store_true", dest="no_loop")
    ap.add_argument("--limit", type=int, default=0,
                    help="处理 N 条后停止（0=一直跑，Ctrl+C 退出）")
    ap.add_argument("--reset", action="store_true", help="先重置演示数据")
    args = ap.parse_args()

    if args.reset:
        S.stream_reset_demo()
        print("↺ 已重置演示数据：工具库回到内置两件套（ping_host / restart_service）\n")

    data = json.loads(Path(S.ALERTS_FILE).read_text(encoding="utf-8"))
    playlist = build_playlist(data.get("alerts", []), profile=args.profile)
    loop = not args.no_loop
    ops_id, mode = S._stream_resolve_ctx(None, None, None)
    S.stream._process = S._stream_make_processor(None, ops_id, mode)
    S.stream.start(playlist, profile=args.profile,
                   interval_ms=args.interval, loop=loop)
    print("=" * 100)
    print(f"TeleOps 实时告警流（CLI）| profile={args.profile} "
          f"| interval={args.interval}ms | loop={loop} | 剧本 {len(playlist)} 条"
          f" | ops={ops_id} | mode={mode}")
    print("=" * 100)

    seen = 0
    try:
        while True:
            st = S.stream.status()
            for it in S.stream.feed(after=seen):
                seen = it["seq"]
                print(f"[{it['at']}] {_fmt(it)}")
            if not S.stream.running and not st["stats"]["ingested"] and not playlist:
                print("剧本为空。")
                break
            if not S.stream.running:
                break
            if args.limit and seen >= args.limit:
                print(f"\n已达条数上限 {args.limit}，停止。")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到中断，停止流水线…")
    finally:
        S.stream.stop()
        st = S.stream.status()
        s = st["stats"]
        print("-" * 100)
        print(f"已接入 {s['ingested']} | 噪声抑制 {s['noise']} | 真实处置 {s['real']}"
              f" | 闭环造工具 {s['created']} | 复用沉淀 {s['reused']}"
              f" | 错误 {s['errors']} | 运行 {st['uptime_s']}s")


if __name__ == "__main__":
    main()
