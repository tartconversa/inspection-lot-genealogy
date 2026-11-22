"""来源 JSON 装载、字段校验与事件规范化。

校验分两层：

1. 结构校验（本模块）：字段集合、类型、空值、区间方向、表内 id 唯一、
   业务键唯一等，不依赖其它表即可判断；
2. 引用一致性（:mod:`traceware.engine` 建图时）：跨表引用是否存在、
   候选组用量约束、回复归属等。

结构不合法的整次交付不会进入事件仓，调用方可修复原件后重新装载，
不会污染仓状态。
"""
from __future__ import annotations

import json
from pathlib import Path

from .model import (
    AssemblyRecord,
    AssignmentRecord,
    CertificateRecord,
    NoticeRecord,
    PartRecord,
    PoolRecord,
    ReplyRecord,
    ReworkRecord,
    ShipmentRecord,
)
from .store import EventStore, make_event_id

TABLES = (
    "parts",
    "pool",
    "assemblies",
    "assignments",
    "rework",
    "certificates",
    "notices",
    "shipments",
    "replies",
)


class SourceValidationError(ValueError):
    """来源原件结构不合法。"""


def _check_type(name: str, value, kind: str) -> None:
    if value is None:
        return
    if kind == "text":
        if not isinstance(value, str) or not value:
            raise SourceValidationError(f"字段 {name} 应为非空字符串，实际 {value!r}")
    elif kind == "int":
        # bool 是 int 的子类，显式排除。
        if not isinstance(value, int) or isinstance(value, bool):
            raise SourceValidationError(f"字段 {name} 应为整数，实际 {value!r}")
    elif kind == "items":
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise SourceValidationError(f"字段 {name} 应为字符串数组，实际 {value!r}")
    elif kind == "range":
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            or not value[0] < value[1]
        ):
            raise SourceValidationError(
                f"字段 {name} 应为严格递增的二元整数区间，实际 {value!r}"
            )
    else:  # pragma: no cover - schema 已固定，防御性分支
        raise SourceValidationError(f"未知字段类型 {kind}")


def load_delivery(path: str | Path, schema: dict) -> dict:
    """读取并做结构校验，返回规范化后的交付字典。"""
    with Path(path).open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if set(doc) != {"intake", "table", "records"}:
        raise SourceValidationError(f"{path}: 顶层必须且只能含 intake/table/records")
    table = doc["table"]
    if table not in schema:
        raise SourceValidationError(f"{path}: 未知数据表 {table}")
    if not isinstance(doc["intake"], int) or doc["intake"] <= 0:
        raise SourceValidationError(f"{path}: intake 必须为正整数")
    if not isinstance(doc["records"], list):
        raise SourceValidationError(f"{path}: records 必须为数组")

    field_types = schema[table]
    seen_ids: set[str] = set()
    clean_records = []
    for record in doc["records"]:
        if set(record) != set(field_types):
            raise SourceValidationError(
                f"{path} 记录 {record.get('id')!r}: 字段集合应为 {set(field_types)}"
            )
        record_id = record["id"]
        if not isinstance(record_id, str) or not record_id:
            raise SourceValidationError(f"{path}: 存在无有效 id 的记录")
        if record_id in seen_ids:
            raise SourceValidationError(f"{path}: 表内 id 重复 {record_id}")
        seen_ids.add(record_id)
        for name, kind in field_types.items():
            _check_type(f"{table}.{record_id}.{name}", record[name], kind)
        clean_records.append(dict(record))
    return {"intake": doc["intake"], "table": table, "records": clean_records}


def load_source_dir(source_dir: str | Path, schema_path: str | Path) -> list[dict]:
    """装载目录下全部原件，按到达序号与文件名稳定排序。"""
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    deliveries = [
        load_delivery(path, schema)
        for path in sorted(Path(source_dir).glob("*.json"))
    ]
    deliveries.sort(key=lambda d: (d["intake"], d["table"]))
    return deliveries


