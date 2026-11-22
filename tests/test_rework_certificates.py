"""返工旧件去向、凭证适用性与“工单关闭不解除限制”测试。"""
import unittest

from tests.helpers import (
    assembly,
    assignment,
    build_engine,
    certificate,
    notice,
    part,
    rework,
    taint_of,
)


def _parts():
    # unitA 经 assignment 确认装入坏件 n1；n5 来自已证明独立的 L8 批次。
    return [
        part("p1", serial="n1", lot="L7", proc=10),
        part("p5", serial="n5", lot="L8", proc=51),
    ]


def _build(certificates=None, rework_destination="hold-bin"):
    rw = rework(
        "rw1", "unitA", removed="n1", replacement="n5",
        destination=rework_destination, old="baseA", new="newA",
        at=75, intake=2,
    )
    return build_engine(
        parts=_parts(),
        pool=[],
        assemblies=[assembly("a1", "unitA", "n1", intake=1)],
        assignments=[assignment("as1", "a1", "n1", intake=1)],
        rework=[rw],
        certificates=certificates or [],
        notices=[notice("nt", lo=0, hi=40)],
    )


class ReworkAndCertificateTests(unittest.TestCase):
    def test_removed_part_keeps_own_inventory_destination(self):
        ev = _build().evaluate()
        old = ev.parts["p1"]  # 零件按 part id 索引，p1 的序号是 n1
        self.assertEqual(old.serial, "n1")
        self.assertEqual(old.location, "hold-bin")
        self.assertEqual(old.taint.category, "confirmed")
        self.assertEqual(old.disposition, "hold")

    def test_rework_without_certificate_does_not_release_unit(self):
        ev = _build().evaluate()
        # 换上的 n5 可排除，但没有覆盖新配置的放行凭证：限制继续。
        self.assertEqual(taint_of(ev, "unitA"), (0, 0, 1))
        self.assertEqual(ev.units["unitA"].disposition, "hold")
        self.assertIn("凭证", "".join(ev.units["unitA"].reasons))

    def test_certificate_on_old_configuration_is_not_applicable(self):
        ev = _build([
            certificate("c-old", "unitA", "baseA", intake=3)
        ]).evaluate()
        self.assertIsNone(ev.units["unitA"].certificate_id)
        self.assertEqual(ev.units["unitA"].disposition, "hold")

    def test_inspected_scope_certificate_does_not_lift_hold(self):
        ev = _build([
            certificate("c-inspect", "unitA", "newA",
                        scope="inspected", intake=3)
        ]).evaluate()
        self.assertIsNone(ev.units["unitA"].certificate_id)
        self.assertEqual(ev.units["unitA"].disposition, "hold")

    def test_wrong_defect_certificate_is_not_applicable(self):
        ev = _build([
            certificate("c-d2", "unitA", "newA", defect="D2", intake=3)
        ]).evaluate()
        self.assertIsNone(ev.units["unitA"].certificate_id)

    def test_valid_certificate_on_new_configuration_releases(self):
        ev = _build([
            certificate("c-right", "unitA", "newA", intake=3)
        ]).evaluate()
        self.assertEqual(ev.units["unitA"].certificate_id, "c-right")
        self.assertEqual(ev.units["unitA"].disposition, "released")
        # 旧件 n1 仍在 hold-bin 限制，整机放行不解除零件限制。
        self.assertEqual(ev.parts["p1"].disposition, "hold")

    def test_rework_requires_destination_for_removed_part(self):
        with self.assertRaisesRegex(ValueError, "必须留去向"):
            _build(rework_destination="")


if __name__ == "__main__":
    unittest.main()
