"""数据源适配器契约。

后台从「可替换示例数据源」接收班次与封闭记录：
任何实现本接口的对象都可被 ingest 服务消费（文件、HTTP 推送、测试桩均可替换）。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass
class SourceBundle:
    source_key: str
    revision: str
    kind: str            # places / ferries / closures / tides / mixed
    priority: int        # 数值越大优先级越高，矛盾时裁决用
    payload: dict[str, Any]


REQUIRED_KEYS = {"source_key", "revision", "payload"}


class AdapterError(Exception):
    pass


class BaseSourceAdapter:
    """所有适配器输出统一形态：

    {
      "source_key": "ferry.official",
      "revision": "2026-10-03-r1",
      "kind": "mixed",
      "priority": 10,
      "payload": {"places": [...], "ferries": [...], "closures": [...], "tide_points": [...], "tides": [...]}
    }
    """

    def fetch(self) -> dict[str, Any]:  # pragma: no cover - 接口
        raise NotImplementedError

    @staticmethod
    def validate(raw: dict[str, Any]) -> dict[str, Any]:
        missing = REQUIRED_KEYS - set(raw or {})
        if missing:
            raise AdapterError(f"数据源缺少必填字段: {sorted(missing)}")
        if not isinstance(raw["payload"], dict):
            raise AdapterError("payload 必须是对象")
        raw.setdefault("kind", "mixed")
        raw.setdefault("priority", 0)
        for sec in ("places", "ferries", "shuttles", "closures", "tide_points", "tides"):
            raw["payload"].setdefault(sec, [])
        return raw

    def bundles(self) -> Iterator[SourceBundle]:
        raw = self.validate(self.fetch())
        yield SourceBundle(
            source_key=raw["source_key"],
            revision=str(raw["revision"]),
            kind=raw["kind"],
            priority=int(raw["priority"]),
            payload=raw["payload"],
        )
