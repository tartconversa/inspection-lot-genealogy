"""匿名箱数量界限、联合预算与禁止 FIFO 补造的规则测试。"""
import unittest

from tests.helpers import assembly, assignment, build_engine, notice, part, pool, taint_of


class ScopedIdentityTests(unittest.TestCase):
    def test_same_lot_different_plant_is_different_identity(self):
        # L7 在 north 与 south 是两个有效段，同名批号互不解释。
        engine = build_engine(
            parts=[
                part("p1", plant="north", lot="L7", serial="n1"),
                part("p2", plant="south", lot="L7", serial="s1"),
            ],
            assemblies=[
                assembly("a1", "U1", "n1", intake=1),
                assembly("a2", "U2", "s1", intake=1),
            ],
            notices=[notice("nt", lo=0, hi=40)],
        )
        ev = engine.evaluate()
        self.assertEqual(taint_of(ev, "U1"), (1, 1, 1))   # north/L7 命中
        self.assertEqual(taint_of(ev, "U2"), (0, 0, 1))   # south/L7 可排除

    def test_duplicate_serial_across_scopes_refused(self):
        with self.assertRaisesRegex(ValueError, "不同有效段"):
            build_engine(
                parts=[
                    part("p1", plant="north", lot="L7", serial="x1"),
                    part("p2", plant="south", lot="L8", serial="x1"),
                ],
                notices=[notice("nt")],
            )


class AnonymousPoolTests(unittest.TestCase):
    def _four_units_two_bad(self):
        # 箱内 4 件，2 件确定在缺陷范围（n1,n2），2 件可排除（n3,n4）；
        # 4 台整机各抽 1 件，无 assignment，身份不可知。
        parts = [
            part("p1", serial="n1", proc=10),
            part("p2", serial="n2", proc=20),
            part("p3", serial="n3", proc=50),
            part("p4", serial="n4", proc=60),
        ]
        return build_engine(
            parts=parts,
            pool=[pool("g1", ["n1", "n2", "n3", "n4"])],
            assemblies=[
                assembly("a1", "U1", "g1", at=41),
                assembly("a2", "U2", "g1", at=42),
                assembly("a3", "U3", "g1", at=43),
                assembly("a4", "U4", "g1", at=44),
            ],
            notices=[notice("nt", lo=0, hi=40)],
        )

    def test_each_branch_is_possible_with_provable_bounds(self):
        ev = self._four_units_two_bad().evaluate()
        for unit in ("U1", "U2", "U3", "U4"):
            self.assertEqual(ev.units[unit].taint.category, "possible")
            self.assertEqual(taint_of(ev, unit), (0, 1, 1))

    def test_joint_budget_caps_at_whole_batch_not_sum_of_worst_cases(self):
        engine = self._four_units_two_bad()
        ev = engine.evaluate()
        (budget,) = ev.joint_budgets
        self.assertEqual(budget.total_bad, 2)
        self.assertEqual(sum(budget.branch_hi.values()), 4)   # 分支最坏值
        self.assertEqual(budget.joint_hi, 2)                  # 联合上界只有 2

    def test_over_consumption_rejected(self):
        with self.assertRaisesRegex(ValueError, "数量约束"):
            build_engine(
                parts=[part("p1", serial="n1")],
                pool=[pool("g1", ["n1"], unit_qty=1)],
                assemblies=[
                    assembly("a1", "U1", "g1"),
                    assembly("a2", "U2", "g1"),
                ],
                notices=[notice("nt")],
            )

    def test_no_fifo_identity_invention(self):
        # 即使箱内第一件是坏件，未提供 assignment 时也不得把它判给第一台。
        ev = self._four_units_two_bad().evaluate()
        self.assertNotEqual(ev.units["U1"].taint.category, "confirmed")

    def test_assignment_narrows_bounds_without_clearing_others(self):
        engine = self._four_units_two_bad()
        # 补来证据：U1 用的是 n1（坏件）。其余三台仍共享剩余 1 个坏件预算。
        engine.register_assignment(assignment("as1", "a1", "n1", intake=2))
        ev = engine.evaluate(cutoff_intake=2)
        self.assertEqual(taint_of(ev, "U1"), (1, 1, 1))
        for unit in ("U2", "U3", "U4"):
            self.assertEqual(taint_of(ev, unit), (0, 1, 1))
        (budget,) = ev.joint_budgets
        # 整批坏件仍是 2：U1 锁定 1 个，其余分支联合上界 1。
        self.assertEqual(budget.total_bad, 2)
        self.assertEqual(budget.joint_hi, 2)

    def test_full_assignments_resolve_identities(self):
        engine = self._four_units_two_bad()
        for asm, part_id in [
            ("a1", "n1"), ("a2", "n2"), ("a3", "n3"), ("a4", "n4")
        ]:
            engine.register_assignment(
                assignment(f"as-{asm}", asm, part_id, intake=2)
            )
        ev = engine.evaluate(cutoff_intake=2)
        self.assertEqual(ev.units["U1"].taint.category, "confirmed")
        self.assertEqual(ev.units["U2"].taint.category, "confirmed")
        self.assertEqual(ev.units["U3"].taint.category, "excluded")
        self.assertEqual(ev.units["U4"].taint.category, "excluded")

    def test_fully_anonymous_box_keeps_quantity_bounds(self):
        # 同段箱：登记 1 个好件 + 2 个匿名位；整箱 3 件被两台整机耗尽。
        engine = build_engine(
            parts=[part("p1", serial="n1", proc=99)],  # proc 落在窗口外
            pool=[pool("g1", ["n1"], unit_qty=3)],
            assemblies=[
                assembly("a1", "U1", "g1", qty=2),
                assembly("a2", "U2", "g1", qty=1),
            ],
            notices=[notice("nt", lo=0, hi=40)],
        )
        ev = engine.evaluate()
        # 坏件只可能在 2 个匿名位中。U1 抽 2 件：可能 0~2 坏，无必然下界。
        self.assertEqual(taint_of(ev, "U1"), (0, 2, 2))
        # U2 抽最后 1 件：U1 可能把好件也抽走，最后一件可能是好件，
        # 故仍只给 [0,1] 界限而不补造身份。
        self.assertEqual(taint_of(ev, "U2"), (0, 1, 1))

    def test_anonymous_box_outside_notice_scope_is_clear(self):
        # 无法确定作用域的纯匿名箱（无成员）不与任何通告段匹配。
        engine = build_engine(
            parts=[],
            pool=[pool("g1", [], unit_qty=2)],
            assemblies=[assembly("a1", "U1", "g1", qty=2)],
            notices=[notice("nt", lot="L9", lo=0, hi=99)],
        )
        ev = engine.evaluate()
        self.assertEqual(taint_of(ev, "U1"), (0, 0, 2))

    def test_evidence_cutoff_respected(self):
        engine = self._four_units_two_bad()
        engine.register_assignment(assignment("as1", "a1", "n1", intake=3))
        # 截止到 intake 2 时证据尚不存在，U1 仍为 possible。
        early = engine.evaluate(cutoff_intake=2)
        self.assertEqual(early.units["U1"].taint.category, "possible")
        later = engine.evaluate(cutoff_intake=3)
        self.assertEqual(later.units["U1"].taint.category, "confirmed")


if __name__ == "__main__":
    unittest.main()
