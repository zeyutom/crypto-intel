"""独立初始化脚本。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from src.db import init_db
init_db()
print("DB ready.")
