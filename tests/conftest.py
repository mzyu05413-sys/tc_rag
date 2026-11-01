"""测试共享配置：把仓库内离线实验脚本目录加入导入路径。"""

import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))
