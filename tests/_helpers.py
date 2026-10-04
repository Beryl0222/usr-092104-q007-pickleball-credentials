"""测试公共搭建：快速构造一个"已授权、场地/裁判就绪"的社区站。"""

from __future__ import annotations

from src.domain import AssociationService


def build_ready_service(event_code: str = "E-T1") -> AssociationService:
    svc = AssociationService()
    svc.declare_tournament_level("COMMUNITY", "社区赛")
    svc.sanction_event(
        event_code=event_code, level="COMMUNITY", name="测试社区站",
        starts_at="2026-11-01T09:00:00+08:00",
        ends_at="2026-11-01T18:00:00+08:00",
        organizer_id="org-t", venue_code="V-1",
        required_officials=[{"role": "REFEREE", "min_cert_level": "B", "count": 1}],
    )
    svc.verify_venue("V-1", "测试馆", "PASS")
    svc.certify_official(official_id="OF-1", name="裁判甲", certificate_no="B-1",
                         cert_level="B",
                         valid_from="2026-01-01T00:00:00+08:00",
                         valid_to="2027-01-01T00:00:00+08:00")
    svc.define_ruleset(ruleset_code="RS-C", version="2026.1", level="COMMUNITY",
                       age_band_method="自然年", doubles_method="固定搭档",
                       equipment_method="抽检A")
    return svc


def register_player(svc: AssociationService, local_no: str, name: str,
                    birth: str, source: str = "stn-1") -> str:
    svc.register_local_identity(source_id=source, local_member_no=local_no,
                                name=name, birth_date=birth)
    return f"P-{source}-{local_no}"


def open_division_with_players(svc: AssociationService, event_code: str,
                               division_code: str, pids: list[str],
                               max_entries: int = 8) -> None:
    svc.open_division(event_code=event_code, division_code=division_code,
                      discipline="DOUBLES", age_band="19+", gender="M",
                      max_entries=max_entries)
    for pid in pids:
        svc.grant_eligibility(event_code=event_code, division_code=division_code,
                              participant_id=pid, basis="核验通过")
