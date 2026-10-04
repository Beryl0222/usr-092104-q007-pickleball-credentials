"""事件存储：只追加日志，负责幂等去重、来源序号缺口检测与乱序重放。

设计要点：
- 接收顺序不决定业务状态。:meth:`EventStore.replay` 一律按 occurred_at
  （原发生时间）排序重放；recorded_at 仅留痕。
- 同一 event_id 原样重传视为重放，静默跳过；内容不同则拒绝（防止串单）。
- 同一 idempotency_key 的第二条事件直接拒绝——地方站"补传一次更正"
  不能靠重发完成，必须发 RESULT_CORRECTED / POINT_BATCH_ADJUSTED。
- 每个来源的 source_seq 必须从 0 起连续。离线后乱序到达允许先入库，
  :meth:`source_gaps` 报告缺口，投影可对未闭合范围拒绝出结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .contract import validate_payload
from .errors import ContractError


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class StoredEvent:
    record: dict

    @property
    def event_id(self) -> str:
        return self.record["event_id"]

    @property
    def event_type(self) -> str:
        return self.record["event_type"]

    @property
    def aggregate_id(self) -> str:
        return self.record["aggregate_id"]

    @property
    def source_id(self) -> str:
        return self.record["source_id"]

    @property
    def source_seq(self) -> int:
        return self.record["source_seq"]

    @property
    def occurred_at(self) -> datetime:
        return _dt(self.record["occurred_at"])

    @property
    def payload(self) -> dict:
        return self.record["payload"]


@dataclass
class AppendResult:
    accepted: bool
    event_id: str
    reason: str = ""  # accepted | duplicate_retransmission | duplicate_idempotency_key | gap_after_accept


class EventStore:
    """内存型只追加事件日志；可通过 load/dump 持久化为 JSON 数组。"""

    def __init__(self) -> None:
        self._events: dict[str, StoredEvent] = {}
        self._idem: dict[str, str] = {}  # idempotency_key -> event_id
        self._source_seqs: dict[str, set[int]] = {}
        self._order: list[str] = []

    # ---- 写入 ----
    def append(self, record: dict) -> AppendResult:
        errors = validate_payload(record)
        if errors:
            raise ContractError(errors)

        event_id = record["event_id"]
        existing = self._events.get(event_id)
        if existing is not None:
            if existing.record == record:
                return AppendResult(False, event_id, "duplicate_retransmission")
            raise ContractError([f"事件 {event_id} 已存在但内容不一致，禁止覆盖"])

        key = record.get("idempotency_key")
        if key is not None:
            if key in self._idem:
                return AppendResult(False, event_id, "duplicate_idempotency_key")
            self._idem[key] = event_id

        stored = StoredEvent(dict(record))
        self._events[event_id] = stored
        self._order.append(event_id)
        self._source_seqs.setdefault(record["source_id"], set()).add(record["source_seq"])

        gaps = self.source_gaps(record["source_id"])
        reason = "gap_after_accept" if gaps else "accepted"
        return AppendResult(True, event_id, reason)

    def append_many(self, records: list[dict]) -> list[AppendResult]:
        return [self.append(r) for r in records]

    # ---- 查询 ----
    def get(self, event_id: str) -> StoredEvent:
        return self._events[event_id]

    def all_events(self) -> list[StoredEvent]:
        """按业务时序（occurred_at, source_id, source_seq, event_id）重放。"""
        return sorted(
            self._events.values(),
            key=lambda e: (
                e.occurred_at,
                e.record["source_id"],
                e.record["source_seq"],
                e.event_id,
            ),
        )

    def replay(self, aggregate_id: str | None = None) -> list[StoredEvent]:
        events = self.all_events()
        if aggregate_id is not None:
            events = [e for e in events if e.aggregate_id == aggregate_id]
        return events

    def source_high_watermark(self, source_id: str) -> int | None:
        seqs = self._source_seqs.get(source_id)
        return max(seqs) if seqs else None

    def source_gaps(self, source_id: str) -> list[int]:
        """返回该来源在 [0, 最大序号] 内尚未到达的序号。"""
        seqs = self._source_seqs.get(source_id, set())
        if not seqs:
            return []
        return [s for s in range(0, max(seqs) + 1) if s not in seqs]

    def open_gaps(self) -> dict[str, list[int]]:
        """所有来源的未闭合缺口（空列表表示该来源已连续闭合）。"""
        return {src: self.source_gaps(src) for src in sorted(self._source_seqs)}

    def __len__(self) -> int:
        return len(self._events)

    # ---- 持久化 ----
    def dump(self) -> list[dict]:
        return [self._events[eid].record for eid in self._order]

    def dump_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, records: list[dict]) -> "EventStore":
        store = cls()
        # 装载不做缺口拦截：离线库本身可能就是乱序的。
        for record in records:
            store.append(record)
        return store

    @classmethod
    def load_json(cls, path: str | Path) -> "EventStore":
        return cls.load(json.loads(Path(path).read_text(encoding="utf-8")))
