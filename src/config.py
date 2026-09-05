"""TeleOps 全局配置：路径与模型参数。

所有模块从这里我们取项目根目录、数据目录、模型名等，
避免到处写硬编码路径。
"""
import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # 读取 .env 中的 DEEPSEEK_API_KEY
except ImportError:
    # 未安装 python-dotenv 时跳过，仅影响 .env 自动加载
    pass

# 项目根目录（本文件位于 src/ 下，往上一级即根）
ROOT = Path(__file__).resolve().parent.parent

# 关键目录（可用环境变量覆盖，便于测试隔离到临时目录）
DATA_DIR = Path(os.environ.get("TELEOPS_DATA_DIR") or str(ROOT / "data"))
KB_DIR = Path(os.environ.get("TELEOPS_KB_DIR") or str(ROOT / "kb"))
TOOLS_DIR = Path(os.environ.get("TELEOPS_TOOLS_DIR") or str(ROOT / "tools"))
SCRIPTS_DIR = ROOT / "scripts"

# 数据文件名
TOPOLOGY_FILE = DATA_DIR / "topology.json"
ALERTS_FILE = DATA_DIR / "alerts.json"
FEEDBACK_FILE = DATA_DIR / "feedback.json"
TOOLS_REGISTRY_FILE = DATA_DIR / "tools.json"
REQUIREMENTS_FILE = DATA_DIR / "requirements.json"
WORKSPACES_FILE = DATA_DIR / "workspaces.json"

# LLM 运行时配置落盘文件（比 .env 更适合热更新，不提交到 git）
#
# 注意：本文件保存的是真实 API Key，测试环境必须通过 TELEOPS_LLM_CONFIG_FILE
# 重定向到临时目录，否则 pytest 会读到开发者本机的 Key 去联网调用（既耗额度，
# 又让结果不确定）。不能靠重定向 DATA_DIR 解决——拓扑/告警种子数据仍来自真实 data/。
LLM_CONFIG_FILE = Path(os.environ.get("TELEOPS_LLM_CONFIG_FILE")
                       or str(DATA_DIR / "llm_config.json"))

# 适配器真实连接配置（外部系统地址/凭据），按 adapter id 索引。
# 不提交到 git（含凭据），示例见 data/adapters.example.json。
ADAPTER_CONFIG_FILE = Path(os.environ.get("TELEOPS_ADAPTER_CONFIG_FILE")
                           or str(DATA_DIR / "adapters.json"))

# 模型配置默认值（.env / 环境变量 -> 运行时 llm_config.json）
DEFAULT_LLM_PROVIDER = os.getenv("TELEOPS_LLM_PROVIDER", "deepseek")
DEFAULT_LLM_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEFAULT_LLM_BASE_URL = os.getenv("TELEOPS_LLM_BASE_URL", "https://api.deepseek.com/v1")
DEFAULT_LLM_MODEL = os.getenv("TELEOPS_LLM_MODEL", "deepseek-chat")
DEFAULT_LOCAL_MODEL_ENDPOINT = os.getenv("LOCAL_MODEL_ENDPOINT", "http://localhost:11434/v1")
DEFAULT_LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b")

# 二次降噪：规则无结论时是否再让 LLM 做一次语义判定（1/0，默认开启）。
# 关掉后规则判不出来的一律按「非噪声」处理，适合离线演示或节省 token。
DEFAULT_LLM_TRIAGE = os.getenv("TELEOPS_LLM_TRIAGE", "1") not in ("0", "false", "False", "")


# 成本护栏：每日预算上限（元）与超限策略。
#   daily_cny <= 0 表示不限制；action 取 warn（仅告警）/ fallback（降级 Mock）/ reject（拒绝）。
# 默认给一个保守的日预算：告警流是无人值守循环，没有护栏可能几小时烧穿整月额度。
DEFAULT_BUDGET_DAILY_CNY = float(os.getenv("TELEOPS_BUDGET_DAILY_CNY", "0") or 0)
DEFAULT_BUDGET_ACTION = os.getenv("TELEOPS_BUDGET_ACTION", "fallback")


def load_llm_config() -> dict:
    """读取运行时 LLM 配置；环境变量 < data/llm_config.json，支持热更新。

    注意：文件里的字段是「全量合并」而非按默认键白名单挑选——早期实现只复制
    cfg 中已存在的键，导致后加的配置项（如 budget_daily_cny）写完读不回来，
    静默失效。新增字段时无需再改这里。
    """
    cfg = {
        "provider": DEFAULT_LLM_PROVIDER,
        "api_key": DEFAULT_LLM_API_KEY,
        "base_url": DEFAULT_LLM_BASE_URL,
        "model": DEFAULT_LLM_MODEL,
        "local_endpoint": DEFAULT_LOCAL_MODEL_ENDPOINT,
        "local_model": DEFAULT_LOCAL_MODEL,
        "llm_triage": DEFAULT_LLM_TRIAGE,
        "budget_daily_cny": DEFAULT_BUDGET_DAILY_CNY,
        "budget_action": DEFAULT_BUDGET_ACTION,
        # 自定义单价覆盖内置定价表：{"provider.model": [输入, 缓存命中, 输出]}
        "pricing": {},
    }
    try:
        if LLM_CONFIG_FILE.exists():
            data = json.loads(LLM_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
    except Exception as e:
        print(f"[config] 读取 {LLM_CONFIG_FILE} 失败：{e}")
    return cfg


def save_llm_config(cfg: dict):
    """保存运行时 LLM 配置到 data/llm_config.json。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LLM_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_adapter_configs() -> dict:
    """读取适配器真实连接配置（data/adapters.json），按 adapter id 索引。

    文件不存在或解析失败时返回空 dict（适配器自动回退 demo 模式）。
    注意：本文件含外部系统凭据，不入库（见 .gitignore）。
    """
    try:
        if ADAPTER_CONFIG_FILE.exists():
            data = json.loads(ADAPTER_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[config] 读取 {ADAPTER_CONFIG_FILE} 失败：{e}")
    return {}


# 兼容旧代码与测试：保留模块级 LLM_TRIAGE 常量（运行时配置优先）
LLM_TRIAGE = DEFAULT_LLM_TRIAGE


def is_llm_triage_enabled() -> bool:
    """读取运行时配置；若运行时未配置，回退到模块级常量（供测试 monkeypatch）。"""
    return load_llm_config().get("llm_triage", LLM_TRIAGE)


# 日志
TRACE_DIR = ROOT / "traces"
TRACE_DIR.mkdir(exist_ok=True)
