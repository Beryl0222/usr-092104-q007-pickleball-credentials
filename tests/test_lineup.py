"""报名名单：冻结、搭档替换、退赛递补、跨组参赛边界测试。"""

import unittest

from src.domain.errors import RuleViolation
from tests._helpers import build_ready_service, register_player, open_division_with_players

EV = "E-T1"
DIV = "MD-19+"


def setup_event_with_entries(max_entries: int = 2):
    svc = build_ready_service(EV)
    pids = [register_player(svc, f"M-{i}", f"选手{i}", "1990-01-01")
            for i in range(1, 9)]
    open_division_with_players(svc, EV, DIV, pids, max_entries=max_entries)
    return svc, pids


class LineupFreezeTest(unittest.TestCase):
    def test_partner_replace_allowed_before_freeze(self) -> None:
        svc, p = setup_event_with_entries(max_entries=8)
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-1",
                         participant_ids=[p[0], p[1]])
        svc.replace_partner(event_code=EV, division_code=DIV, entry_code="EN-1",
                            out_participant_id=p[1], in_participant_id=p[2])
        self.assertEqual(svc.state.entry(EV, "EN-1").participant_ids, [p[0], p[2]])

    def test_partner_replace_blocked_after_freeze(self) -> None:
        svc, p = setup_event_with_entries()
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-1",
                         participant_ids=[p[0], p[1]])
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-2",
                         participant_ids=[p[2], p[3]])
        svc.freeze_ruleset_for_event(event_code=EV, ruleset_code="RS-C",
                                     ruleset_version="2026.1")
        svc.freeze_lineup(event_code=EV, division_code=DIV,
                          entry_codes=["EN-1", "EN-2"])
        with self.assertRaises(RuleViolation) as ctx:
            svc.replace_partner(event_code=EV, division_code=DIV, entry_code="EN-1",
                                out_participant_id=p[1], in_participant_id=p[4])
        self.assertIn("冻结", "；".join(ctx.exception.problems))

    def test_withdrawal_after_freeze_opens_vacancy_and_alternate_promotes(self) -> None:
        svc, p = setup_event_with_entries(max_entries=2)
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-1",
                         participant_ids=[p[0], p[1]])
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-2",
                         participant_ids=[p[2], p[3]])
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-ALT",
                         participant_ids=[p[4], p[5]], is_alternate=True)
        svc.freeze_ruleset_for_event(event_code=EV, ruleset_code="RS-C",
                                     ruleset_version="2026.1")
        svc.freeze_lineup(event_code=EV, division_code=DIV,
                          entry_codes=["EN-1", "EN-2"])

        svc.declare_withdrawal(event_code=EV, entry_code="EN-2", reason="受伤")
        d = svc.state.division(EV, DIV)
        self.assertEqual(d.vacancies, ["EN-2"])

        # 冻阵后不能直接换搭档，只能候补递补
        with self.assertRaises(RuleViolation):
            svc.replace_partner(event_code=EV, division_code=DIV, entry_code="EN-1",
                                out_participant_id=p[0], in_participant_id=p[6])

        svc.promote_alternate(event_code=EV, division_code=DIV,
                              entry_code="EN-ALT", vacated_entry_code="EN-2")
        alt = svc.state.entry(EV, "EN-ALT")
        self.assertEqual(alt.status, "PROMOTED")
        self.assertEqual(alt.promoted["sequence"], 1)
        self.assertEqual(svc.state.division(EV, DIV).vacancies, [])

    def test_alternate_cannot_promote_without_real_vacancy(self) -> None:
        svc, p = setup_event_with_entries(max_entries=8)
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-1",
                         participant_ids=[p[0], p[1]])
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-ALT",
                         participant_ids=[p[2], p[3]], is_alternate=True)
        svc.freeze_ruleset_for_event(event_code=EV, ruleset_code="RS-C",
                                     ruleset_version="2026.1")
        svc.freeze_lineup(event_code=EV, division_code=DIV, entry_codes=["EN-1"])
        # 没有冻阵后退赛造成的空缺 -> 递补被拒
        with self.assertRaises(RuleViolation) as ctx:
            svc.promote_alternate(event_code=EV, division_code=DIV,
                                  entry_code="EN-ALT", vacated_entry_code="EN-1")
        self.assertIn("空缺席位", "；".join(ctx.exception.problems))

    def test_promotion_sequence_must_be_consecutive(self) -> None:
        svc, p = setup_event_with_entries(max_entries=2)
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-1",
                         participant_ids=[p[0], p[1]])
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-2",
                         participant_ids=[p[2], p[3]])
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-ALT1",
                         participant_ids=[p[4], p[5]], is_alternate=True)
        svc.freeze_ruleset_for_event(event_code=EV, ruleset_code="RS-C",
                                     ruleset_version="2026.1")
        svc.freeze_lineup(event_code=EV, division_code=DIV,
                          entry_codes=["EN-1", "EN-2"])
        svc.declare_withdrawal(event_code=EV, entry_code="EN-2", reason="r")
        svc.promote_alternate(event_code=EV, division_code=DIV,
                              entry_code="EN-ALT1", vacated_entry_code="EN-2")
        self.assertEqual(svc.state.entry(EV, "EN-ALT1").promoted["sequence"], 1)

    def test_withdrawal_before_freeze_leaves_no_alternate_vacancy(self) -> None:
        # 冻结前退赛不产生"冻阵后空缺"，主办方可直接调整名单后再冻结
        svc, p = setup_event_with_entries(max_entries=8)
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-1",
                         participant_ids=[p[0], p[1]])
        svc.declare_withdrawal(event_code=EV, entry_code="EN-1", reason="赛前退出")
        svc.freeze_ruleset_for_event(event_code=EV, ruleset_code="RS-C",
                                     ruleset_version="2026.1")
        # 名单为空也可冻结；不会出现待递补空缺
        svc.freeze_lineup(event_code=EV, division_code=DIV, entry_codes=[])
        self.assertEqual(svc.state.division(EV, DIV).vacancies, [])

    def test_cross_division_participation_allowed_with_each_eligibility(self) -> None:
        # 跨组参赛：选手在两个组别分别取得资格即可；同组重复占位仍被禁止
        svc, p = setup_event_with_entries(max_entries=8)
        open_division_with_players(svc, EV, "XD-19+", [p[0], p[2]], max_entries=8)
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-MD",
                         participant_ids=[p[0], p[1]])
        svc.submit_entry(event_code=EV, division_code="XD-19+", entry_code="EN-XD",
                         participant_ids=[p[0], p[2]])  # 同一选手跨组，允许
        with self.assertRaises(RuleViolation) as ctx:
            svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-MD2",
                             participant_ids=[p[0], p[3]])  # 同组重复占位
        self.assertIn("重复占位", "；".join(ctx.exception.problems))

    def test_ineligible_player_blocks_entry_and_freeze(self) -> None:
        svc, p = setup_event_with_entries(max_entries=8)
        # 新登记一名未授予本组别资格的选手
        ghost = register_player(svc, "M-GHOST", "无资格选手", "1999-09-09")
        with self.assertRaises(RuleViolation) as ctx:
            svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-X",
                             participant_ids=[p[6], ghost])
        self.assertIn("未获得", "；".join(ctx.exception.problems))

    def test_entry_capacity_full_only_alternate_accepted(self) -> None:
        svc, p = setup_event_with_entries(max_entries=1)
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-1",
                         participant_ids=[p[0], p[1]])
        with self.assertRaises(RuleViolation) as ctx:
            svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-2",
                             participant_ids=[p[2], p[3]])
        self.assertIn("正赛名额已满", "；".join(ctx.exception.problems))
        # 候补可以报
        svc.submit_entry(event_code=EV, division_code=DIV, entry_code="EN-ALT",
                         participant_ids=[p[2], p[3]], is_alternate=True)
        self.assertTrue(svc.state.entry(EV, "EN-ALT").is_alternate)


if __name__ == "__main__":
    unittest.main()
