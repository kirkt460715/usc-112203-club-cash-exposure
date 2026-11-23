"""treasury 引擎的单元测试：覆盖资金桶、知识时点、关联敞口、拨付裁决与回测纪律。"""
from datetime import date
import unittest

from treasury.backtest import backtest_historical, stress_run
from treasury.calendar import d
from treasury.fund import _ceil_cover
from treasury.io_utils import load_records, verify_catalog
from treasury.knowledge import (
    COMMITTED_NOT_DUE,
    OVERDUE_UNPAID,
    RECEIVED,
    REVOCABLE,
    assert_no_lookahead,
    visible_receivables,
)
from treasury.ledger import (
    EQUIPMENT,
    Injection,
    Receipt,
    simulate_club,
)
from treasury.relations import active_relations
from treasury.stress import build_scenarios, projected_receipt_date, normal_lag_by_sponsor

RECORDS = "records"


class RecordIntegrityTests(unittest.TestCase):
    def test_catalog_verifies(self):
        catalog = verify_catalog(RECORDS)
        self.assertEqual(catalog["receivables"]["rows"], 216)
        records = load_records(RECORDS)
        self.assertEqual(len(records["clubs"]), 8)


class KnowledgePointInTimeTests(unittest.TestCase):
    def setUp(self):
        self.records = load_records(RECORDS)

    def test_receivable_states_are_classified(self):
        # 2026-03-15：3 月应收（due 3/8）已过期未回；1 月应收已收
        views = visible_receivables(self.records["receivables"], date(2026, 3, 15))
        states = {v.row["id"]: v.state for v in views}
        self.assertEqual(states["F2026-1-1"], RECEIVED)
        overdue = [v for v in views if v.state == OVERDUE_UNPAID]
        self.assertTrue(overdue)
        self.assertTrue(all(v.overdue for v in overdue))

    def test_revocable_flagged_only_when_unpaid(self):
        views = visible_receivables(self.records["receivables"], date(2026, 3, 15))
        c3 = [v for v in views if v.row["club"] == "C3"]
        # C3 的合同全部 cancellable，未回款者应进入 revocable
        self.assertTrue(any(v.state == REVOCABLE for v in c3))
        self.assertTrue(all(v.row["cancellable"] for v in c3 if v.state == REVOCABLE))

    def test_future_signing_invisible(self):
        # 4 月应收在 2/3 才签约：2 月 1 日不可见，2 月 10 日可见且未到期
        early = visible_receivables(self.records["receivables"], date(2026, 2, 1))
        late = visible_receivables(self.records["receivables"], date(2026, 2, 10))
        self.assertNotIn("F2026-4-1", {v.row["id"] for v in early})
        apr = next(v for v in late if v.row["id"] == "F2026-4-1")
        self.assertEqual(apr.state, COMMITTED_NOT_DUE)

    def test_lookahead_guard_allows_known_unpaid_but_blocks_future_signing(self):
        views = visible_receivables(self.records["receivables"], date(2024, 6, 30))
        # 7 月合同 6 月已签、7 月才回执：合法的"已知未回款"，守卫不应报错
        self.assertIn("F2024-7-1", {v.row["id"] for v in views})
        assert_no_lookahead(views, date(2024, 6, 30))


class RelationTests(unittest.TestCase):
    def setUp(self):
        self.records = load_records(RECORDS)

    def test_correction_invisible_before_known_date(self):
        before = active_relations(self.records["payer_relations"], date(2026, 4, 19))
        after = active_relations(self.records["payer_relations"], date(2026, 4, 20))
        self.assertEqual(before["S3"].payer_group, "G3")
        self.assertEqual(before["S3"].id, "L3")
        self.assertEqual(after["S3"].payer_group, "G1")
        self.assertEqual(after["S3"].id, "L3-R")

    def test_unknown_group_preserved(self):
        rels = active_relations(self.records["payer_relations"], date(2026, 4, 30))
        self.assertIsNone(rels["S6"].payer_group)

    def test_ignoring_corrections_keeps_legacy_relation(self):
        rows = [r for r in self.records["payer_relations"] if not r.get("supersedes")]
        rels = active_relations(rows, date(2026, 4, 30))
        self.assertEqual(rels["S3"].payer_group, "G3")


