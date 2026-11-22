"""应用服务：把事件仓重建成引擎，并提供质控/发货台统一操作。

仓是唯一事实源；引擎是纯内存重算产物，任何时刻都可以从仓完整重建，
因此“重放原件”与“一次接收只完成一部分后继续”得到的状态一致。
"""
from __future__ import annotations

import json
from pathlib import Path

from .engine import TraceEngine
from .loader import (
    TABLES,
    SourceValidationError,
    ingest_deliveries,
    load_source_dir,
    to_records,
)
from .model import Evaluation
from .store import EventStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE = ROOT / ".traceware"
SCHEMA_PATH = ROOT / "source-fields.json"
SOURCE_DIR = ROOT / "sources"


class TraceService:
    def __init__(self, store_path: str | Path = DEFAULT_STORE):
        self.store = EventStore(store_path)

    # ------------------------------------------------------------------
    # 装载
    # ------------------------------------------------------------------
    def load_sources(
        self,
        source_dir: str | Path = SOURCE_DIR,
        schema_path: str | Path = SCHEMA_PATH,
    ) -> dict:
        """校验并装载来源目录。返回装载统计。

        结构不合法的交付在入库前被拒绝，仓内已有状态不受影响。
        """
        deliveries = load_source_dir(source_dir, schema_path)
        written, skipped = ingest_deliveries(deliveries, self.store)
        return {
            "deliveries": len(deliveries),
            "written": written,
            "skipped": skipped,
            "max_intake": self.store.max_intake(),
        }

    def load_up_to(self, intake: int, **kwargs) -> dict:
        """只装载到达序号不超过 intake 的交付（模拟一次接收只到一部分）。"""
        deliveries = load_source_dir(
            kwargs.get("source_dir", SOURCE_DIR),
            kwargs.get("schema_path", SCHEMA_PATH),
        )
        part = [d for d in deliveries if d["intake"] <= intake]
        written, skipped = ingest_deliveries(part, self.store)
        return {
            "deliveries": len(part),
            "written": written,
            "skipped": skipped,
            "max_intake": self.store.max_intake(),
        }

    # ------------------------------------------------------------------
    # 重建与评估
    # ------------------------------------------------------------------
    def build_engine(self) -> TraceEngine:
        records_by_table: dict[str, list] = {table: [] for table in TABLES}
        for event in self.store.events():
            records_by_table[event.table].extend(to_records({
                "intake": event.intake,
                "table": event.table,
                "records": [event.payload],
            }))
        return TraceEngine(records_by_table)

    def evaluate(self, cutoff_intake: int | None = None) -> Evaluation | None:
        return self.build_engine().evaluate(cutoff_intake)

    def history(self) -> list[dict]:
        return self.build_engine().history()

    def query_lot(self, plant: str, item: str, lot: str) -> dict:
        return self.build_engine().query_lot(plant, item, lot)

    def query_shipment(self, shipment_id: str) -> dict:
        return self.build_engine().query_shipment(shipment_id)

    def releasable_range(self) -> dict:
        return self.build_engine().releasable_range()

    def warnings(self) -> tuple[str, ...]:
        return self.build_engine().warnings


def reset_store(store_path: str | Path = DEFAULT_STORE) -> None:
    """清空本地仓（演示与测试辅助）。"""
    log = Path(store_path) / "events.jsonl"
    if log.exists():
        log.unlink()
