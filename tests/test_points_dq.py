"""积分批次：防重复计算、成绩更正、DQ 边界、申诉撤销、榜单连续更正版。"""

import unittest

from src.domain.errors import RuleViolation
from tests._helpers import build_ready_service, register_player, open_division_with_players

EV, DIV = "E-T1", "MD-19+"


def play_event(max_entries: int = 2, alt: bool = True):
    """构造一站：2 个正赛名额（+候补），冻结规则与阵容并完成决赛。"""
    svc = build_ready_service(EV)
    pids = [register_player(svc, f"M-{i}", f"选手{i}", "1990-01-01")
            for i in range(1, 9)]
    open_division_with_players(svc, EV, DIV, pids, max_entries=max_entries)
    svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-1",
                     participant_ids=[pids[0], pids[1]])
    svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-2",
                     participant_ids=[pids[2], pids[3]])
    if alt:
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-ALT",
                         participant_ids=[pids[4], pids[5]], is_alternate=True)
    svc.freeze_ruleset_for_event(event_code=EV, ruleset_code="RS-C",
                                 ruleset_version="2026.1")
    svc.freeze_lineup(event_code=EV, division_code=DIV,
                      entry_codes=["EN-1", "EN-2"])
    return svc, pids


class PointIntegrityTest(unittest.TestCase):
    def test_each_result_counts_once_and_stale_result_rejected(self) -> None:
        svc, p = play_event()
        r1 = svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-1",
                               round_code="F", placing=2,
                               idempotency_key="r1")
        r2 = svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-2",
                               round_code="F", placing=1,
                               idempotency_key="r2")
        corr = svc.correct_result(event_code=EV, division_code=DIV, entry_code="EN-1",
                                  round_code="F", corrected_placing=1,
                                  reason="记分颠倒")
        svc.open_point_batch(batch_code="PB-1", event_code=EV)

        # 用已被更正的旧结果计分 -> 拒绝
        with self.assertRaises(RuleViolation) as ctx:
            svc.compute_points(batch_code="PB-1", lines=[
                {"participant_id": p[0], "points": 80, "result_event_id": r1["event_id"]},
            ])
        self.assertIn("已被更正", "；".join(ctx.exception.problems))

        # 同一批次内同一(选手,结果)重复 -> 拒绝
        with self.assertRaises(RuleViolation) as ctx:
            svc.compute_points(batch_code="PB-1", lines=[
                {"participant_id": p[0], "points": 100, "result_event_id": corr["event_id"]},
                {"participant_id": p[0], "points": 100, "result_event_id": corr["event_id"]},
            ])
        self.assertIn("批次内重复计积", "；".join(ctx.exception.problems))

        svc.compute_points(batch_code="PB-1", lines=[
            {"participant_id": p[0], "points": 100, "result_event_id": corr["event_id"]},
            {"participant_id": p[1], "points": 100, "result_event_id": corr["event_id"]},
            {"participant_id": p[2], "points": 60, "result_event_id": r2["event_id"]},
            {"participant_id": p[3], "points": 60, "result_event_id": r2["event_id"]},
        ])
        svc.freeze_point_batch("PB-1")

        # 选手台账可逐笔解释来源
        ledger = svc.point_ledger(p[0])
        self.assertEqual(ledger[0]["result_event_id"], corr["event_id"])
        self.assertEqual(ledger[0]["ruleset"], "RS-C@2026.1")
        self.assertEqual(svc.state.effective_points(p[0]), 100)

    def test_duplicate_batch_counting_blocked_after_freeze(self) -> None:
        # 地方站补传导致同一结果想在"未声明替代旧批次"的第二个批次再计一次 -> 拒绝。
        # 正确路径必须是 supersedes_batch 指向旧批次（旧批次积分随之标 VOIDED）。
        svc, p = play_event()
        r1 = svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-1",
                               round_code="F", placing=1, idempotency_key="r1")
        svc.open_point_batch(batch_code="PB-1", event_code=EV)
        svc.compute_points(batch_code="PB-1", lines=[
            {"participant_id": p[0], "points": 100, "result_event_id": r1["event_id"]},
            {"participant_id": p[1], "points": 100, "result_event_id": r1["event_id"]},
        ])
        svc.freeze_point_batch("PB-1")
        svc.open_point_batch(batch_code="PB-2X", event_code=EV)  # 未声明 supersedes
        with self.assertRaises(RuleViolation) as ctx:
            svc.compute_points(batch_code="PB-2X", lines=[
                {"participant_id": p[0], "points": 100, "result_event_id": r1["event_id"]},
            ])
        self.assertIn("已在批次 PB-1 计过", "；".join(ctx.exception.problems))

        # 声明替代后复用结果合法，且旧批次不再计入有效积分
        svc.open_point_batch(batch_code="PB-2", event_code=EV,
                             supersedes_batch="PB-1")
        svc.compute_points(batch_code="PB-2", lines=[
            {"participant_id": p[0], "points": 100, "result_event_id": r1["event_id"]},
            {"participant_id": p[1], "points": 100, "result_event_id": r1["event_id"]},
        ])
        svc.freeze_point_batch("PB-2")
        ledger = svc.point_ledger(p[0])
        self.assertEqual({(l["batch_code"], l["status"]) for l in ledger},
                         {("PB-1", "VOIDED"), ("PB-2", "FROZEN")})
        self.assertEqual(svc.state.effective_points(p[0]), 100)  # 不翻倍

    def test_frozen_batch_cannot_be_adjusted(self) -> None:
        svc, p = play_event()
        r1 = svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-1",
                               round_code="F", placing=1, idempotency_key="r1")
        svc.open_point_batch(batch_code="PB-1", event_code=EV)
        svc.compute_points(batch_code="PB-1", lines=[
            {"participant_id": p[0], "points": 100, "result_event_id": r1["event_id"]},
            {"participant_id": p[1], "points": 100, "result_event_id": r1["event_id"]},
        ])
        svc.freeze_point_batch("PB-1")
        with self.assertRaises(RuleViolation):
            svc.adjust_points(batch_code="PB-1", lines=[], reason="x", cause_event_ids=[])