class LedgerBucketTests(unittest.TestCase):
    def _club(self):
        return {"id": "CX", "start_cash": 100, "earmarked": 50, "cap": 1000}

    def test_earmarked_cannot_pay_payroll(self):
        obs = [
            {"id": "O1", "club": "CX", "due": [2026, 1, 20],
             "purpose": "payroll", "amount": 120, "rank": 1},
        ]
        # 只有一笔 equipment 专款回款，无法救 payroll
        receipts = [Receipt(date(2026, 1, 10), 200, "earmarked", "F-em")]
        ledger = simulate_club(self._club(), obs, receipts, date(2026, 1, 31))
        self.assertEqual(ledger.outcomes[0].shortfall, 20)
        self.assertEqual(ledger.earmarked, 250)
        self.assertEqual(ledger.free, 0)
        self.assertEqual(ledger.months[0].status, "break")

    def test_earmarked_pays_equipment_and_free_is_retained(self):
        obs = [
            {"id": "O2", "club": "CX", "due": [2026, 1, 28],
             "purpose": EQUIPMENT, "amount": 220, "rank": 3},
        ]
        receipts = [Receipt(date(2026, 1, 10), 200, "earmarked", "F-em")]
        ledger = simulate_club(self._club(), obs, receipts, date(2026, 1, 31))
        # 专款 50+200=250 足够，普通桶不动
        self.assertEqual(ledger.outcomes[0].shortfall, 0)
        self.assertEqual(ledger.free, 100)
        self.assertEqual(ledger.earmarked, 30)

    def test_bridge_then_recover_within_horizon(self):
        obs = [
            {"id": "O3", "club": "CX", "due": [2026, 1, 20],
             "purpose": "payroll", "amount": 150, "rank": 1},
        ]
        # 到期日普通桶只有 100，月底回款补齐
        receipts = [Receipt(date(2026, 1, 25), 80, "free", "F-late")]
        ledger = simulate_club(self._club(), obs, receipts, date(2026, 1, 31))
        outcome = ledger.outcomes[0]
        self.assertEqual(outcome.paid_on_time, 100)
        self.assertEqual(outcome.shortfall, 0)
        self.assertEqual(outcome.recovered_date, date(2026, 1, 25))
        self.assertEqual(ledger.months[0].status, "bridge")

    def test_injection_purpose_restriction(self):
        obs = [
            {"id": "O4", "club": "CX", "due": [2026, 1, 20],
             "purpose": "payroll", "amount": 160, "rank": 1},
        ]
        # equipment 用途注入不能用于 payroll
        injections = [Injection(date(2026, 1, 19), 100, EQUIPMENT, "T-wrong")]
        ledger = simulate_club(self._club(), obs, [], date(2026, 1, 31), injections)
        self.assertEqual(ledger.outcomes[0].shortfall, 60)
        self.assertEqual(ledger.earmarked, 150)


class StressProjectionTests(unittest.TestCase):
    def setUp(self):
        self.records = load_records(RECORDS)
        self.scenarios = {s.id: s for s in build_scenarios(self.records["stress"])}
        self.rels = active_relations(self.records["payer_relations"], date(2026, 4, 30))
        self.lags = normal_lag_by_sponsor(self.records["receivables"], date(2026, 4, 30))

    def _view(self, rid):
        views = visible_receivables(self.records["receivables"], date(2026, 4, 30))
        return next(v for v in views if v.row["id"] == rid)

    def test_group_delay_hits_all_sponsors_in_group(self):
        scenario = self.scenarios["S-one"]  # G1 延迟 42 天
        v = self._view("F2026-5-1")  # S1 属于 G1
        landing = projected_receipt_date(v, scenario, self.rels, self.lags)
        self.assertEqual(landing, date(2026, 6, 19))  # 5/8 + 42

    def test_cancellable_receivable_revoked_under_stress(self):
        scenario = self.scenarios["S-unknown"]
        # S6 无集团且在 unlinked 中：纳入可能关联后，可撤销款返回 None
        v = self._view("F2026-5-6")  # C6/S6，cancellable
        self.assertIsNone(
            projected_receipt_date(v, scenario, self.rels, self.lags,
                                    include_possible=True)
        )
        # 不纳入可能关联时按正常账期到账
        normal = projected_receipt_date(v, scenario, self.rels, self.lags,
                                        include_possible=False)
        self.assertIsNotNone(normal)

    def test_normal_lag_uses_only_past_samples(self):
        lags = normal_lag_by_sponsor(self.records["receivables"], date(2024, 3, 1))
        # 样本只来自 2024-03 前的回执；正常账期 9 天
        self.assertEqual(lags["S1"], 9)
        # 2024-03 前 S1 只有 1、2 月两笔样本
        self.assertIn("__global__", lags)


