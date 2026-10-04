"""命令服务：校验业务规则 -> 产生事件 -> 追加存储 -> 折叠状态。

命令面与上报面分离：
- 命令方法（sanction_event、freeze_lineup ……）在产生事件前跑策略校验，
  违反冻结/递补/重算边界会抛 :class:`RuleViolation`；
- :meth:`ingest` / :meth:`ingest_many` 用于接入外部来源的现成事件信封，
  不做业务拦截——离线、乱序上报必须能先入库，再靠重放还原状态。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import contract as C
from .events import EventStore, StoredEvent
from .errors import RuleViolation
from .policies import (
    readiness_report, national_counting_gate, propose_identity_match,
    can_submit_entry, can_replace_partner, can_freeze_lineup,
    can_promote_alternate, can_record_result, handle_dq_boundary,
    validate_point_lines, can_open_batch, can_freeze_batch,
    can_publish_correction,
)
from .state import RehydratedState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check(problems: list[str]) -> None:
    if problems:
        raise RuleViolation(problems)


class AssociationService:
    def __init__(self, store: EventStore | None = None, source_id: str = "national-platform") -> None:
        self.store = store or EventStore()
        self.source_id = source_id
        self.state = RehydratedState()
        for e in self.store.all_events():
            self.state.apply(e)
        self._local_seq: dict[str, int] = {}

    # ---------- 内部：发事件 ----------
    def _next_seq(self, source_id: str) -> int:
        wm = self.store.source_high_watermark(source_id)
        base = wm + 1 if wm is not None else 0
        seq = max(base, self._local_seq.get(source_id, -1) + 1)
        self._local_seq[source_id] = seq
        return seq

    def _version_for(self, aggregate_id: str) -> int:
        return len(self.store.replay(aggregate_id)) + 1

    def emit(self, event_type: str, aggregate_id: str, payload: dict, *,
             summary: str, occurred_at: datetime | None = None,
             source_id: str | None = None, idempotency_key: str | None = None,
             causes: list[str] | None = None, enforce_version: bool = True) -> dict:
        src = source_id or self.source_id
        ts = occurred_at or _now()
        record = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "aggregate_type": C.EVENT_AGGREGATE[event_type],
            "aggregate_id": aggregate_id,
            "occurred_at": ts.isoformat(),
            "recorded_at": _now().isoformat(),
            "version": self._version_for(aggregate_id) if enforce_version else 1,
            "summary": summary,
            "source_id": src,
            "source_seq": self._next_seq(src),
            "payload": payload,
        }
        if idempotency_key:
            record["idempotency_key"] = idempotency_key
        if causes:
            record["causes"] = causes
        result = self.store.append(record)
        if result.reason == "duplicate_idempotency_key":
            raise RuleViolation(
                f"幂等键 {idempotency_key} 已存在：禁止靠重发改变状态，"
                "补传更正必须发送纠正事件（RESULT_CORRECTED / POINT_BATCH_ADJUSTED）"
            )
        # 统一按业务时序重建，避免迟到事件（occurred_at 在过去）增量折叠错位。
        self.rebuild()
        return record

    def ingest(self, record: dict) -> str:
        """接入外部来源事件（不做业务拦截），返回入库结果原因。

        入库后整体重放：离线/乱序批次里事件可能按接收顺序先到，状态必须
        以 occurred_at 重放重建，而不是按到达顺序折叠。
        """
        r = self.store.append(record)
        self.rebuild()
        return r.reason

    def ingest_many(self, records: list[dict]) -> list[str]:
        reasons = [self.store.append(r).reason for r in records]
        self.rebuild()
        return reasons

    def rebuild(self) -> None:
        """按业务时序（occurred_at + source_seq）从事件存储重建全部投影。"""
        self.state = RehydratedState()
        for e in self.store.all_events():
            self.state.apply(e)

    # ---------- 赛事层级与授权 ----------
    def declare_tournament_level(self, level: str, name: str) -> dict:
        return self.emit(C.EV_LEVEL_DECLARED, level,
                         {"level": level, "name": name},
                         summary=f"登记赛事层级：{name}")

    def sanction_event(self, *, event_code: str, level: str, name: str,
                       starts_at: str, ends_at: str, organizer_id: str,
                       venue_code: str | None = None,
                       required_officials: list[dict] | None = None) -> dict:
        return self.emit(C.EV_EVENT_SANCTIONED, event_code, {
            "event_code": event_code, "level": level, "name": name,
            "starts_at": starts_at, "ends_at": ends_at, "organizer_id": organizer_id,
            "venue_code": venue_code, "required_officials": required_officials or [],
        }, summary=f"办赛授权：{name}（{level}）")

    # ---------- 场地与裁判 ----------
    def verify_venue(self, venue_code: str, name: str, status: str = "PASS") -> dict:
        return self.emit(C.EV_VENUE_VERIFIED, venue_code,
                         {"venue_code": venue_code, "name": name, "status": status},
                         summary=f"场地核验：{name} -> {status}")

    def certify_official(self, *, official_id: str, name: str, certificate_no: str,
                         cert_level: str, valid_from: str, valid_to: str) -> dict:
        return self.emit(C.EV_OFFICIAL_CERTIFIED, official_id, {
            "official_id": official_id, "name": name, "certificate_no": certificate_no,
            "cert_level": cert_level, "valid_from": valid_from, "valid_to": valid_to,
        }, summary=f"裁判资质登记：{name}（{cert_level} 级）")

    # ---------- 运动员身份 ----------
    def register_local_identity(self, *, source_id: str, local_member_no: str,
                                name: str, birth_date: str,
                                occurred_at: datetime | None = None) -> dict:
        """地方系统以本地会员号注册；自动生成独立档案，绝不按同名合并。"""
        agg = f"{source_id}/{local_member_no}"
        return self.emit(C.EV_IDENTITY_REGISTERED, agg, {
            "source_id": source_id, "local_member_no": local_member_no,
            "name": name, "birth_date": birth_date,
        }, summary=f"地方身份登记：{source_id}/{local_member_no}（{name}）",
            source_id=source_id, occurred_at=occurred_at)

    def propose_identity_match(self, *, match_id: str, candidate_a: str,
                               candidate_b: str, reason: str, confidence: float) -> dict:
        _check(propose_identity_match(self.state, candidate_a, candidate_b, reason))
        return self.emit(C.EV_MATCH_PROPOSED, match_id, {
            "match_id": match_id, "candidate_a": candidate_a,
            "candidate_b": candidate_b, "reason": reason, "confidence": confidence,
        }, summary=f"提出身份待核对：{candidate_a} ≟ {candidate_b}（{reason}）")

    def decide_identity_match(self, *, match_id: str, decision: str,
                              reviewer: str, target_participant_id: str | None = None) -> dict:
        match = self.state.matches.get(match_id)
        if match is None:
            raise RuleViolation(f"待核对匹配 {match_id} 不存在")
        if match.status != "PENDING":
            raise RuleViolation(f"匹配 {match_id} 已裁决为 {match.status}，不能重复裁决")
        if decision not in ("CONFIRMED", "REJECTED"):
            raise RuleViolation("decision 必须是 CONFIRMED 或 REJECTED")
        return self.emit(C.EV_MATCH_DECIDED, match_id, {
            "match_id": match_id, "decision": decision, "reviewer": reviewer,
            "target_participant_id": target_participant_id,
        }, summary=f"身份核对裁决：{match_id} -> {decision}（{reviewer}）",
            causes=[match.event_ids[0]])

    # ---------- 规则 ----------
    def define_ruleset(self, *, ruleset_code: str, version: str, level: str,
                       age_band_method: str, doubles_method: str,
                       equipment_method: str, extras: dict | None = None) -> dict:
        return self.emit(C.EV_RULESET_DEFINED, f"{ruleset_code}:{version}", {
            "ruleset_code": ruleset_code, "version": version, "level": level,
            "age_band_method": age_band_method, "doubles_method": doubles_method,
            "equipment_method": equipment_method, "extras": extras or {},
        }, summary=f"规则版本发布：{ruleset_code}@{version}")

    def freeze_ruleset_for_event(self, *, event_code: str, ruleset_code: str,
                                 ruleset_version: str) -> dict:
        if event_code not in self.state.events:
            raise RuleViolation(f"赛事 {event_code} 不存在")
        return self.emit(C.EV_RULESET_FROZEN, event_code, {
            "event_code": event_code, "ruleset_code": ruleset_code,
            "ruleset_version": ruleset_version,
        }, summary=f"赛事 {event_code} 冻结规则 {ruleset_code}@{ruleset_version}")

    # ---------- 组别与资格 ----------
    def open_division(self, *, event_code: str, division_code: str, discipline: str,
                      age_band: str, gender: str, max_entries: int) -> dict:
        return self.emit(C.EV_DIVISION_OPENED, f"{event_code}/{division_code}", {
            "event_code": event_code, "division_code": division_code,
            "discipline": discipline, "age_band": age_band, "gender": gender,
            "max_entries": max_entries,
        }, summary=f"开设组别：{event_code}/{division_code}")

    def grant_eligibility(self, *, event_code: str, division_code: str,
                          participant_id: str, basis: str) -> dict:
        return self.emit(C.EV_ELIGIBILITY_GRANTED, f"{event_code}/{division_code}", {
            "event_code": event_code, "division_code": division_code,
            "participant_id": participant_id, "basis": basis,
        }, summary=f"授予组别资格：{participant_id} -> {division_code}（{basis}）")

    def revoke_eligibility(self, *, event_code: str, division_code: str,
                           participant_id: str, reason: str) -> dict:
        return self.emit(C.EV_ELIGIBILITY_REVOKED, f"{event_code}/{division_code}", {
            "event_code": event_code, "division_code": division_code,
            "participant_id": participant_id, "reason": reason,
        }, summary=f"撤销组别资格：{participant_id}（{reason}）")

    # ---------- 报名与阵容 ----------
    def submit_entry(self, *, event_code: str, division_code: str, entry_code: str,
                     participant_ids: list[str], is_alternate: bool = False) -> dict:
        _check(can_submit_entry(self.state, event_code, division_code,
                                participant_ids, is_alternate))
        return self.emit(C.EV_ENTRY_SUBMITTED, f"{event_code}/{entry_code}", {
            "event_code": event_code, "division_code": division_code,
            "entry_code": entry_code, "participant_ids": participant_ids,
            "is_alternate": is_alternate,
        }, summary=f"报名：{entry_code} -> {event_code}/{division_code}")

    def replace_partner(self, *, event_code: str, division_code: str, entry_code: str,
                        out_participant_id: str, in_participant_id: str) -> dict:
        _check(can_replace_partner(self.state, event_code, division_code, entry_code,
                                   out_participant_id, in_participant_id))
        return self.emit(C.EV_PARTNER_REPLACED, f"{event_code}/{entry_code}", {
            "event_code": event_code, "division_code": division_code,
            "entry_code": entry_code, "out_participant_id": out_participant_id,
            "in_participant_id": in_participant_id,
        }, summary=f"搭档替换：{entry_code} {out_participant_id} -> {in_participant_id}")

    def freeze_lineup(self, *, event_code: str, division_code: str,
                      entry_codes: list[str]) -> dict:
        _check(can_freeze_lineup(self.state, event_code, division_code, entry_codes))
        return self.emit(C.EV_LINEUP_FROZEN, f"{event_code}/{division_code}", {
            "event_code": event_code, "division_code": division_code,
            "entry_codes": entry_codes,
        }, summary=f"阵容冻结：{event_code}/{division_code}（{len(entry_codes)} 队）")

    def declare_withdrawal(self, *, event_code: str, entry_code: str,
                           reason: str, occurred_at: datetime | None = None) -> dict:
        entry = self.state.entries.get((event_code, entry_code))
        if entry is None:
            raise RuleViolation(f"报名 {entry_code} 不存在")
        if entry.status == "WITHDRAWN":
            raise RuleViolation(f"报名 {entry_code} 已退赛")
        d = self.state.divisions[(event_code, entry.division_code)]
        after = d.lineup_frozen
        return self.emit(C.EV_WITHDRAWAL_DECLARED, f"{event_code}/{entry_code}", {
            "event_code": event_code, "entry_code": entry_code,
            "after_lineup_frozen": after, "reason": reason,
        }, summary=f"退赛：{entry_code}（{reason}）" + ("，冻阵后空缺席位待递补" if after else ""),
            occurred_at=occurred_at)

    def promote_alternate(self, *, event_code: str, division_code: str,
                          entry_code: str, vacated_entry_code: str) -> dict:
        d = self.state.division(event_code, division_code)
        sequence = d.alternate_seq + 1
        _check(can_promote_alternate(self.state, event_code, division_code,
                                     entry_code, vacated_entry_code, sequence))
        return self.emit(C.EV_ALTERNATE_PROMOTED, f"{event_code}/{entry_code}", {
            "event_code": event_code, "division_code": division_code,
            "entry_code": entry_code, "vacated_entry_code": vacated_entry_code,
            "sequence": sequence,
        }, summary=f"候补递补：{entry_code} 补 {vacated_entry_code}（第 {sequence} 顺位）")

    # ---------- 结果与纠错 ----------
    def record_result(self, *, event_code: str, division_code: str, entry_code: str,
                      round_code: str, placing: int,
                      source_id: str | None = None,
                      idempotency_key: str | None = None,
                      occurred_at: datetime | None = None) -> dict:
        _check(can_record_result(self.state, event_code, entry_code))
        return self.emit(C.EV_RESULT_RECEIVED,
                         f"{event_code}/{entry_code}/{round_code}", {
            "event_code": event_code, "division_code": division_code,
            "entry_code": entry_code, "round": round_code, "placing": placing,
        }, summary=f"成绩上报：{entry_code} {round_code} 第 {placing} 名",
            source_id=source_id, idempotency_key=idempotency_key, occurred_at=occurred_at)

    def correct_result(self, *, event_code: str, division_code: str, entry_code: str,
                       round_code: str, corrected_placing: int, reason: str,
                       source_id: str | None = None,
                       occurred_at: datetime | None = None) -> dict:
        """地方站补传更正：只能发纠正事件，不能重发原结果。"""
        key = (event_code, division_code, entry_code, round_code)
        if key not in self.state.results:
            raise RuleViolation(f"没有可纠正的原成绩：{entry_code}/{round_code}")
        cause = self.state.results[key].versions[-1].event_id
        return self.emit(C.EV_RESULT_CORRECTED,
                         f"{event_code}/{entry_code}/{round_code}", {
            "event_code": event_code, "division_code": division_code,
            "entry_code": entry_code, "round": round_code,
            "corrected_placing": corrected_placing, "reason": reason,
        }, summary=f"成绩更正：{entry_code} {round_code} -> 第 {corrected_placing} 名（{reason}）",
            source_id=source_id, causes=[cause], occurred_at=occurred_at)

    # ---------- 处罚与申诉 ----------
    def issue_disciplinary(self, *, case_id: str, event_code: str, entry_code: str,
                           rule: str, sanction: str, issued_at: str) -> dict:
        return self.emit(C.EV_DISCIPLINARY_ISSUED, case_id, {
            "event_code": event_code, "entry_code": entry_code,
            "rule": rule, "sanction": sanction, "issued_at": issued_at,
        }, summary=f"处罚：{case_id} {entry_code} 违反 {rule} -> {sanction}")

    def file_appeal(self, *, appeal_id: str, case_id: str, filed_by: str,
                    grounds: str) -> dict:
        if case_id not in self.state.cases:
            raise RuleViolation(f"处罚案件 {case_id} 不存在，无法申诉")
        return self.emit(C.EV_APPEAL_FILED, appeal_id, {
            "case_id": case_id, "filed_by": filed_by, "grounds": grounds,
        }, summary=f"申诉受理：{appeal_id} 针对 {case_id}")

    def decide_appeal(self, *, appeal_id: str, case_id: str, decision: str,
                      decided_at: str) -> dict:
        if appeal_id not in self.state.appeals:
            raise RuleViolation(f"申诉 {appeal_id} 不存在")
        if decision not in ("UPHELD", "DISMISSED"):
            raise RuleViolation("decision 必须是 UPHELD 或 DISMISSED")
        return self.emit(C.EV_APPEAL_DECIDED, appeal_id, {
            "case_id": case_id, "appeal_id": appeal_id,
            "decision": decision, "decided_at": decided_at,
        }, summary=f"申诉裁决：{appeal_id} -> {decision}",
            causes=[self.state.appeals[appeal_id]["event_ids"][0]])

    def issue_dq(self, *, case_id: str, event_code: str, division_code: str,
                 entry_code: str, match_round: str, reason: str,
                 occurred_at: datetime | None = None) -> dict:
        """赛后取消资格。返回重算边界说明，调用方据此编排更正批次/榜单。"""
        ts = occurred_at or _now()
        boundary = handle_dq_boundary(self.state, event_code, ts)
        record = self.emit(C.EV_DQ_ISSUED, case_id, {
            "case_id": case_id, "event_code": event_code,
            "division_code": division_code, "entry_code": entry_code,
            "match_round": match_round, "effective_stage": boundary["stage"],
            "reason": reason,
        }, summary=f"取消资格：{entry_code}（{reason}）；{boundary['explanation']}",
            occurred_at=ts)
        return {"event": record, "boundary": boundary}

    # ---------- 积分批次 ----------
    def open_point_batch(self, *, batch_code: str, event_code: str,
                         supersedes_batch: str | None = None) -> dict:
        _check(can_open_batch(self.state, event_code))
        ev = self.state.events[event_code]
        assert ev.frozen_ruleset is not None
        lineup_refs = [
            d.frozen_event_id for d in self.state.divisions.values()
            if d.event_code == event_code and d.frozen_event_id
        ]
        return self.emit(C.EV_POINT_BATCH_OPENED, batch_code, {
            "batch_code": batch_code, "event_code": event_code,
            "ruleset_code": ev.frozen_ruleset.code,
            "ruleset_version": ev.frozen_ruleset.version,
            "frozen_lineup_ref": ",".join(lineup_refs),
            "supersedes_batch": supersedes_batch,
        }, summary=f"积分批次开启：{batch_code}（赛事 {event_code}，规则与阵容已冻结）")

    def compute_points(self, *, batch_code: str, lines: list[dict]) -> dict:
        batch = self.state.batches[batch_code]
        _check(validate_point_lines(self.state, batch, lines))
        return self.emit(C.EV_POINT_BATCH_COMPUTED, batch_code, {
            "batch_code": batch_code, "lines": lines,
        }, summary=f"积分批次计算：{batch_code}（{len(lines)} 行）")

    def adjust_points(self, *, batch_code: str, lines: list[dict], reason: str,
                      cause_event_ids: list[str]) -> dict:
        """冻结批次不能调整；冻结后的变化必须开 supersedes 更正批次。"""
        batch = self.state.batches[batch_code]
        if batch.status == "FROZEN":
            raise RuleViolation(
                f"批次 {batch_code} 已冻结，禁止调整；请开启 supersedes_batch "
                f"指向它的更正批次，冻结批次原样保留")
        _check(validate_point_lines(self.state, batch, lines, allow_result_reuse=True))
        return self.emit(C.EV_POINT_BATCH_ADJUSTED, batch_code, {
            "batch_code": batch_code, "lines": lines, "reason": reason,
            "cause_event_ids": cause_event_ids,
        }, summary=f"积分批次调整：{batch_code}（{reason}）", causes=cause_event_ids)

    def freeze_point_batch(self, batch_code: str) -> dict:
        batch = self.state.batches[batch_code]
        _check(can_freeze_batch(batch))
        return self.emit(C.EV_POINT_BATCH_FROZEN, batch_code,
                         {"batch_code": batch_code},
                         summary=f"积分批次冻结：{batch_code}（此后只能出更正批次与榜单更正版）")

    # ---------- 榜单 ----------
    def release_ranking(self, *, release_code: str, as_of: str,
                        entries: list[dict]) -> dict:
        return self.emit(C.EV_RANKING_RELEASED, release_code, {
            "release_code": release_code, "as_of": as_of, "entries": entries,
        }, summary=f"榜单发布：{release_code}（截至 {as_of}）")

    def publish_ranking_correction(self, *, release_code: str, supersedes_release: str,
                                   correction_seq: int, entries: list[dict],
                                   reason: str, as_of: str | None = None) -> dict:
        """榜单快照只追加更正版：旧版保留，版次连续，不覆盖。"""
        _check(can_publish_correction(self.state, supersedes_release, correction_seq))
        payload = {
            "release_code": release_code, "correction_seq": correction_seq,
            "supersedes_release": supersedes_release,
            "entries": entries, "reason": reason,
        }
        if as_of:
            payload["as_of"] = as_of
        return self.emit(C.EV_RANKING_CORRECTED, release_code, payload,
                         summary=f"榜单更正版 v{correction_seq}：{release_code}（{reason}）")

    # ---------- 查询 ----------
    def readiness(self, event_code: str) -> dict:
        return readiness_report(self.state, event_code)

    def point_ledger(self, participant_id: str) -> list[dict]:
        return self.state.point_ledger(participant_id)

    def ranking_corrections(self, release_code: str) -> list[dict]:
        return [
            {"release_code": s.release_code, "correction_seq": s.correction_seq,
             "reason": s.reason, "event_id": s.event_id}
            for s in self.state.ranking_corrections(release_code)
        ]
