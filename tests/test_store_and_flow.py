"""事件仓幂等、部分接收续传与端到端来源装载测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from traceware.loader import SourceValidationError, load_source_dir
from traceware.service import ROOT, SCHEMA_PATH, SOURCE_DIR, TraceService
from traceware.store import EventStore, make_event_id


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_replay(self):
        events = [
            (make_event_id(1, "parts", "p1"), "parts", 1, {"id": "p1"}),
        ]
        self.store.append_many(events)
        with self.assertRaises(ValueError):
            self.store.append_many(events)
        self.assertEqual(len(self.store.events()), 1)

    def test_partial_delivery_can_continue(self):
        self.store.append_many([
            (make_event_id(1, "parts", "p1"), "parts", 1, {"id": "p1"}),
        ])
        # 模拟进程中断后新进程重开同一仓：已提交内容仍在，可继续提交。
        reopened = EventStore(self.tmp.name)
        reopened.append_many([
            (make_event_id(2, "parts", "p2"), "parts", 2, {"id": "p2"}),
        ])
        self.assertEqual([e.event_id for e in reopened.events()],
                         ["in01:parts:p1", "in02:parts:p2"])

    def test_jsonl_lines_are_independent(self):
        self.store.append_many([
            (make_event_id(1, "parts", "p1"), "parts", 1, {"id": "p1"}),
            (make_event_id(1, "parts", "p2"), "parts", 1, {"id": "p2"}),
        ])
        lines = (Path(self.tmp.name) / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(json.loads(line))  # 每行都是独立合法 JSON


class EndToEndSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = TraceService(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_sample_scenario_matches_expected_dispositions(self):
        stats = self.service.load_sources()
        self.assertEqual(stats["written"], 29)
        self.assertEqual(stats["skipped"], 0)
        ev = self.service.evaluate()
        self.assertEqual(ev.notice_id, "notice2")

        units = ev.units
        # unitA：返工后拿到覆盖新配置 newA 的有效凭证，但其已随 ship-a
        # 离厂且客户未回复 -> 在途通知待办，不能按库存解锁。
        self.assertEqual(units["unitA"].disposition, "in_transit")
        self.assertEqual(units["unitA"].certificate_id, "right-cert")
        self.assertEqual(ev.parts["p1"].disposition, "hold")
        self.assertEqual(ev.parts["p1"].location, "hold-bin")
        # notice2 把箱内坏件收缩为仅 n1 一件；unitB/unitD 由 assignment
        # 确认抽走好件，数量界限传播后 unitA/unitC 的坏件上界也都归 0
        # （整批联合上界只剩 1，没有任何分支需要按最坏值相加）。
        for unit in ("unitB", "unitC", "unitD"):
            self.assertEqual(units[unit].taint.category, "excluded")
        self.assertEqual(ev.joint_budgets[0].total_bad, 1)
        self.assertEqual(ev.joint_budgets[0].joint_hi, 1)
        # unitE 使用 south/L7，跨段编号不解释为同一批 -> 可排除。
        self.assertEqual(units["unitE"].taint.category, "excluded")
        # unitF 直投 n006（L8 proc=60）-> 可排除。
        self.assertEqual(units["unitF"].taint.category, "excluded")

    def test_shipment_todos_and_partial_replies(self):
        self.service.load_sources()
        ev = self.service.evaluate()
        # ship-c：unitC 已 return，unitE 的 checked 不抵销 unitC 之外的义务；
        # 该单内被通知单位均已回复 -> 无待办。
        ship_c = ev.shipments["ship-c"]
        self.assertEqual(ship_c.open_units, ())
        replied_units = {unit for _r, unit, _k in ship_c.replied}
        self.assertEqual(replied_units, {"unitC"})
        # ship-a：unitA 在途，无客户回复 -> 通知待办保持。
        ship_a = ev.shipments["ship-a"]
        self.assertEqual(ship_a.open_units, ("unitA",))
        # ship-b 的 unitB 已退回，当前依据下可放行。
        self.assertEqual(ev.units["unitB"].disposition, "returned_free")

    def test_intake_by_intake_loading_converges_to_full_state(self):
        partial = TraceService(Path(self.tmp.name) / "partial")
        partial.load_up_to(1)
        self.assertEqual(partial.store.max_intake(), 1)
        ev1 = partial.evaluate()
        self.assertEqual(ev1.notice_id, "notice1")
        # 四条匿名分支全部 possible，联合上界受整批 2 个坏件约束。
        self.assertEqual(len(ev1.joint_budgets), 1)
        self.assertEqual(ev1.joint_budgets[0].joint_hi, 2)

        partial.load_up_to(2)
        ev2 = partial.evaluate()
        # 返工与 assignment 同时到达：unitA 换上独立批次 n5，当前污点可排除，
        # 但有效放行凭证要到 intake 3 才到；且 ship-a 已在本批到达 ->
        # 整机已是在途通知状态，不能按库存解锁。
        self.assertEqual(ev2.units["unitA"].taint.category, "excluded")
        self.assertEqual(ev2.units["unitA"].disposition, "in_transit")
        # 其余三台仍共享箱内坏件预算（notice1 下为 2 件，n1 已确认抽走）。
        for unit in ("unitB", "unitC", "unitD"):
            self.assertEqual(ev2.units[unit].taint.category, "possible")

        partial.load_up_to(3)
        full = TraceService(Path(self.tmp.name) / "full")
        full.load_sources()
        ev_partial = partial.evaluate()
        ev_full = full.evaluate()
        self.assertEqual(
            {u: e.taint for u, e in ev_partial.units.items()},
            {u: e.taint for u, e in ev_full.units.items()},
        )

    def test_reload_is_fully_idempotent(self):
        self.service.load_sources()
        again = self.service.load_sources()
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["skipped"], 29)

    def test_replay_rebuilds_identical_state(self):
        self.service.load_sources()
        rebuilt = TraceService(self.service.store.path).build_engine()
        ev_a = self.service.evaluate()
        ev_b = rebuilt.evaluate()
        self.assertEqual(
            {u: e.disposition for u, e in ev_a.units.items()},
            {u: e.disposition for u, e in ev_b.units.items()},
        )

    def test_invalid_source_is_rejected_before_persisting(self):
        bad_dir = Path(self.tmp.name) / "bad"
        bad_dir.mkdir()
        valid = next(SOURCE_DIR.glob("01-parts.json")).read_text(encoding="utf-8")
        bad_doc = json.loads(valid)
        bad_doc["records"][0]["proc"] = "not-an-int"
        (bad_dir / "01-parts.json").write_text(
            json.dumps(bad_doc), encoding="utf-8"
        )
        with self.assertRaises(SourceValidationError):
            load_source_dir(bad_dir, SCHEMA_PATH)

    def test_lot_and_shipment_queries(self):
        self.service.load_sources()
        lot_view = self.service.query_lot("north", "sensor", "L7")
        serials = {row["serial"] for row in lot_view["parts"]}
        self.assertEqual(serials, {"n001", "n002"})
        self.assertTrue(lot_view["history"])
        ship_view = self.service.query_shipment("ship-c")
        self.assertEqual(ship_view["shipment"].customer, "clientC")
        self.assertTrue(ship_view["history"])

    def test_source_files_remain_the_contract(self):
        # 守护测试：来源目录与字段契约未被改动。
        deliveries = load_source_dir(SOURCE_DIR, SCHEMA_PATH)
        self.assertGreaterEqual(len({d["intake"] for d in deliveries}), 3)
        self.assertEqual(ROOT, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    unittest.main()
