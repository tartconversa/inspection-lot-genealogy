"""追溯仓命令行（质控与发货台共用）。

用法（建议在隔离模式下运行，避免来源目录中的同名模块干扰）：
    python -I run_traceware.py load
    python -I run_traceware.py status
    python -I run_traceware.py lot north sensor L7
    python -I run_traceware.py shipment ship-c
    python -I run_traceware.py history
    python -I run_traceware.py replay
"""
from __future__ import annotations

import argparse
import sys

from .loader import SourceValidationError
from .model import Taint
from .service import TraceService, reset_store

CATEGORY_CN = {
    "confirmed": "确定受影响",
    "possible": "可能受影响",
    "excluded": "可排除",
}
DISPOSITION_CN = {
    "hold": "在厂限制",
    "released": "在厂可放行",
    "in_transit": "在途待回复",
    "returned_hold": "退回后仍限制",
    "returned_free": "退回可放行",
    "scrapped": "已报废",
    "consumed": "已装机",
}
ACTION_CN = {
    "allow": "放行",
    "hold_order": "暂缓订单",
    "notify_only": "通知并待回复",
}


def _fmt_taint(taint: Taint) -> str:
    return (
        f"{CATEGORY_CN[taint.category]}"
        f"(界限 [{taint.lo},{taint.hi}]/{taint.capacity})"
    )


def cmd_load(service: TraceService, args) -> int:
    try:
        if args.intake is not None:
            stats = service.load_up_to(args.intake)
        else:
            stats = service.load_sources()
    except SourceValidationError as exc:
        print(f"装载被拒: {exc}", file=sys.stderr)
        return 2
    print(
        f"交付 {stats['deliveries']} 次；新入仓 {stats['written']} 条；"
        f"幂等跳过 {stats['skipped']} 条；当前证据截止 intake={stats['max_intake']}"
    )
    for warning in service.warnings():
        print(f"  警告: {warning}")
    return 0


def cmd_status(service: TraceService, args) -> int:
    evaluation = service.evaluate()
    if evaluation is None:
        print("仓内尚无缺陷通告，无限制。")
        return 0
    print(f"当前有效通告: {evaluation.notice_id}（证据截止 intake={evaluation.cutoff_intake}）")
    print("\n[整机处置]")
    for unit_id in sorted(evaluation.units):
        exp = evaluation.units[unit_id]
        cert = exp.certificate_id or "无有效放行凭证"
        print(
            f"  {unit_id:8s} {_fmt_taint(exp.taint):28s} "
            f"{DISPOSITION_CN[exp.disposition]:10s} 发货台:{ACTION_CN[exp.ship_action]:10s} "
            f"配置={exp.configuration or '原始配置'} 凭证={cert}"
        )
    print("\n[零件/拆下旧件]")
    for part_id in sorted(evaluation.parts):
        exp = evaluation.parts[part_id]
        print(
            f"  {exp.serial:6s} {_fmt_taint(exp.taint):28s} "
            f"去向={exp.location:10s} {DISPOSITION_CN.get(exp.disposition, exp.disposition)}"
        )
    print("\n[整批联合预算：分支最坏值不相加]")
    for budget in evaluation.joint_budgets:
        branches = ", ".join(f"{u}<={h}" for u, h in sorted(budget.branch_hi.items()))
        print(
            f"  {budget.pool_id} 通告={budget.notice_id} 整批缺陷件<={budget.total_bad}；"
            f"分支 {branches}；联合上界={budget.joint_hi}"
        )
    print("\n[发货通知与回复待办]")
    for ship_id in sorted(evaluation.shipments):
        ship = evaluation.shipments[ship_id]
        flag = "；依据已被修正改变" if ship.basis_changed else ""
        print(
            f"  {ship_id} -> {ship.customer}: 通知 {list(ship.notified_units) or '无'}，"
            f"待回复 {list(ship.open_units) or '无'}，"
            f"已回复 {[(u, k) for _r, u, k in ship.replied]}{flag}"
        )
    releasable = service.releasable_range()
    print("\n[当前可放行范围]")
    print(f"  整机 {releasable['units']}")
    print(f"  零件 {releasable['parts']}")
    print(f"  交付单 {releasable['shipments']}")
    return 0


