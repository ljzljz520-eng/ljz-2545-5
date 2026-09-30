"""内存桩适配器：用于数据适配验收测试与管理后台推送。"""
from .base import BaseSourceAdapter


class DictAdapter(BaseSourceAdapter):
    def __init__(self, raw: dict):
        self.raw = raw

    def fetch(self):
        return self.raw
