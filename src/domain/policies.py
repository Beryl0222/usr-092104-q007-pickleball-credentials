"""领域策略：就绪核验与各类变更边界。

策略函数只读取 :class:`~src.domain.state.RehydratedState`，返回问题列表或
计算结果，不直接产生事件；命令服务据此决定接受或拒绝。
"""

from __future__ import annotations

from datetime import datetime

from . import contract as C
from .state import RehydratedState, SanctionedEvent, Division, PointBatch

# 裁判证书级别（数字越大级别越高）
CERT_LEVEL_RANK = {"C": 1, "B": 2, "A": 3, "NATIONAL_REFEREE": 4}

# 阵容冻结后禁止的动作
POST_FREEZE_FORBIDDEN = "阵容冻结后禁止{action}；如需变更只能退赛并由候补充置"


def _now(ts: datetime | None) -> datetime:
    return ts or datetime.now().astimezone()


# ---------- 赛前就绪 ----------

def readiness_report(state: RehydratedState, event_code: str) -> dict:
    """承办方开赛前视角：缺少哪些授权或合格人员。

    返回 {"ready": bool, "missing": [...], "checks": {...}}，缺失项以
    中文可读描述给出，可直接反馈给承办方。
    """
    missing: list[str] = []
    checks: dict[str, object] = {}

    ev = state.events.get(event_code)
    if ev is None:
        return {"ready": False, "missing": ["赛事尚未获办赛授权（EVENT_SANCTIONED）"], "checks": {}}

    venue_ok = False
    if ev.venue_code:
        venue = state.venues.get(ev.venue_code)
        venue_ok = venue is not None and venue.status == "PASS"
        if venue is None:
            missing.append(f"场地 {ev.venue_code} 未核验")
        elif venue.status != "PASS":
            missing.append(f"场地 {venue.name} 核验状态为 {venue.status}，未通过")
    else:
        missing.append("赛事未登记场地")
    checks["venue"] = venue_ok

    # 按级别从高到低分配岗位，同一名裁判不能同时占两个岗位人次。
    pool = [
        o for o in state.officials.values()
        if o.valid_from <= ev.starts_at <= o.valid_to
    ]
    officials_detail = []
    for req in sorted(ev.required_officials,
                      key=lambda r: CERT_LEVEL_RANK.get(r.get("min_cert_level", "C"), 1),
                      reverse=True):
        role = req["role"]
        need_level = req.get("min_cert_level", "C")
        count = int(req.get("count", 1))
        qualified = [o for o in pool if CERT_LEVEL_RANK.get(o.cert_level, 0)
                     >= CERT_LEVEL_RANK.get(need_level, 1)]
        assigned = qualified[:count]
        for o in assigned:
            pool.remove(o)
        officials_detail.append({"role": role, "required": count, "available": len(assigned)})
        if len(assigned) < count:
            missing.append(
                f"{role} 缺合格裁判：需 {count} 名（{need_level} 级及以上、证书在有效期），"
                f"仅 {len(assigned)} 名可上岗"
            )
    checks["officials"] = officials_detail

    divisions = [d for d in state.divisions.values() if d.event_code == event_code]
    # 组别/报名是授权后的后续阶段：提示但不计入"授权与人员就绪"的阻断项。
    warnings: list[str] = []
    if not divisions:
        warnings.append("尚未开设任何组别")
    checks["divisions"] = len(divisions)

    ineligible_entries = []
    for d in divisions:
        for code in d.frozen_entry_codes or _division_entry_codes(state, d):
            entry = state.entries.get((event_code, code))
            if entry is None or entry.status in ("WITHDRAWN", "DQ"):
                continue
            for pid in entry.participant_ids:
                if not state.is_eligible(event_code, d.division_code, pid):
                    ineligible_entries.append(f"{code}:{pid}")
    if ineligible_entries:
        warnings.append("以下报名选手无本组别资格：" + "、".join(ineligible_entries))
    checks["ineligible_entries"] = ineligible_entries

    return {"ready": not missing, "missing": missing,
            "warnings": warnings, "checks": checks}


def _division_entry_codes(state: RehydratedState, d: Division) -> list[str]:
    return [code for (ec, code), e in state.entries.items()
            if ec == d.event_code and e.division_code == d.division_code]


# ---------- 全国计分门槛 ----------