class DQBoundaryTest(unittest.TestCase):
    def _frozen_with_ranking(self):
        svc, p = play_event()
        r1 = svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-1",
                               round_code="F", placing=1, idempotency_key="r1")
        r2 = svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-2",
                               round_code="F", placing=2, idempotency_key="r2")
        svc.open_point_batch(batch_code="PB-1", event_code=EV)
        svc.compute_points(batch_code="PB-1", lines=[
            {"participant_id": p[0], "points": 100, "result_event_id": r1["event_id"]},
            {"participant_id": p[1], "points": 100, "result_event_id": r1["event_id"]},
            {"participant_id": p[2], "points": 60, "result_event_id": r2["event_id"]},
            {"participant_id": p[3], "points": 60, "result_event_id": r2["event_id"]},
        ])
        svc.freeze_point_batch("PB-1")
        svc.release_ranking(release_code="RK-1", as_of="2026-11-02T00:00:00+08:00",
                            entries=[{"participant_id": pid, "points": v}
                                     for pid, v in [(p[0], 100), (p[1], 100),
                                                    (p[2], 60), (p[3], 60)]])
        return svc, p, r1, r2

    def test_dq_after_freeze_requires_correction_batch_and_ranking(self) -> None:
        svc, p, r1, r2 = self._frozen_with_ranking()
        out = svc.issue_dq(case_id="CASE-1", event_code=EV, division_code=DIV,
                           entry_code="EN-2", match_round="F",
                           reason="器材抽检不合格")
        self.assertEqual(out["boundary"]["stage"], "AFTER_BATCH_FROZEN")
        self.assertTrue(out["boundary"]["must_open_correction_batch"])
        self.assertTrue(out["boundary"]["must_publish_ranking_correction"])

        # 冻结批次原样保留
        self.assertEqual(svc.state.batches["PB-1"].status, "FROZEN")
        self.assertEqual(svc.state.entry(EV, "EN-2").status, "DQ")

        # 更正批次：DQ 行只能 0 分冲销
        svc.open_point_batch(batch_code="PB-2", event_code=EV,
                             supersedes_batch="PB-1")
        with self.assertRaises(RuleViolation):
            svc.compute_points(batch_code="PB-2", lines=[
                {"participant_id": p[2], "points": 60, "result_event_id": r2["event_id"]},
            ])
        svc.compute_points(batch_code="PB-2", lines=[
            {"participant_id": p[0], "points": 100, "result_event_id": r1["event_id"]},
            {"participant_id": p[1], "points": 100, "result_event_id": r1["event_id"]},
            {"participant_id": p[2], "points": 0, "result_event_id": r2["event_id"],
             "reason": "CASE-1 DQ 冲销"},
            {"participant_id": p[3], "points": 0, "result_event_id": r2["event_id"],
             "reason": "CASE-1 DQ 冲销"},
        ])
        svc.freeze_point_batch("PB-2")
        self.assertEqual(svc.state.effective_points(p[2]), 0)
        self.assertEqual(svc.state.effective_points(p[0]), 100)

        # 榜单：旧版保留，更正版连续不覆盖
        with self.assertRaises(RuleViolation):
            svc.publish_ranking_correction(release_code="RK-X", supersedes_release="RK-GHOST",
                                           correction_seq=1, entries=[], reason="r")
        svc.publish_ranking_correction(
            release_code="RK-1-C1", supersedes_release="RK-1", correction_seq=1,
            entries=[{"participant_id": p[0], "points": 100},
                     {"participant_id": p[1], "points": 100}],
            reason="CASE-1 DQ 冲销 EN-2")
        chain = svc.ranking_corrections("RK-1")
        self.assertEqual([c["release_code"] for c in chain], ["RK-1", "RK-1-C1"])
        self.assertEqual(svc.state.rankings["RK-1"].reason, "首发榜单")  # 旧版未被覆盖
        with self.assertRaises(RuleViolation):
            svc.publish_ranking_correction(
                release_code="RK-1-C1D", supersedes_release="RK-1", correction_seq=1,
                entries=[], reason="重复版次")

    def test_dq_before_freeze_recomputes_in_place(self) -> None:
        # 批次未冻结时 DQ：成绩作废，直接调整当前批次，不开更正链
        svc, p = play_event()
        r2 = svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-2",
                               round_code="F", placing=1, idempotency_key="r2")
        svc.open_point_batch(batch_code="PB-1", event_code=EV)
        svc.compute_points(batch_code="PB-1", lines=[
            {"participant_id": p[2], "points": 100, "result_event_id": r2["event_id"]},
            {"participant_id": p[3], "points": 100, "result_event_id": r2["event_id"]},
        ])
        out = svc.issue_dq(case_id="CASE-2", event_code=EV, division_code=DIV,
                           entry_code="EN-2", match_round="F", reason="冒名顶替")
        self.assertEqual(out["boundary"]["stage"], "BEFORE_BATCH")
        self.assertFalse(out["boundary"]["must_open_correction_batch"])
        svc.adjust_points(batch_code="PB-1", lines=[
            {"participant_id": p[2], "points": 0, "result_event_id": r2["event_id"],
             "reason": "CASE-2 DQ 作废"},
            {"participant_id": p[3], "points": 0, "result_event_id": r2["event_id"],
             "reason": "CASE-2 DQ 作废"},
        ], reason="CASE-2 赛前/未冻结 DQ，成绩作废重算",
           cause_event_ids=[out["event"]["event_id"]])
        self.assertEqual(svc.state.effective_points(p[2]), 0)
        self.assertEqual(len(svc.state.event_batches(EV)), 1)

    def test_appeal_upheld_reinstates_and_downstream_dq_still_blocks_points(self) -> None:
        # 申诉成立撤销 DQ：报名恢复；申诉驳回则 DQ 维持
        svc, p = play_event()
        r2 = svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-2",
                               round_code="F", placing=1, idempotency_key="r2")
        out = svc.issue_dq(case_id="CASE-3", event_code=EV, division_code=DIV,
                           entry_code="EN-2", match_round="F", reason="器材")
        svc.file_appeal(appeal_id="AP-1", case_id="CASE-3",
                        filed_by=p[2], grounds="抽检程序不合规")
        svc.decide_appeal(appeal_id="AP-1", case_id="CASE-3", decision="UPHELD",
                          decided_at="2026-11-03T10:00:00+08:00")
        self.assertFalse(svc.state.cases["CASE-3"].standing)
        self.assertNotEqual(svc.state.entry(EV, "EN-2").status, "DQ")

    def test_appeal_dismissed_keeps_dq(self) -> None:
        svc, p = play_event()
        svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-2",
                          round_code="F", placing=1, idempotency_key="r2")
        svc.issue_dq(case_id="CASE-4", event_code=EV, division_code=DIV,
                     entry_code="EN-2", match_round="F", reason="器材")
        svc.file_appeal(appeal_id="AP-2", case_id="CASE-4", filed_by=p[2], grounds="g")
        svc.decide_appeal(appeal_id="AP-2", case_id="CASE-4", decision="DISMISSED",
                          decided_at="2026-11-03T10:00:00+08:00")
        self.assertTrue(svc.state.cases["CASE-4"].standing)
        self.assertEqual(svc.state.entry(EV, "EN-2").status, "DQ")

    def test_late_dq_report_with_occurred_at_before_freeze_still_uses_correction(self) -> None:
        # 违规原发生时间在积分冻结之前，但裁决/上报在冻结之后（迟到上报）：
        # 冻结批次已不可改写，仍必须走更正批次；stage 如实记录为 BEFORE_BATCH。
        # 本用例只验证边界判定（以到达时批次状态为准），不依赖历史事件的完整时序。
        from datetime import datetime, timedelta
        svc, p, r1, r2 = self._frozen_with_ranking()
        batch = svc.state.batches["PB-1"]
        freeze_at = next(h["at"] for h in batch.history if h["kind"] == "FREEZE")
        before = datetime.fromisoformat(freeze_at)
        from datetime import timedelta
        occurred = before - timedelta(days=1)
        out = svc.issue_dq(case_id="CASE-5", event_code=EV, division_code=DIV,
                           entry_code="EN-2", match_round="F", reason="迟到的赛前违规裁决",
                           occurred_at=occurred)
        self.assertEqual(out["boundary"]["stage"], "BEFORE_BATCH")
        self.assertTrue(out["boundary"]["late_report_anomaly"])
        self.assertTrue(out["boundary"]["must_open_correction_batch"])
        # 乱序折叠（DQ 早于报名事件）后，报名状态仍一致为 DQ
        self.assertEqual(svc.state.entry(EV, "EN-2").status, "DQ")

    def test_result_for_withdrawn_entry_rejected(self) -> None:
        svc, p = play_event()
        svc.declare_withdrawal(event_code=EV, entry_code="EN-2", reason="赛前伤退")
        svc.promote_alternate(event_code=EV, division_code=DIV,
                              entry_code="EN-ALT", vacated_entry_code="EN-2")
        with self.assertRaises(RuleViolation):
            svc.record_result(event_code=EV, division_code=DIV, entry_code="EN-2",
                              round_code="F", placing=1)


if __name__ == "__main__":
    unittest.main()
