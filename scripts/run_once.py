"""一键跑: ingest → factors → review → report。"""
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from src.pipeline import run_all_once
result = run_all_once()
print(json.dumps(result, indent=2, ensure_ascii=False))
