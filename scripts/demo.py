"""端到端联调演示：构建一站社区赛的完整事件流并落盘 data/sample_stream.json。

运行：python3 -m scripts.demo
演示覆盖：
1. 两个来源各自登记同名运动员，只产生待核对匹配，不自动合并；
2. 赛前就绪核验先报缺失，补齐场地/裁判后通过；
3. 冻结规则与阵容；冻阵后退赛 -> 候补按顺位递补；
4. 成绩上报后补传更正（纠正事件，而非重发）；
5. 积分批次计算/冻结，选手台账可逐笔解释来源；
6. 榜单发布后出现赛后 DQ（冻结后）：开更正批次、发榜单连续更正版；
7. 地方来源离线乱序上报：按 occurred_at + source_seq 重建，缺口可查。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.domain import AssociationService
from src.domain.contract import validate_payload

OUT = Path(__file__).resolve().parents[1] / "data" / "sample_stream.json"


def main() -> None:
    svc = AssociationService()

    # --- 层级与赛事授权 ---
    svc.declare_tournament_level("COMMUNITY", "社区赛")
    svc.sanction_event(
        event_code="E-2026-HZ-014", level="COMMUNITY",
        name="杭州西湖社区匹克球公开赛第14站",
        starts_at="2026-10-18T09:00:00+08:00",
        ends_at="2026-10-18T18:00:00+08:00",
        organizer_id="org-hz-xihu-07", venue_code="V-BJ-03",
        required_officials=[
            {"role": "REFEREE", "min_cert_level": "B", "count": 1},
            {"role": "LINE_JUDGE", "min_cert_level": "C", "count": 2},
        ],
    )

    # 开赛前先看缺什么
    before = svc.readiness("E-2026-HZ-014")
    assert not before["ready"] and before["missing"]

    svc.verify_venue("V-BJ-03", "滨江体育馆3号场", "PASS")
    svc.certify_official(official_id="OF-1001", name="周敏", certificate_no="REF-B-2207",
                         cert_level="B", valid_from="2026-01-01T00:00:00+08:00",
                         valid_to="2027-01-01T00:00:00+08:00")
    svc.certify_official(official_id="OF-2001", name="李强", certificate_no="LJ-C-3310",
                         cert_level="C", valid_from="2026-01-01T00:00:00+08:00",
                         valid_to="2027-01-01T00:00:00+08:00")
    svc.certify_official(official_id="OF-2002", name="赵芳", certificate_no="LJ-C-3311",
                         cert_level="C", valid_from="2026-01-01T00:00:00+08:00",
                         valid_to="2027-01-01T00:00:00+08:00")
    assert svc.readiness("E-2026-HZ-014")["ready"]

    # --- 同名运动员：两个来源各有会员号，只是待核对 ---
    svc.register_local_identity(source_id="community-stn-014", local_member_no="M-88",
                                name="陈晨", birth_date="1992-05-01")
    svc.register_local_identity(source_id="provincial-games-zj", local_member_no="P-7701",
                                name="陈晨", birth_date="1992-05-01")
    pid_a = "P-community-stn-014-M-88"
    pid_b = "P-provincial-games-zj-P-7701"
    assert pid_a != pid_b and pid_a in svc.state.profiles and pid_b in svc.state.profiles
    svc.propose_identity_match(match_id="MATCH-0001", candidate_a=pid_a, candidate_b=pid_b,
                               reason="同名同姓且出生日期一致，待人工核对", confidence=0.81)
    assert svc.state.matches["MATCH-0001"].status == "PENDING"  # 绝不自动合并

    svc.register_local_identity(source_id="community-stn-014", local_member_no="M-99",
                                name="林涛", birth_date="1988-11-20")
    pid_c = "P-community-stn-014-M-99"
    svc.register_local_identity(source_id="community-stn-014", local_member_no="M-100",
                                name="高远", birth_date="1995-03-09")
    pid_d = "P-community-stn-014-M-100"

    # --- 规则、组别、资格 ---
    svc.define_ruleset(ruleset_code="RS-COMMUNITY", version="2026.1", level="COMMUNITY",
                       age_band_method="年龄自然年分组", doubles_method="男双/女双/混双固定",
                       equipment_method="拍面抽检A方案")
    svc.open_division(event_code="E-2026-HZ-014", division_code="MD-19+",
                      discipline="DOUBLES", age_band="19+", gender="M", max_entries=2)
    for pid in (pid_c, pid_d):
        svc.grant_eligibility(event_code="E-2026-HZ-014", division_code="MD-19+",
                              participant_id=pid, basis="年龄与会员资格核验通过")

    # --- 报名（2 个正赛 + 1 个候补），冻结前允许换搭档 ---
    svc.submit_entry(event_code="E-2026-HZ-014", division_code="MD-19+",
                     entry_code="EN-01", participant_ids=[pid_c, pid_d])
    # 第二对选手：借用同来源另两个本地号
    svc.register_local_identity(source_id="community-stn-014", local_member_no="M-101",
                                name="何凯", birth_date="1990-01-17")
    svc.register_local_identity(source_id="community-stn-014", local_member_no="M-102",
                                name="罗斌", birth_date="1991-07-08")
    pid_e, pid_f = "P-community-stn-014-M-101", "P-community-stn-014-M-102"
    svc.register_local_identity(source_id="community-stn-014", local_member_no="M-103",
                                name="宋涛", birth_date="1993-09-02")
    pid_g = "P-community-stn-014-M-103"
    for pid in (pid_e, pid_f, pid_g, pid_a):
        svc.grant_eligibility(event_code="E-2026-HZ-014", division_code="MD-19+",
                              participant_id=pid, basis="年龄与会员资格核验通过")
    svc.submit_entry(event_code="E-2026-HZ-014", division_code="MD-19+",
                     entry_code="EN-02", participant_ids=[pid_e, pid_f])
    svc.submit_entry(event_code="E-2026-HZ-014", division_code="MD-19+",
                     entry_code="EN-03", participant_ids=[pid_g, pid_a],
                     is_alternate=True)

    # 冻结规则与阵容
    svc.freeze_ruleset_for_event(event_code="E-2026-HZ-014",
                                 ruleset_code="RS-COMMUNITY", ruleset_version="2026.1")
    svc.freeze_lineup(event_code="E-2026-HZ-014", division_code="MD-19+",
                      entry_codes=["EN-01", "EN-02"])

    # --- 冻阵后退赛：只能候补递补，不能直接换人 ---
    svc.declare_withdrawal(event_code="E-2026-HZ-014", entry_code="EN-02",
                           reason="选手赛前受伤")
    svc.promote_alternate(event_code="E-2026-HZ-014", division_code="MD-19+",
                          entry_code="EN-03", vacated_entry_code="EN-02")
    assert svc.state.entry("E-2026-HZ-014", "EN-03").status == "PROMOTED"

    # --- 成绩：原报 -> 更正（不能重发原事件） ---
    r1 = svc.record_result(event_code="E-2026-HZ-014", division_code="MD-19+",
                           entry_code="EN-01", round_code="F", placing=2,
                           source_id="community-stn-014",
                           idempotency_key="result:E-2026-HZ-014:EN-01:F")
    r2 = svc.record_result(event_code="E-2026-HZ-014", division_code="MD-19+",
                           entry_code="EN-03", round_code="F", placing=1,
                           source_id="community-stn-014",
                           idempotency_key="result:E-2026-HZ-014:EN-03:F")
    correction = svc.correct_result(event_code="E-2026-HZ-014", division_code="MD-19+",
                                    entry_code="EN-01", round_code="F",
                                    corrected_placing=1, reason="记分表录入颠倒，裁判长复核更正",
                                    source_id="community-stn-014")

    # --- 积分批次（全国计分时规则与阵容必须已冻结） ---
    svc.open_point_batch(batch_code="PB-HZ014-1", event_code="E-2026-HZ-014")
    svc.compute_points(batch_code="PB-HZ014-1", lines=[
        {"participant_id": pid_c, "points": 100, "result_event_id": correction["event_id"]},
        {"participant_id": pid_d, "points": 100, "result_event_id": correction["event_id"]},
        {"participant_id": pid_g, "points": 60, "result_event_id": r2["event_id"]},
        {"participant_id": pid_a, "points": 60, "result_event_id": r2["event_id"]},
    ])
    svc.freeze_point_batch("PB-HZ014-1")

    # 旧结果事件（被更正前的第二名）不得再计分
    # --- 榜单首发 ---
    svc.release_ranking(release_code="RK-2026-W42", as_of="2026-10-20T00:00:00+08:00",
                        entries=[
                            {"participant_id": pid_c, "points": 100},
                            {"participant_id": pid_d, "points": 100},
                            {"participant_id": pid_g, "points": 60},
                            {"participant_id": pid_a, "points": 60},
                        ])

    # --- 赛后 DQ：批次已冻结 -> 更正批次 + 榜单更正版，旧版保留 ---
    dq = svc.issue_dq(case_id="CASE-009", event_code="E-2026-HZ-014",
                      division_code="MD-19+", entry_code="EN-03", match_round="F",
                      reason="器材抽检拍面厚度超标，赛后复核成立")
    assert dq["boundary"]["must_open_correction_batch"]

    svc.open_point_batch(batch_code="PB-HZ014-2", event_code="E-2026-HZ-014",
                         supersedes_batch="PB-HZ014-1")
    # EN-03 两人积分冲销为 0；其余行原样保留
    svc.compute_points(batch_code="PB-HZ014-2", lines=[
        {"participant_id": pid_c, "points": 100, "result_event_id": correction["event_id"]},
        {"participant_id": pid_d, "points": 100, "result_event_id": correction["event_id"]},
        {"participant_id": pid_g, "points": 0, "result_event_id": r2["event_id"],
         "reason": "CASE-009 DQ，冲销原 60 分"},
        {"participant_id": pid_a, "points": 0, "result_event_id": r2["event_id"],
         "reason": "CASE-009 DQ，冲销原 60 分"},
    ])
    svc.freeze_point_batch("PB-HZ014-2")
    svc.publish_ranking_correction(
        release_code="RK-2026-W42-C1", supersedes_release="RK-2026-W42",
        correction_seq=1, as_of="2026-10-22T00:00:00+08:00",
        entries=[
            {"participant_id": pid_c, "points": 100},
            {"participant_id": pid_d, "points": 100},
        ],
        reason="CASE-009 器材违规 DQ 成立，冲销 EN-03 积分")

    # --- 地方来源离线乱序上报（独立小来源演示重建） ---
    def raw(source_seq: int, occurred: str, event_type: str, agg: str, payload: dict,
            eid: str) -> dict:
        rec = {
            "event_id": eid, "event_type": event_type,
            "aggregate_type": "venue" if event_type == "VENUE_VERIFIED" else "sanctioned_event",
            "aggregate_id": agg, "occurred_at": occurred,
            "recorded_at": "2026-10-19T08:00:00+08:00",
            "version": 1, "summary": "离线补传", "source_id": "stn-offline-071",
            "source_seq": source_seq, "payload": payload,
        }
        validate_payload(rec)
        return rec

    late = [
        raw(2, "2026-10-18T17:00:00+08:00", "VENUE_VERIFIED", "V-LATE-01",
            {"venue_code": "V-LATE-01", "name": "临平备用场", "status": "PASS"},
            "evt-late-003"),
        raw(0, "2026-10-17T09:00:00+08:00", "EVENT_SANCTIONED", "E-LATE-01",
            {"event_code": "E-LATE-01", "level": "COMMUNITY", "name": "离线社区站",
             "starts_at": "2026-10-25T09:00:00+08:00",
             "ends_at": "2026-10-25T17:00:00+08:00", "organizer_id": "org-late-01"},
            "evt-late-001"),
    ]
    # 故意只到 seq 0、2，缺口 seq=1 可被发现
    reasons = svc.ingest_many(late)
    assert "gap_after_accept" in reasons
    assert svc.store.source_gaps("stn-offline-071") == [1]
    # 乱序到达不影响业务时序：按 occurred_at 重放时授权(10-17)在场地核验(10-18)之前
    replay_ids = [e.event_id for e in svc.store.all_events()
                  if e.event_id in {"evt-late-001", "evt-late-003"}]
    assert replay_ids == ["evt-late-001", "evt-late-003"]

    OUT.write_text(json.dumps(svc.store.dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {len(svc.store)} 条事件 -> {OUT}")
    print("林涛当前有效积分：", svc.state.effective_points(pid_c))
    print("宋涛积分台账（含冲销留痕）：")
    print(json.dumps(svc.point_ledger(pid_g), ensure_ascii=False, indent=2))
    print("榜单更正链：", [c["release_code"] for c in svc.ranking_corrections("RK-2026-W42")])


if __name__ == "__main__":
    main()