# ---------------------------------------------------------------------------
# 规范化为模型记录
# ---------------------------------------------------------------------------


def to_records(delivery: dict) -> list:
    table = delivery["table"]
    intake = delivery["intake"]
    out: list = []
    for row in delivery["records"]:
        if table == "parts":
            out.append(
                PartRecord(
                    id=row["id"],
                    plant=row["plant"],
                    lot=row["lot"],
                    item=row["item"],
                    proc=row["proc"],
                    serial=row["serial"],
                )
            )
        elif table == "pool":
            out.append(
                PoolRecord(
                    id=row["id"],
                    batch=row["batch"],
                    members=tuple(row["members"]),
                    unit_qty=row["unit_qty"],
                )
            )
        elif table == "assemblies":
            out.append(
                AssemblyRecord(
                    id=row["id"], unit=row["unit"], source=row["source"],
                    qty=row["qty"], at=row["at"], line=row["line"], intake=intake,
                )
            )
        elif table == "assignments":
            out.append(
                AssignmentRecord(
                    id=row["id"], assembly=row["assembly"], part=row["part"],
                    intake=intake,
                )
            )
        elif table == "rework":
            out.append(
                ReworkRecord(
                    id=row["id"], unit=row["unit"],
                    old_configuration=row["old_configuration"],
                    new_configuration=row["new_configuration"], removed=row["removed"],
                    replacement=row["replacement"], destination=row["destination"],
                    at=row["at"], intake=intake,
                )
            )
        elif table == "certificates":
            out.append(
                CertificateRecord(
                    id=row["id"], unit=row["unit"], configuration=row["configuration"],
                    defect=row["defect"], issued_min=row["issued_min"],
                    scope=row["scope"], intake=intake,
                )
            )
        elif table == "notices":
            out.append(
                NoticeRecord(
                    id=row["id"], defect=row["defect"], plant=row["plant"],
                    lot=row["lot"], item=row["item"], span=tuple(row["span"]),
                    replaces=row["replaces"], intake=intake,
                )
            )
        elif table == "shipments":
            out.append(
                ShipmentRecord(
                    id=row["id"], customer=row["customer"],
                    units=tuple(row["units"]), at=row["at"],
                    received=None if row["received"] is None else int(row["received"]),
                    intake=intake,
                )
            )
        elif table == "replies":
            out.append(
                ReplyRecord(
                    id=row["id"], shipment=row["shipment"], customer=row["customer"],
                    units=tuple(row["units"]), kind=row["kind"], intake=intake,
                )
            )
    return out


def ingest_deliveries(
    deliveries: list[dict], store: EventStore
) -> tuple[int, int]:
    """把已校验交付写入事件仓。

    按 (intake, table) 顺序逐表原子提交；已在仓内的整表跳过（重放幂等）。
    返回 ``(新提交事件数, 跳过事件数)``。一次接收只到一半时，已提交的
    表保留在仓内，未提交部分下次继续——状态始终可继续处理。
    """
    written = skipped = 0
    for delivery in deliveries:
        events = [
            (
                make_event_id(delivery["intake"], delivery["table"], row["id"]),
                delivery["table"],
                delivery["intake"],
                row,
            )
            for row in delivery["records"]
        ]
        if not events:
            continue
        if all(store.contains(event_id) for event_id, *_ in events):
            skipped += len(events)
            continue
        fresh = [event for event in events if not store.contains(event[0])]
        if len(fresh) != len(events):
            # 同一表只允许整表已在或整表全新：部分存在意味着原件 id 被改写，
            # 拒绝混写，避免同表新旧版本并存。
            colliding = [e[0] for e in events if store.contains(e[0])]
            raise SourceValidationError(
                f"表 {delivery['intake']:02d}-{delivery['table']} 存在部分事件 id "
                f"冲突: {colliding}"
            )
        store.append_many(events)
        written += len(events)
    return written, skipped
