"""LLM 运行时配置 / 用量 / 余额端点。

从 server.py 抽出（D2 演进式拆分 R1）。LLMConfig 模型与掩码/控制台 URL 辅助函数
保留在 server.py（仍被其它逻辑复用），此处经别名引用，handler 体逐字搬移。
"""
import base64
import json
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.config import load_llm_config, save_llm_config
from src.core import usage
from src.api.context import ctx as s

router = APIRouter()

# 复用 server.py 中定义的模型与辅助函数（不重复定义，避免漂移）
LLMConfig = s.LLMConfig
_mask_llm_cfg = s._mask_llm_cfg
_provider_console_url = s._provider_console_url


@router.get("/llm/config")
def get_llm_config():
    """读取当前 LLM 配置（返回的 api_key 已被掩码）。"""
    return _mask_llm_cfg(load_llm_config())


@router.post("/llm/config")
async def update_llm_config(req: LLMConfig, request: Request):
    """保存 LLM 配置；api_key 留空/星号/已设置均表示保留原值，不覆盖。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未授权：请先登录或填写 API Token")
    cfg = load_llm_config()
    payload = req.dict(exclude_unset=True)
    # 保护 api_key：空值或掩码代表不覆盖
    if "api_key" in payload and payload["api_key"] in ("", "***", "已设置"):
        payload.pop("api_key", None)
    # 切换 provider 时自动回填预设 base_url / model（仅当用户没填）
    preset = s.LLM_PROVIDER_PRESETS.get(payload.get("provider", cfg["provider"]), {})
    if payload.get("provider") and payload.get("provider") != cfg.get("provider"):
        for k in ("base_url", "model"):
            if not payload.get(k) and preset.get(k):
                payload[k] = preset[k]
    cfg.update(payload)
    save_llm_config(cfg)
    # 同步模块级常量，让未重启的进程立即生效（尤其是 LLM_TRIAGE 开关）
    s._config_module.DEFAULT_LLM_TRIAGE = cfg.get("llm_triage", s._config_module.DEFAULT_LLM_TRIAGE)
    s._config_module.LLM_TRIAGE = cfg.get("llm_triage", s._config_module.LLM_TRIAGE)
    # 热更新：让全局 LLMClient 在下次 complete 前重新初始化
    s.llm._ensure_client()
    # 预算调整后重置熔断提示，让用户下次超限能再看到日志
    s.llm._budget_noticed = False
    return _mask_llm_cfg(cfg)


@router.get("/llm/usage")
def get_llm_usage():
    """LLM 用量统计 + 预算状态（今日/累计调用数、token、估算费用）。"""
    return usage.summary()


@router.post("/llm/usage/reset")
def reset_llm_usage(request: Request):
    """清空用量统计（演示重置用）。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未授权：请先登录或填写 API Token")
    usage.reset()
    return {"ok": True, **usage.summary()}


@router.get("/llm/balance")
def get_llm_balance():
    """查询账户余额（服务端代理，API Key 不出后端）。

    目前 DeepSeek 提供官方 /user/balance 接口；其他供应商尚未接入，
    返回 supported=false，前端提示去平台控制台查看。
    """
    cfg = load_llm_config()
    provider = (cfg.get("provider") or "").strip()
    api_key = (cfg.get("api_key") or "").strip()
    if provider != "deepseek" or not api_key:
        return {
            "supported": False,
            "reason": "当前供应商暂不支持余额查询，请前往平台控制台查看",
            "console_url": _provider_console_url(provider),
        }
    # 余额接口不在 /v1 下，需去掉 base_url 的版本后缀
    base = (cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = f"{base}/user/balance"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"supported": True, "console_url": "https://platform.deepseek.com",
                **data}
    except Exception as e:
        return {"supported": True, "error": str(e),
                "console_url": "https://platform.deepseek.com"}