def national_counting_gate(state: RehydratedState, event_code: str) -> list[str]:
    """计入全国体系前：规则与阵容必须冻结。"""
    problems: list[str] = []
    ev = state.events.get(event_code)
    if ev is None:
        return ["赛事不存在"]
    if ev.frozen_ruleset is None:
        problems.append("赛事尚未冻结所用规则（RULESET_FROZEN_FOR_EVENT）")
    divisions_for_event = [d for d in state.divisions.values() if d.event_code == event_code]
    if not divisions_for_event:
        problems.append("赛事尚未开设任何组别，无阵容可冻结（DIVISION_OPENED/LINEUP_FROZEN）")
    for d in divisions_for_event:
        if not d.lineup_frozen:
            problems.append(f"组别 {d.division_code} 阵容未冻结（LINEUP_FROZEN）")
            continue
        # 冻阵后允许"退赛 -> 候补递补"：冻结快照里的退赛者必须有已完成的递补。
        promoted_into: dict[str, str] = {}  # vacated -> promoted entry
        for code in _division_entry_codes(state, d):
            entry = state.entries.get((event_code, code))
            if entry is not None and entry.promoted:
                promoted_into[entry.promoted["vacates"]] = code
        for code in d.frozen_entry_codes:
            entry = state.entries.get((event_code, code))
            if entry is None:
                problems.append(f"冻结阵容中的报名 {code} 不存在")
            elif entry.status == "WITHDRAWN":
                if code in promoted_into:
                    continue  # 已有候补递补，阵容闭合
                problems.append(
                    f"冻结阵容中的 {code} 已退赛且未见候补递补（ALTERNATE_PROMOTED）"
                )
            # DQ 是赛后事件，不阻断开批次（积分边界由 DQ 处理流程负责）。
        # 仍未闭合的冻阵后空缺也要报出
        for code in d.vacancies:
            if code not in promoted_into:
                problems.append(f"组别 {d.division_code} 因 {code} 退赛空缺席位尚未递补")
    return problems


# ---------- 身份：同名只能待核对 ----------

def propose_identity_match(state: RehydratedState, candidate_a: str,
                           candidate_b: str, reason: str) -> list[str]:
    problems = []
    if state._resolve_participant(candidate_a) is None:
        problems.append(f"候选 {candidate_a} 无法解析到任何身份档案")
    if state._resolve_participant(candidate_b) is None:
        problems.append(f"候选 {candidate_b} 无法解析到任何身份档案")
    if candidate_a == candidate_b:
        problems.append("不能对同一候选发起匹配")
    if not reason:
        problems.append("必须给出待核对理由（同名/证件线索等）")
    return problems


# ---------- 报名/阵容变更边界 ----------

def can_submit_entry(state: RehydratedState, event_code: str, division_code: str,
                     participant_ids: list[str], is_alternate: bool = False) -> list[str]:
    problems: list[str] = []
    d = state.divisions.get((event_code, division_code))
    if d is None:
        problems.append(f"组别 {division_code} 未开设")
        return problems
    if d.lineup_frozen:
        problems.append(POST_FREEZE_FORBIDDEN.format(action="新增报名"))
        return problems
    active = [code for code in _division_entry_codes(state, d)
              if not state.entries[(event_code, code)].is_alternate
              and state.entries[(event_code, code)].status not in ("WITHDRAWN", "DQ")]
    # 候补不占正赛名额，不受上限拦截；冻结后仍一律禁止新增。
    if not is_alternate and len(active) >= d.max_entries:
        problems.append(f"组别 {division_code} 正赛名额已满（{d.max_entries}），只能报候补")
    for pid in participant_ids:
        if not state.is_eligible(event_code, division_code, pid):
            problems.append(f"选手 {pid} 未获得 {division_code} 组别资格")
        # 同组一人只能占一个名额（含候补），跨组参赛在各自组别分别授予资格即可
        for code in _division_entry_codes(state, d):
            if (state.entries[(event_code, code)].status not in ("WITHDRAWN", "DQ")
                    and pid in state.entries[(event_code, code)].participant_ids):
                problems.append(f"选手 {pid} 已在同组报名 {code} 中，不能重复占位")
    return problems