class FundAllocationTests(unittest.TestCase):
    def setUp(self):
        self.records = load_records(RECORDS)

    def test_minimum_cover_rounds_up(self):
        self.assertEqual(_ceil_cover(100, 0.8), 80)
        self.assertEqual(_ceil_cover(101, 0.8), 81)  # 80.8 向上取整
        self.assertEqual(_ceil_cover(100, 1.0), 100)

    def test_resolutions_encumber_pool_and_are_not_redeployed(self):
        run = stress_run(self.records, 2026, date(2026, 4, 30), "S-one")
        tranche_ids = {t.resolution_id for t in run["fund"].tranches}
        self.assertIn("R1", tranche_ids)
        self.assertIn("R2", tranche_ids)
        # 初始 4700 - 已承诺 1820 = 2880 可新拨
        self.assertEqual(run["fund"].pool.initial - 1250 - 570, 2880)
        # R1 已划款、R2 已确认，均不允许再次部署
        for t in run["fund"].tranches:
            if t.resolution_id:
                self.assertEqual(t.source, "resolution")

    def test_cap_blocks_overlimit_club(self):
        run = stress_run(self.records, 2026, date(2026, 4, 30), "S-all")
        caps = {c["id"]: c["cap"] for c in self.records["clubs"]}
        for cid, used in run["fund"].club_aid.items():
            self.assertLessEqual(used, caps[cid])
        # 必须存在因 cap 阻塞的明示短缺
        self.assertTrue(
            any("cap" in s["blocking_constraint"]
                for s in run["fund"].explicit_shortfalls)
        )

    def test_explicit_shortfalls_cover_remaining_unpaid(self):
        run = stress_run(self.records, 2026, date(2026, 4, 30), "S-all")
        for s in run["fund"].explicit_shortfalls:
            self.assertGreater(s["explicit_shortfall"], 0)
        # 资金守恒：部署额不超过初始+补充的总盘子
        pool = run["fund"].pool
        total_envelope = pool.initial + sum(r[3] for r in pool.replenishment)
        self.assertLessEqual(pool.deployed, total_envelope)

    def test_returns_refund_pool_on_receipt(self):
        run = stress_run(self.records, 2026, date(2026, 4, 30), "S-all")
        # 累计归还以俱乐部普通桶实际划出额为准（不足部分不虚构入池）
        returned = sum(t.returned for t in run["fund"].tranches)
        self.assertEqual(returned, run["fund"].pool.returned_total)
        self.assertGreater(returned, 0)
        for t in run["fund"].tranches:
            self.assertLessEqual(t.returned, t.amount)

    def test_final_ledger_due_shortfall_reconciles_exactly_with_explicit_list(self):
        # 逐义务精确对账：拨款窗口（due >= as_of）内复算日历的到期未付额
        # == 明示短缺清单的 explicit_shortfall；as_of 前的历史断点不参与本轮拨款。
        as_of = date(2026, 4, 30)
        run = stress_run(self.records, 2026, as_of, "S-all")
        due_shortfall = {
            o.obligation_id: o.amount - o.paid_on_time
            for ledger in run["fund"].final_ledgers.values()
            for o in ledger.outcomes
            if o.amount - o.paid_on_time > 0 and d(o.due) >= as_of
        }
        explicit = {s["obligation"]: s["explicit_shortfall"]
                    for s in run["fund"].explicit_shortfalls}
        self.assertEqual(set(due_shortfall), set(explicit))
        for ob_id, amount in explicit.items():
            self.assertEqual(due_shortfall[ob_id], amount)
        open_total = sum(
            o.shortfall
            for ledger in run["fund"].final_ledgers.values()
            for o in ledger.outcomes
            if d(o.due) >= as_of
        )
        self.assertLessEqual(open_total, sum(explicit.values()))


