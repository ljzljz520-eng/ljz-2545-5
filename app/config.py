"""全局配置：数据库连接、时区、离线包目录等。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_URL = os.environ.get(
    "LIGHTHOUSE_DSN",
    f"postgresql://node:node@localhost:5432/{os.environ.get('LIGHTHOUSE_DB', 'lighthouse')}",
)

DATA_DIR = BASE_DIR / "data" / "samples"
DEFAULT_SCENARIO = os.environ.get("LIGHTHOUSE_SCENARIO", "default")
SCENARIO_DIR = DATA_DIR / DEFAULT_SCENARIO

OFFLINE_DIR = BASE_DIR / "offline"

# 接驳最小缓冲（分钟）：前一段到达 -> 下一段出发
MIN_TRANSFER_MINUTES = 15
# 跨日接驳阈值：到达与出发之间跨越午夜
OVERNIGHT_GAP_HOURS = 6

# 开放资料（Open Data）许可与免责声明
OPEN_DATA_NOTICE = "本页开放资料按 CC BY 4.0 发布；潮汐/班次随时间变化，离线包不保证现实交通可用。"