def can_replace_partner(state: RehydratedState, event_code: str, division_code: str,
                        entry_code: str, out_pid: str, in_pid: str) -> list[str]:
    problems: list[str] = []
    d = state.divisions.get((event_code, division_code))
    entry = state.entries.get((event_code, entry_code))
    if d is None or entry is None:
        return ["组别或报名不存在"]
    if entry.status in ("WITHDRAWN", "DQ"):
        problems.append(f"报名 {entry_code} 状态为 {entry.status}，不能替换搭档")
    if d.lineup_frozen:
        problems.append(POST_FREEZE_FORBIDDEN.format(action="替换搭档") +
                        "：冻阵后只能整队退赛并由候补递补")
    if out_pid not in entry.participant_ids:
        problems.append(f"被替换选手 {out_pid} 不在报名 {entry_code} 中")
    if in_pid in entry.participant_ids:
        problems.append(f"替入选手 {in_pid} 已在该报名中")
    if not state.is_eligible(event_code, division_code, in_pid):
        problems.append(f"替入选手 {in_pid} 未获得 {division_code} 组别资格")
    return problems


def can_freeze_lineup(state: RehydratedState, event_code: str, division_code: str,
                      entry_codes: list[str]) -> list[str]:
    problems: list[str] = []
    d = state.divisions.get((event_code, division_code))
    if d is None:
        return ["组别不存在"]
    active = [code for code in _division_entry_codes(state, d)
              if state.entries[(event_code, code)].status not in ("WITHDRAWN", "DQ")]
    if set(entry_codes) - set(active):
        problems.append("冻结名单含不存在或已退出的报名："
                        + "、".join(sorted(set(entry_codes) - set(active))))
    if len(entry_codes) > d.max_entries:
        problems.append(f"冻结名单 {len(entry_codes)} 队超过组别上限 {d.max_entries}")
    seen: dict[str, str] = {}
    for code in entry_codes:
        for pid in state.entries[(event_code, code)].participant_ids:
            if not state.is_eligible(event_code, division_code, pid):
                problems.append(f"选手 {pid} 在 {code} 中但无组别资格")
            if pid in seen:
                problems.append(f"选手 {pid} 同时出现在 {seen[pid]} 与 {code}")
            seen[pid] = code
    return problems


def can_promote_alternate(state: RehydratedState, event_code: str, division_code: str,
                          entry_code: str, vacated_entry_code: str, sequence: int) -> list[str]:
    problems: list[str] = []
    d = state.divisions.get((event_code, division_code))
    alt = state.entries.get((event_code, entry_code))
    vac = state.entries.get((event_code, vacated_entry_code))
    if d is None or alt is None or vac is None:
        return ["组别、候补报名或空出名额不存在"]
    if not alt.is_alternate:
        problems.append(f"{entry_code} 不是候补报名")
    if alt.status != "SUBMITTED":
        problems.append(f"候补 {entry_code} 状态为 {alt.status}，不可递补")
    if vacated_entry_code not in d.vacancies:
        problems.append(f"{vacated_entry_code} 不在冻阵后空缺席位中（退赛必须发生在阵容冻结后）")
    if sequence != d.alternate_seq + 1:
        problems.append(f"递补序号必须为 {d.alternate_seq + 1}（按候补顺序，不得跳号）")
    for pid in alt.participant_ids:
        if not state.is_eligible(event_code, division_code, pid):
            problems.append(f"递补选手 {pid} 无组别资格")
    return problems


# ---------- 结果与 DQ / 重算边界 ----------

def can_record_result(state: RehydratedState, event_code: str, entry_code: str) -> list[str]:
    entry = state.entries.get((event_code, entry_code))
    if entry is None:
        return [f"报名 {entry_code} 不存在"]
    if entry.status == "WITHDRAWN":
        return [f"报名 {entry_code} 已退赛，不能记录成绩"]
    return []


def dq_effective_stage(state: RehydratedState, event_code: str,
                       occurred_at: datetime) -> str:
    """判定 DQ 在积分冻结之前还是之后——决定走重算还是更正版。"""
    batch = state.frozen_batch_for(event_code)
    if batch is None:
        return "BEFORE_BATCH"
    freeze_at = next(h["at"] for h in batch.history if h["kind"] == "FREEZE")
    return "AFTER_BATCH_FROZEN" if occurred_at.isoformat() >= freeze_at else "BEFORE_BATCH"


