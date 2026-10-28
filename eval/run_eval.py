"""本地评估入口；复用 CLI 服务，避免另写一套口径。"""

from tc_rag.interfaces.workflow import app

if __name__ == "__main__":
    # 用法：python eval/run_eval.py evaluate RUN --gold GOLD --config CONFIG
    app()