def cmd_lot(service: TraceService, args) -> int:
    result = service.query_lot(args.plant, args.item, args.lot)
    print(f"料批 {result['scope']}")
    print("零件:")
    for row in result["parts"]:
        exp = row["exposure"]
        print(
            f"  {row['serial']} proc={row['proc']} {_fmt_taint(exp.taint)} "
            f"去向={exp.location} {DISPOSITION_CN.get(exp.disposition, exp.disposition)}"
        )
    print("关联整机:")
    for exp in result["units"]:
        print(
            f"  {exp.unit}: {_fmt_taint(exp.taint)} "
            f"{DISPOSITION_CN[exp.disposition]} 发货台={ACTION_CN[exp.ship_action]}"
        )
    print("历次通告处置:")
    for snap in result["history"]:
        notice = snap["notice"]
        print(f"  通告 {notice.id} span={list(notice.span)} intake={notice.intake}")
    return 0


def cmd_shipment(service: TraceService, args) -> int:
    result = service.query_shipment(args.shipment_id)
    shipment = result["shipment"]
    print(
        f"交付单 {shipment.id} 客户={shipment.customer} 发货at={shipment.at} "
        f"签收={shipment.received or '在途'}"
    )
    current = result["current"]
    if current:
        print(
            f"  当前: 通知={list(current.notified_units)} 待回复={list(current.open_units)} "
            f"已回复={[(u, k) for _r, u, k in current.replied]} "
            f"依据变更={current.basis_changed}"
        )
    for exp in result["units"]:
        print(
            f"  {exp.unit}: {_fmt_taint(exp.taint)} "
            f"{DISPOSITION_CN[exp.disposition]} {ACTION_CN[exp.ship_action]}"
        )
    print("历史快照（旧通知不删除）:")
    for snap in result["history"]:
        eval_snap = snap["evaluation"]
        ship_snap = eval_snap.shipments.get(args.shipment_id)
        if ship_snap:
            print(
                f"  通告 {snap['notice'].id}: 通知={list(ship_snap.notified_units)} "
                f"待回复={list(ship_snap.open_units)}"
            )
    return 0


def cmd_history(service: TraceService, args) -> int:
    for snap in service.history():
        notice = snap["notice"]
        evaluation = snap["evaluation"]
        print(
            f"通告 {notice.id} defect={notice.defect} 段=({notice.plant},"
            f"{notice.item},{notice.lot}) span={list(notice.span)} "
            f"replaces={notice.replaces} intake={notice.intake}"
        )
        if evaluation is None:
            print("  （截止点无有效通告）")
            continue
        for unit_id in sorted(evaluation.units):
            exp = evaluation.units[unit_id]
            print(
                f"  {unit_id:8s} {_fmt_taint(exp.taint):28s} "
                f"{DISPOSITION_CN[exp.disposition]}"
            )
    return 0


def cmd_replay(service: TraceService, args) -> int:
    """重放校验：重建仓并比对两次评估，证明重放结果一致。"""
    first = service.evaluate()
    rebuilt = TraceService(service.store.path).evaluate()
    assert first is not None and rebuilt is not None
    same = (
        first.notice_id == rebuilt.notice_id
        and {u: e.taint for u, e in first.units.items()}
        == {u: e.taint for u, e in rebuilt.units.items()}
        and {p: e.taint for p, e in first.parts.items()}
        == {p: e.taint for p, e in rebuilt.parts.items()}
    )
    print("重放结果与原状态一致" if same else "重放不一致！")
    return 0 if same else 1


def cmd_reset(service: TraceService, args) -> int:
    reset_store(service.store.path)
    print("本地事件仓已清空。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="装配批次检验凭证与停售追溯仓")
    parser.add_argument("--store", default=None, help="事件仓目录（默认 .traceware）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="校验并装载 sources/ 原件")
    p_load.add_argument("--intake", type=int, default=None, help="只装载到指定到达序号")
    p_load.set_defaults(func=cmd_load)

    p_status = sub.add_parser("status", help="当前暴露、处置与待办总览")
    p_status.set_defaults(func=cmd_status)

    p_lot = sub.add_parser("lot", help="按料批查询历次处置与可放行范围")
    p_lot.add_argument("plant")
    p_lot.add_argument("item")
    p_lot.add_argument("lot")
    p_lot.set_defaults(func=cmd_lot)

    p_ship = sub.add_parser("shipment", help="按交付单查询历次处置")
    p_ship.add_argument("shipment_id")
    p_ship.set_defaults(func=cmd_shipment)

    p_history = sub.add_parser("history", help="通告修订链与各版本历史结论")
    p_history.set_defaults(func=cmd_history)

    p_replay = sub.add_parser("replay", help="重放仓并校验结果一致")
    p_replay.set_defaults(func=cmd_replay)

    p_reset = sub.add_parser("reset", help="清空本地事件仓")
    p_reset.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = TraceService(args.store) if args.store else TraceService()
    return args.func(service, args)
