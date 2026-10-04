"""赛前就绪核验、规则冻结与跨层级规则的测试。"""

import unittest

from src.domain.errors import RuleViolation
from tests._helpers import build_ready_service


class ReadinessTest(unittest.TestCase):
    def test_missing_venue_and_officials_reported_before_play(self) -> None:
        from src.domain import AssociationService
        svc = AssociationService()
        svc.declare_tournament_level("COMMUNITY", "社区赛")
        svc.sanction_event(
            event_code="E-9", level="COMMUNITY", name="缺料站",
            starts_at="2026-11-02T09:00:00+08:00",
            ends_at="2026-11-02T18:00:00+08:00", organizer_id="o",
            venue_code="V-9",
            required_officials=[{"role": "REFEREE", "min_cert_level": "A", "count": 1}],
        )
        report = svc.readiness("E-9")
        self.assertFalse(report["ready"])
        joined = "；".join(report["missing"])
        self.assertIn("V-9", joined)          # 场地未核验
        self.assertIn("REFEREE", joined)      # 裁判缺失

    def test_official_certificate_expired_blocks_readiness(self) -> None:
        svc = build_ready_service("E-T")
        # 新增一站，比赛时间在证书有效期之外
        svc.sanction_event(
            event_code="E-X", level="COMMUNITY", name="过期证书站",
            starts_at="2028-01-02T09:00:00+08:00",
            ends_at="2028-01-02T18:00:00+08:00", organizer_id="o",
            venue_code="V-1",
            required_officials=[{"role": "REFEREE", "min_cert_level": "B", "count": 1}],
        )
        report = svc.readiness("E-X")
        self.assertFalse(report["ready"])
        self.assertIn("REFEREE", "；".join(report["missing"]))

    def test_cert_level_below_requirement_blocks(self) -> None:
        from src.domain import AssociationService
        svc = AssociationService()
        svc.declare_tournament_level("COMMUNITY", "社区赛")
        svc.sanction_event(
            event_code="E-C", level="COMMUNITY", name="级别不足站",
            starts_at="2026-11-03T09:00:00+08:00",
            ends_at="2026-11-03T18:00:00+08:00", organizer_id="o",
            venue_code="V-C",
            required_officials=[{"role": "REFEREE", "min_cert_level": "A", "count": 1}],
        )
        svc.verify_venue("V-C", "馆", "PASS")
        svc.certify_official(official_id="OF-C", name="仅B级", certificate_no="B",
                             cert_level="B",
                             valid_from="2026-01-01T00:00:00+08:00",
                             valid_to="2027-01-01T00:00:00+08:00")
        self.assertFalse(svc.readiness("E-C")["ready"])

    def test_venue_failed_verification_blocks(self) -> None:
        svc = build_ready_service("E-T")
        svc.verify_venue("V-1", "测试馆", "FAIL")
        self.assertFalse(svc.readiness("E-T")["ready"])

    def test_ready_event_passes(self) -> None:
        svc = build_ready_service()
        report = svc.readiness("E-T1")
        self.assertTrue(report["ready"], report["missing"])


class RulesetFreezeTest(unittest.TestCase):
    def test_national_counting_requires_frozen_ruleset_and_lineup(self) -> None:
        svc = build_ready_service()
        # 未冻结规则、未冻结阵容 -> 不能开积分批次
        with self.assertRaises(RuleViolation) as ctx:
            svc.open_point_batch(batch_code="PB-1", event_code="E-T1")
        self.assertIn("规则", "；".join(ctx.exception.problems))
        self.assertIn("阵容", "；".join(ctx.exception.problems))

    def test_each_level_uses_own_ruleset_version(self) -> None:
        # 不同层级可各自定义规则；全国计分时只冻结赛事实际使用的那一个版本。
        svc = build_ready_service()
        svc.define_ruleset(ruleset_code="RS-PROV", version="2026.3",
                           level="PROVINCIAL_QUAL", age_band_method="资格年分组",
                           doubles_method="允许跨省配对", equipment_method="送检B方案")
        svc.freeze_ruleset_for_event(event_code="E-T1", ruleset_code="RS-C",
                                     ruleset_version="2026.1")
        self.assertEqual(svc.state.events["E-T1"].frozen_ruleset.code, "RS-C")
        self.assertEqual(svc.state.events["E-T1"].frozen_ruleset.version, "2026.1")
        self.assertEqual(svc.state.rulesets["RS-PROV"].equipment_method, "送检B方案")


if __name__ == "__main__":
    unittest.main()
