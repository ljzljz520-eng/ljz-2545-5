"""从本地 JSON 文件读取示例场景（可替换数据源的默认实现）。"""
import json
from pathlib import Path

from .base import BaseSourceAdapter


class JsonFileAdapter(BaseSourceAdapter):
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self):
        if not self.path.exists():
            raise FileNotFoundError(f"数据源文件不存在: {self.path}")
        with self.path.open(encoding="utf-8") as fh:
            return json.load(fh)
