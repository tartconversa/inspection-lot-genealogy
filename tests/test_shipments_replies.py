"""发货暂缓、离厂通知、回复归属与部分回收测试。"""
import unittest

from tests.helpers import (
    assembly,
    build_engine,
    notice,
    part,
    pool,
    reply,
    shipment,
    taint_of,
)


class ShipmentGateTests(unittest.TestCase):
    def _possible_parts_and_pool(self):
        # 两台整机共享一个 2 件箱（1 坏 1 好），无 assignment -> 均 possible。
        parts = [
            part("p1", serial="n1", proc=10),
            part("p2", serial="n2", proc=50),
        ]
        groups = [pool("g1", ["n1", "n2"])]
        assemblies = [
            assembly("a1", "U1", "g1", at=41),
            assembly("a2", "U2", "g1", at=42),
        ]
        return parts, groups, assemblies

    def test_possible_exposure_holds_order(self):
        engine = build_engine(
            parts=self._possible_parts_and_pool()[0],
            pool=self._possible_parts_and_pool()[1],
            assemblies=self._possible_parts_and_pool()[2],
            notices=[notice("nt", lo=0, hi=40)],
        )
        ev = engine.evaluate()
        self.assertEqual(ev.units["U1"].ship_action, "hold_order")
        self.assertEqual(ev.units["U2"].ship_action, "hold_order")

    def test_excluded_unit_is_shippable(self):
        engine = build_engine(
            parts=[part("p1", serial="n1", proc=99)],
            assemblies=[assembly("a1", "U9", "n1")],
            notices=[notice("nt", lo=0, hi=40)],
        )
        ev = engine.evaluate()
        self.assertEqual(ev.units["U9"].ship_action, "allow")
        self.assertEqual(ev.units["U9"].disposition, "released")

    def test_shipped_unit_becomes_notify_only_not_inventory_unlock(self):
        parts, groups, assemblies = self._possible_parts_and_pool()
        engine = build_engine(
            parts=parts,
            pool=groups,
            assemblies=assemblies,
            notices=[notice("nt", lo=0, hi=40)],
            shipments=[
                shipment("s1", "clientX", ["U1"], at=60, received=None, intake=2)
            ],
        )
        ev = engine.evaluate()
        self.assertEqual(ev.units["U1"].disposition, "in_transit")
        self.assertEqual(ev.units["U1"].ship_action, "notify_only")
        ship = ev.shipments["s1"]
        self.assertEqual(ship.notified_units, ("U1",))
        self.assertEqual(ship.open_units, ("U1",))

    def test_reply_from_wrong_customer_is_ignored(self):
        parts, groups, assemblies = self._possible_parts_and_pool()
        engine = build_engine(
            parts=parts,
            pool=groups,
            assemblies=assemblies,
            notices=[notice("nt", lo=0, hi=40)],
            shipments=[
                shipment("s1", "clientX", ["U1", "U2"], at=60, intake=2)
            ],
            replies=[reply("r1", "s1", "intruder", ["U1"], intake=3)],
        )
        ev = engine.evaluate()
        # 整条回复无效：两个单位仍待回复，且产生警告。
        self.assertEqual(ev.shipments["s1"].open_units, ("U1", "U2"))
        self.assertTrue(any("不符" in w for w in ev.warnings))

    def test_partial_recovery_does_not_offset_other_customers_goods(self):
        parts, groups, assemblies = self._possible_parts_and_pool()
        engine = build_engine(
            parts=parts,
            pool=groups,
            assemblies=assemblies,
            notices=[notice("nt", lo=0, hi=40)],
            shipments=[
                shipment("s1", "clientX", ["U1"], at=60, intake=2),
                shipment("s2", "clientY", ["U2"], at=61, intake=2),
            ],
            replies=[reply("r1", "s1", "clientX", ["U1"], intake=3)],
        )
        ev = engine.evaluate()
        self.assertEqual(ev.shipments["s1"].open_units, ())
        self.assertEqual(ev.shipments["s2"].open_units, ("U2",))

    def test_reply_covering_foreign_unit_only_counts_valid_units(self):
        parts, groups, assemblies = self._possible_parts_and_pool()
        engine = build_engine(
            parts=parts,
            pool=groups,
            assemblies=assemblies,
            notices=[notice("nt", lo=0, hi=40)],
            shipments=[
                shipment("s1", "clientX", ["U1"], at=60, intake=2)
            ],
            replies=[reply("r1", "s1", "clientX", ["U1", "U2"], intake=3)],
        )
        ev = engine.evaluate()
        self.assertEqual(ev.shipments["s1"].open_units, ())
        self.assertTrue(any("不属于该交付" in w for w in ev.warnings))

    def test_returned_unit_reevaluated_under_current_basis(self):
        parts, groups, assemblies = self._possible_parts_and_pool()
        engine = build_engine(
            parts=parts,
            pool=groups,
            assemblies=assemblies,
            notices=[notice("nt", lo=0, hi=40)],
            shipments=[
                shipment("s1", "clientX", ["U1"], at=60, intake=2)
            ],
            replies=[reply("r1", "s1", "clientX", ["U1"], intake=3)],
        )
        ev = engine.evaluate()
        # 回收后仍在厂，匿名界限未消除 -> 退回后仍限制。
        self.assertEqual(ev.units["U1"].disposition, "returned_hold")

    def test_evidence_cutoff_hides_late_shipment(self):
        # 发货记录在 intake 2 才到达：截止 intake 1 时整机仍在厂可暂缓。
        parts, groups, assemblies = self._possible_parts_and_pool()
        engine = build_engine(
            parts=parts,
            pool=groups,
            assemblies=assemblies,
            notices=[notice("nt", lo=0, hi=40)],
            shipments=[
                shipment("s1", "clientX", ["U1"], at=60, intake=2)
            ],
        )
        early = engine.evaluate(cutoff_intake=1)
        self.assertEqual(early.units["U1"].disposition, "hold")
        self.assertNotIn("s1", early.shipments)


if __name__ == "__main__":
    unittest.main()
