"""TeleOps 全局配置：路径与模型参数。

所有模块从这里我们取项目根目录、数据目录、模型名等，
避免到处写硬编码路径。
"""
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

# 模型配置
LLM_PROVIDER = "deepseek"          # deepseek | local
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
LOCAL_MODEL_ENDPOINT = os.getenv("LOCAL_MODEL_ENDPOINT", "http://localhost:11434/v1")
LOCAL_MODEL = "qwen2.5:7b"

# 日志
TRACE_DIR = ROOT / "traces"
TRACE_DIR.mkdir(exist_ok=True)