def handle_dq_boundary(state: RehydratedState, event_code: str,
                       occurred_at: datetime) -> dict:
    """给出 DQ 后的处理边界，供服务编排后续批次事件。

    关键不变量：**冻结批次永不改写**。因此处理方式取决于"到达时批次状态"，
    ``stage`` 记录违规原发生时间相对冻结时刻的位置，用于因果追溯。

    - 尚无冻结批次：成绩作废，未冻结批次按更正成绩重算/调整；
    - 批次已冻结（含违规发生在冻结前、但裁决/上报迟到的情形）：开更正批次
      （supersedes_batch 指向旧批次）冲销补发；已发榜的再发榜单更正版。
    """
    stage = dq_effective_stage(state, event_code, occurred_at)
    batch = state.frozen_batch_for(event_code)
    ranking_exists = state.latest_ranking() is not None
    return {
        "stage": stage,
        "late_report_anomaly": stage == "BEFORE_BATCH" and batch is not None,
        "frozen_batch": batch.batch_code if batch else None,
        "must_open_correction_batch": bool(batch),
        "must_publish_ranking_correction": bool(batch) and ranking_exists,
        "explanation": (
            "积分批次已冻结（或为冻前迟到裁决）：禁止改写，须开更正批次冲销补发，"
            "已发布榜单须出连续更正版"
            if batch else
            "积分尚未冻结：成绩作废后在计入全国体系前重算（调整当前批次）"
        ),
    }


# ---------- 积分防重复 ----------

def validate_point_lines(state: RehydratedState, batch: PointBatch,
                         lines: list[dict], *, allow_result_reuse: bool = False) -> list[str]:
    problems: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    for line in lines:
        pair = (line["participant_id"], line["result_event_id"])
        if pair in seen_pairs:
            problems.append(
                f"批次内重复计积：选手 {line['participant_id']} + 结果 "
                f"{line['result_event_id']} 出现多次"
            )
        seen_pairs.add(pair)
        # 结果事件必须真实存在且为最新版本——地方站补传更正后，旧 RESULT_RECEIVED
        # 不得再被计分。
        result_eid = line["result_event_id"]
        found = None
        for res in state.results.values():
            if res.versions[-1].event_id == result_eid:
                found = res
                break
            if any(v.event_id == result_eid for v in res.versions):
                problems.append(
                    f"结果 {result_eid} 已被更正，不能再用于计分；须使用最新 RESULT_CORRECTED"
                )
        if found is None and not any(v.event_id == result_eid for res in state.results.values()
                                     for v in res.versions):
            problems.append(f"结果事件 {result_eid} 不存在")
        elif found is not None:
            entry = state.entries.get((batch.event_code, found.entry_code))
            if entry is not None and entry.status == "DQ":
                standing = state.standing_dq_entries(batch.event_code)
                if found.entry_code in standing and float(line["points"]) != 0:
                    problems.append(
                        f"报名 {found.entry_code} DQ 仍有效，其结果 {result_eid} "
                        "只能以 0 分冲销行计入更正批次"
                    )

    if not allow_result_reuse:
        # 同一选手同一结果，在同一赛事"仍有效"的批次中只能计一次。
        # 已被更正批次替代（supersedes_batch 指向）的旧批次视为冲销，不再阻断；
        # 冻结后用新结果事件纠正时自然换了 result_event_id，也不会触发本规则。
        superseded = {b.supersedes_batch for b in state.event_batches(batch.event_code)
                      if b.supersedes_batch}
        for other in state.event_batches(batch.event_code):
            if other.batch_code == batch.batch_code or other.batch_code in superseded:
                continue
            for old in other.lines:
                if (old.participant_id, old.result_event_id) in seen_pairs:
                    problems.append(
                        f"结果 {old.result_event_id} 已在批次 {other.batch_code} 计过："
                        f"补传不得造成全国积分重复计算；请走批次更正并冲销旧行"
                    )
    return problems


def can_open_batch(state: RehydratedState, event_code: str) -> list[str]:
    problems = national_counting_gate(state, event_code)
    readiness = readiness_report(state, event_code)
    if not readiness["ready"]:
        problems.append("赛前就绪未满足：" + "；".join(readiness["missing"]))
    return problems


def can_freeze_batch(batch: PointBatch) -> list[str]:
    problems = []
    if batch.status == "OPEN":
        problems.append("批次尚未计算积分")
    if batch.status == "FROZEN":
        problems.append("批次已冻结，冻结不可重复")
    return problems


# ---------- 榜单更正版 ----------

def can_publish_correction(state: RehydratedState, release_code: str,
                           correction_seq: int) -> list[str]:
    problems = []
    prior = [r for r in state.rankings.values()
             if r.release_code == release_code
             or r.supersedes_release == release_code]
    if not prior:
        problems.append(f"榜单 {release_code} 不存在，不能发更正版（首发请用 RANKING_RELEASED）")
        return problems
    if any(r.correction_seq == correction_seq for r in prior):
        problems.append(f"更正版次 {correction_seq} 已存在，版次必须连续且不重复")
    return problems
