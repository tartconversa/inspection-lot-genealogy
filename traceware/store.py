"""只追加事件仓（event store）。

来源表的每条记录被规范化为一个只追加事件。仓文件是 JSONL，每行一个
事件信封。事件 id 全局唯一，重复接收同一原件时整段跳过、不产生副作用，
因此可以安全重放；一次接收只处理了一部分时，未提交的文件不在仓内，
下次继续装载即可回到“可继续处理且适用证据相符”的状态。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    table: str
    intake: int
    seq: int
    payload: dict


def make_event_id(intake: int, table: str, record_id: str) -> str:
    return f"in{intake:02d}:{table}:{record_id}"


class EventStore:
    """JSONL 只追加仓。

    仓目录中：

    * ``events.jsonl``：已确认提交的事件，每行一个信封；
    * ``LOCK`` 不需要——本实现单进程使用，写入采用临时文件原子替换。
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.log_path = self.path / "events.jsonl"
        self._ids: set[str] = set()
        self._seq = 0
        for event in self._read_all():
            self._ids.add(event.event_id)
            self._seq = max(self._seq, event.seq)

    # ------------------------------------------------------------------
    def _read_all(self) -> list[StoredEvent]:
        if not self.log_path.exists():
            return []
        events: list[StoredEvent] = []
        with self.log_path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                envelope = json.loads(line)
                events.append(
                    StoredEvent(
                        event_id=envelope["event_id"],
                        table=envelope["table"],
                        intake=envelope["intake"],
                        seq=envelope["seq"],
                        payload=envelope["payload"],
                    )
                )
        return events

    def events(self) -> list[StoredEvent]:
        """按提交顺序返回全部事件（同序重放结果必须一致）。"""
        return self._read_all()

    def contains(self, event_id: str) -> bool:
        return event_id in self._ids

    # ------------------------------------------------------------------
    def append_many(self, events: list[tuple[str, str, int, dict]]) -> list[StoredEvent]:
        """原子提交一批事件。

        参数每项为 ``(event_id, table, intake, payload)``。若批中任一
        event_id 已在仓内，整批拒绝（调用方应先按 contains 过滤），
        保证“同一次交付”的原子性：要么全进，要么全不进。
        """
        for event_id, _table, _intake, _payload in events:
            if event_id in self._ids:
                raise ValueError(f"事件已存在，拒绝重复提交: {event_id}")
        prepared: list[StoredEvent] = []
        for event_id, table, intake, payload in events:
            self._seq += 1
            prepared.append(
                StoredEvent(
                    event_id=event_id,
                    table=table,
                    intake=intake,
                    seq=self._seq,
                    payload=payload,
                )
            )
        # 临时文件 + 原子替换，避免写到一半的行被后续重放读到。
        fd, tmp_name = tempfile.mkstemp(
            prefix=".events-", suffix=".tmp", dir=str(self.path)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                if self.log_path.exists():
                    with self.log_path.open("r", encoding="utf-8") as old:
                        tmp.write(old.read())
                for event in prepared:
                    tmp.write(
                        json.dumps(
                            {
                                "event_id": event.event_id,
                                "table": event.table,
                                "intake": event.intake,
                                "seq": event.seq,
                                "payload": event.payload,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            os.replace(tmp_name, self.log_path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        self._ids.update(event.event_id for event in prepared)
        return prepared

    def max_intake(self) -> int:
        events = self.events()
        return max((e.intake for e in events), default=0)