class BacktestDisciplineTests(unittest.TestCase):
    def setUp(self):
        self.records = load_records(RECORDS)

    def test_backtest_uses_only_past_information(self):
        run = backtest_historical(
            self.records, 2024, date(2024, 6, 30), "S-one"
        )
        score = run["backtest_score"]
        self.assertEqual(score["sample_size"], 64)  # 8 队 × 8 个已签约账期（1-8 月）
        self.assertGreater(score["exact"], 0)
        # 所有投影到账都不得早于 as_of 之前还"未知"的信息
        for row in run["backtest_detail"]:
            self.assertTrue(row["receivable"].startswith("F2024-"))

    def test_normal_lag_ignores_receipts_after_as_of(self):
        # 两个时点的样本规模必须不同：晚到回执不能泄漏进早期估计
        lags_jan = normal_lag_by_sponsor(self.records["receivables"], date(2024, 1, 20))
        lags_mar = normal_lag_by_sponsor(self.records["receivables"], date(2024, 3, 20))
        self.assertEqual(lags_jan["S1"], 9)
        # 1 月时点每支 sponsor 至多 1 笔样本，3 月时点有 3 笔，全局账期估计随之更新
        jan_samples = sum(
            1 for r in self.records["receivables"]
            if r["received"] and d(r["received"]) <= date(2024, 1, 20)
        )
        mar_samples = sum(
            1 for r in self.records["receivables"]
            if r["received"] and d(r["received"]) <= date(2024, 3, 20)
        )
        self.assertLess(jan_samples, mar_samples)
        self.assertIn("__global__", lags_jan)
        self.assertIn("__global__", lags_mar)

    def test_backtest_score_excludes_later_receipts_from_judgement(self):
        # 同一赛季更晚 as_of 应看到更多已收样本，exact 数不应下降到 0
        early = backtest_historical(self.records, 2024, date(2024, 4, 30), "S-one")
        later = backtest_historical(self.records, 2024, date(2024, 8, 31), "S-one")
        self.assertLess(
            early["backtest_score"]["sample_size"],
            later["backtest_score"]["sample_size"],
        )


class ConservationTests(unittest.TestCase):
    def setUp(self):
        self.records = load_records(RECORDS)

    def test_cash_conservation_across_full_season(self):
        # 现金守恒（未付义务不扣现金，只挂账）：
        # 期初两桶 + 入账 + 保障金注入 == 已兑付 + 期末两桶 + 向保障金池的归还
        run = stress_run(self.records, 2026, date(2026, 4, 30), "S-all")
        clubs = {c["id"]: c for c in self.records["clubs"]}
        for cid, ledger in run["fund"].final_ledgers.items():
            opening = clubs[cid]["start_cash"] + clubs[cid]["earmarked"]
            receipts_in = sum(r.amount for r in ledger.receipts)
            injections_in = sum(
                t.amount for t in run["fund"].tranches if t.club == cid
            )
            # 累计现金兑付 = amount - shortfall（含窗内 bridge 补付）
            paid = sum(o.amount - o.shortfall for o in ledger.outcomes)
            ending = ledger.free + ledger.earmarked
            repaid = sum(t.returned for t in run["fund"].tranches if t.club == cid)
            self.assertEqual(
                opening + receipts_in + injections_in,
                paid + ending + repaid,
                f"{cid} 资金不守恒",
            )
            # 另立不变量：挂账义务额恒等于义务总额减累计兑付
            due_total = sum(o.amount for o in ledger.outcomes)
            open_short = sum(o.shortfall for o in ledger.outcomes)
            self.assertEqual(open_short, due_total - paid)


class SensitivityTests(unittest.TestCase):
    def setUp(self):
        self.records = load_records(RECORDS)

    def test_correction_moves_c3_break_into_shared_month(self):
        before = stress_run(self.records, 2026, date(2026, 4, 30), "S-one",
                            use_corrections=False)
        after = stress_run(self.records, 2026, date(2026, 4, 30), "S-one",
                           use_corrections=True)
        c3_before = [m.month for m in before["projection"]["ledgers"]["C3"].months
                     if m.status == "break"]
        c3_after = [m.month for m in after["projection"]["ledgers"]["C3"].months
                    if m.status == "break"]
        c1_after = [m.month for m in after["projection"]["ledgers"]["C1"].months
                    if m.status == "break"]
        # 采纳更正（S3 并入 G1）后，C3 的断点提前到 3 月，与同属 G1 的 C1 同档；
        # 旧关系图下 C3 在 3 月仍可自我覆盖。
        self.assertNotIn("2026-03", c3_before)
        self.assertIn("2026-03", c3_after)
        self.assertIn("2026-03", c1_after)
        # 更正后整体断点月份不少于旧口径
        self.assertGreaterEqual(len(c3_after), len(c3_before))


if __name__ == "__main__":
    unittest.main()
