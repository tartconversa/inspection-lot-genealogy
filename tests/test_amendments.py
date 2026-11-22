"""供应商修正（replaces）与历史通知可追溯测试。"""
import unittest

from tests.helpers import (
    assembly,
    assignment,
    build_engine,
    certificate,
    notice,
    part,
    pool,
    reply,
    rework,
    shipment,
    taint_of,
)


def _base_parts():
    # n1/n2 同属 L7，工艺时刻分别为 12/27；n3/n4 为 L8；n5 为独立 L8 批次。
    return [
        part("p1", serial="n1", lot="L7", proc=12),
        part("p2", serial="n2", lot="L7", proc=27),
        part("p3", serial="n3", lot="L8", proc=36),
        part("p4", serial="n4", lot="L8", proc=38),
        part("p5", serial="n5", lot="L8", proc=51),
    ]


def _base_assemblies():
    return [
        assembly("a1", "unitA", "g1", at=45),
        assembly("a2", "unitB", "g1", at=45),
        assembly("a3", "unitC", "g1", at=47),
        assembly("a4", "unitD", "g1", at=47),
    ]


def _two_notices():
    return [
        notice("notice1", lo=0, hi=40, replaces=None, intake=1),
        notice("notice2", lo=0, hi=20, replaces="notice1", intake=3),
    ]


class AmendmentTests(unittest.TestCase):
    def test_amendment_narrows_defect_window(self):
        engine = build_engine(
            parts=_base_parts(),
            pool=[pool("g1", ["n1", "n2", "n3", "n4"], unit_qty=4)],
            assemblies=_base_assemblies(),
            assignments=[
                assignment("as-b", "a2", "n2", intake=3),
                assignment("as-d", "a4", "n4", intake=3),
            ],
            notices=_two_notices(),
        )
        current = engine.evaluate()
        self.assertEqual(current.notice_id, "notice2")
        # n2 的 proc=27 落在新窗口 [0,20) 之外 -> unitB 可排除。
        self.assertEqual(taint_of(current, "unitB"), (0, 0, 1))
        self.assertEqual(taint_of(current, "unitD"), (0, 0, 1))
        # 剩余未指派的 unitA/unitC 共享 n1 这一个坏件预算。
        self.assertEqual(current.units["unitA"].taint.category, "possible")
        self.assertEqual(current.units["unitC"].taint.category, "possible")

    def test_history_snapshots_keep_old_basis(self):
        engine = build_engine(
            parts=_base_parts(),
            pool=[pool("g1", ["n1", "n2", "n3", "n4"], unit_qty=4)],
            assemblies=_base_assemblies(),
            notices=_two_notices(),
        )
        snapshots = {snap["notice"].id: snap["evaluation"]
                     for snap in engine.history()}
        old = snapshots["notice1"]
        new = snapshots["notice2"]
        # 旧通告下四台整机全部 possible（2 个坏件在 4 台中不可区分）。
        for unit in ("unitA", "unitB", "unitC", "unitD"):
            self.assertEqual(old.units[unit].taint.category, "possible")
        # 新通告窗口收窄，坏件只剩 n1。
        self.assertEqual(new.notice_id, "notice2")

    def test_completed_recovery_and_inspections_survive_amendment(self):
        engine = build_engine(
            parts=_base_parts(),
            pool=[pool("g1", ["n1", "n2", "n3", "n4"], unit_qty=4)],
            assemblies=_base_assemblies(),
            assignments=[
                assignment("as-c", "a3", "n3", intake=3),
            ],
            notices=_two_notices(),
            shipments=[
                shipment("ship-c", "clientC", ["unitC"], at=54,
                         received="65", intake=1)
            ],
            replies=[
                reply("ret-c", "ship-c", "clientC", ["unitC"],
                      kind="return", intake=2)
            ],
            certificates=[
                certificate("tool-only", "unitC", "baseC", defect="D2",
                            scope="inspected", intake=3)
            ],
        )
        ev = engine.evaluate()
        # n3 属于 L8，两个通告版本下都可排除；unitC 未返工过 ->
        # 退回后当前依据可放行。历史回复与治具复检凭证仍完整保留。
        self.assertEqual(ev.units["unitC"].disposition, "returned_free")
        self.assertTrue(
            any(row[0] == "ret-c" for row in ev.shipments["ship-c"].replied)
        )
        self.assertTrue(ev.shipments["ship-c"].basis_changed)
        self.assertTrue(
            any(c.id == "tool-only" for c in engine.certificates)
        )

    def test_rework_replacement_and_certificate_under_amendment(self):
        engine = build_engine(
            parts=_base_parts(),
            pool=[pool("g1", ["n1", "n2", "n3", "n4"], unit_qty=4)],
            assemblies=_base_assemblies(),
            assignments=[assignment("as-a", "a1", "n1", intake=2)],
            rework=[
                rework("rw-a", "unitA", removed="n1", replacement="n5",
                       destination="hold-bin", old="baseA", new="newA",
                       at=75, intake=2)
            ],
            certificates=[
                certificate("right-cert", "unitA", "newA", intake=3)
            ],
            notices=_two_notices(),
        )
        ev = engine.evaluate()
        # 旧件 n1 在两个通告下都命中，hold-bin 限制不因修正而解除。
        self.assertEqual(ev.parts["p1"].location, "hold-bin")
        self.assertEqual(ev.parts["p1"].disposition, "hold")
        # 整机新配置拿到有效放行凭证。
        self.assertEqual(ev.units["unitA"].certificate_id, "right-cert")
        self.assertEqual(ev.units["unitA"].disposition, "released")

    def test_basis_changed_flag_for_in_transit_notification(self):
        engine = build_engine(
            parts=_base_parts(),
            pool=[pool("g1", ["n1", "n2", "n3", "n4"], unit_qty=4)],
            assemblies=_base_assemblies(),
            notices=_two_notices(),
            shipments=[
                shipment("ship-b", "clientB", ["unitB"], at=50,
                         received="59", intake=1)
            ],
        )
        # 旧通告下 unitB 被通知；修正后可排除，但没有客户回复。
        ev_old = engine.evaluate(cutoff_intake=1)
        self.assertEqual(
            ev_old.shipments["ship-b"].notified_units, ("unitB",)
        )
        ev_new = engine.evaluate()
        ship = ev_new.shipments["ship-b"]
        self.assertTrue(ship.basis_changed)
        self.assertIn("unitB", ship.notified_units)  # 历史通知不删
        # 无客户回复 -> 待办仍然挂着，不能按库存解锁。
        self.assertEqual(ship.open_units, ("unitB",))
        # 界限本身已收窄：旧通告下整批 2 个坏件，新通告下只剩 1 个。
        old_taint = ev_old.units["unitB"].taint
        new_taint = ev_new.units["unitB"].taint
        self.assertEqual((old_taint.lo, old_taint.hi), (0, 1))
        self.assertEqual((new_taint.lo, new_taint.hi), (0, 1))


if __name__ == "__main__":
    unittest.main()
