"""量化评估基准包（P0-2 / 量化指标看板）。

提供「合成注入故障 → 运维 Agent 真实根因 → 算 Top-1 准确率 / 噪声抑制率」的
可复用核心，供 scripts/eval_closed_loop.py 调用并写 data/eval_results.json。
"""
