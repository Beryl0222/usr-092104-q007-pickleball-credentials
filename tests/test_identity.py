"""同名运动员只能待核对、人工裁决后才合并的测试。"""

import unittest

from src.domain.errors import RuleViolation
from tests._helpers import build_ready_service


class IdentityTest(unittest.TestCase):
    def test_same_name_different_member_numbers_stay_separate(self) -> None:
        svc = build_ready_service()
        svc.register_local_identity(source_id="stn-a", local_member_no="M-1",
                                    name="陈晨", birth_date="1992-05-01")
        svc.register_local_identity(source_id="stn-b", local_member_no="M-900",
                                    name="陈晨", birth_date="1992-05-01")
        pa, pb = "P-stn-a-M-1", "P-stn-b-M-900"
        self.assertIn(pa, svc.state.profiles)
        self.assertIn(pb, svc.state.profiles)
        # 两个本地会员号分别挂在两个档案，未自动合并
        self.assertEqual(svc.state.locals["stn-a/M-1"].participant_id, pa)
        self.assertEqual(svc.state.locals["stn-b/M-900"].participant_id, pb)

    def test_propose_then_reject_keeps_separate(self) -> None:
        svc = build_ready_service()
        svc.register_local_identity(source_id="stn-a", local_member_no="M-1",
                                    name="陈晨", birth_date="1992-05-01")
        svc.register_local_identity(source_id="stn-b", local_member_no="M-900",
                                    name="陈晨", birth_date="1995-05-01")
        svc.propose_identity_match(match_id="MT-1", candidate_a="P-stn-a-M-1",
                                   candidate_b="P-stn-b-M-900",
                                   reason="同名待核对", confidence=0.4)
        svc.decide_identity_match(match_id="MT-1", decision="REJECTED",
                                  reviewer="officer-li")
        self.assertEqual(svc.state.matches["MT-1"].status, "REJECTED")
        self.assertIn("P-stn-b-M-900", svc.state.profiles)
        self.assertIn("P-stn-a-M-1", svc.state.profiles)

    def test_confirmed_match_merges_only_after_human_decision(self) -> None:
        svc = build_ready_service()
        svc.register_local_identity(source_id="stn-a", local_member_no="M-1",
                                    name="陈晨", birth_date="1992-05-01")
        svc.register_local_identity(source_id="stn-b", local_member_no="M-900",
                                    name="陈晨", birth_date="1992-05-01")
        svc.propose_identity_match(match_id="MT-2", candidate_a="P-stn-a-M-1",
                                   candidate_b="P-stn-b-M-900",
                                   reason="同名+同生日+证件后四位一致", confidence=0.9)
        # 裁决前仍是两个档案
        self.assertIn("P-stn-b-M-900", svc.state.profiles)
        svc.decide_identity_match(match_id="MT-2", decision="CONFIRMED",
                                  reviewer="officer-li",
                                  target_participant_id="P-stn-a-M-1")
        # 合并后：两个来源的本地号都指向保留档案，旧档不残留
        self.assertEqual(svc.state.locals["stn-b/M-900"].participant_id, "P-stn-a-M-1")
        survivor = svc.state.profiles["P-stn-a-M-1"]
        self.assertEqual({(r.source_id, r.local_member_no) for r in survivor.locals},
                         {("stn-a", "M-1"), ("stn-b", "M-900")})
        self.assertNotIn("P-stn-b-M-900", svc.state.profiles)

    def test_cannot_decide_twice(self) -> None:
        svc = build_ready_service()
        svc.register_local_identity(source_id="stn-a", local_member_no="M-1",
                                    name="陈晨", birth_date="1992-05-01")
        svc.register_local_identity(source_id="stn-b", local_member_no="M-2",
                                    name="陈晨", birth_date="1992-05-01")
        svc.propose_identity_match(match_id="MT-3", candidate_a="P-stn-a-M-1",
                                   candidate_b="P-stn-b-M-2", reason="同名", confidence=0.5)
        svc.decide_identity_match(match_id="MT-3", decision="REJECTED", reviewer="r1")
        with self.assertRaises(RuleViolation):
            svc.decide_identity_match(match_id="MT-3", decision="CONFIRMED",
                                      reviewer="r2")

    def test_propose_requires_resolvable_candidates(self) -> None:
        svc = build_ready_service()
        with self.assertRaises(RuleViolation):
            svc.propose_identity_match(match_id="MT-4", candidate_a="ghost-1",
                                       candidate_b="ghost-2", reason="x", confidence=0.1)


if __name__ == "__main__":
    unittest.main()
