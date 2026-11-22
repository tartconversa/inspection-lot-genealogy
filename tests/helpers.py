"""测试共用构造器：直接在内存里拼装事实记录，不依赖来源文件。"""
from __future__ import annotations

from traceware.engine import TraceEngine
from traceware.model import (
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


def part(id_, plant="north", lot="L7", item="sensor", proc=10, serial=None):
    return PartRecord(id=id_, plant=plant, lot=lot, item=item, proc=proc,
                      serial=serial or id_)


def pool(id_, members, unit_qty=None, batch="crate"):
    return PoolRecord(id=id_, batch=batch, members=tuple(members),
                      unit_qty=unit_qty if unit_qty is not None else len(members))


def assembly(id_, unit, source, qty=1, at=50, line="l1", intake=1):
    return AssemblyRecord(id=id_, unit=unit, source=source, qty=qty, at=at,
                          line=line, intake=intake)


def assignment(id_, asm, part_id, intake=1):
    return AssignmentRecord(id=id_, assembly=asm, part=part_id, intake=intake)


def rework(id_, unit, removed, replacement, destination,
           old="base", new="new", at=70, intake=2):
    return ReworkRecord(id=id_, unit=unit, old_configuration=old,
                        new_configuration=new, removed=removed,
                        replacement=replacement, destination=destination,
                        at=at, intake=intake)


def certificate(id_, unit, configuration, defect="D1", issued_min=80,
                scope="defect-clearance", intake=3):
    return CertificateRecord(id=id_, unit=unit, configuration=configuration,
                             defect=defect, issued_min=issued_min, scope=scope,
                             intake=intake)


def notice(id_, lo=0, hi=40, replaces=None, intake=1, plant="north",
           lot="L7", item="sensor", defect="D1"):
    return NoticeRecord(id=id_, defect=defect, plant=plant, lot=lot, item=item,
                        span=(lo, hi), replaces=replaces, intake=intake)


def shipment(id_, customer, units, at=60, received=None, intake=1):
    return ShipmentRecord(id=id_, customer=customer, units=tuple(units), at=at,
                          received=received, intake=intake)


def reply(id_, ship_id, customer, units, kind="return", intake=2):
    return ReplyRecord(id=id_, shipment=ship_id, customer=customer,
                       units=tuple(units), kind=kind, intake=intake)


def build_engine(**tables) -> TraceEngine:
    defaults = {
        "parts": [], "pool": [], "assemblies": [], "assignments": [],
        "rework": [], "certificates": [], "notices": [],
        "shipments": [], "replies": [],
    }
    defaults.update(tables)
    return TraceEngine(defaults)


def taint_of(evaluation, unit) -> tuple[int, int, int]:
    t = evaluation.units[unit].taint
    return (t.lo, t.hi, t.capacity)
