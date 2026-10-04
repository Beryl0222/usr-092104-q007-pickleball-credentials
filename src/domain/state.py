"""事件折叠出的可重放状态（投影）。

本模块只做"发生了什么 -> 现在是什么样"的纯折叠：
- 所有结论都带支撑它的事件 id（可追溯）；
- 纠正/申诉不删除旧记录，只追加新版本，形成连续更正链；
- 不做命令是否允许的判断（那是 policies/service 的职责）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import contract as C
from .events import StoredEvent


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ---------- 只读数据结构 ----------

@dataclass
class LocalRef:
    source_id: str
    local_member_no: str

    @property
    def key(self) -> str:
        return f"{self.source_id}/{self.local_member_no}"


@dataclass
class LocalRegistration:
    ref: LocalRef
    participant_id: str
    name: str
    birth_date: str
    event_ids: list[str] = field(default_factory=list)


@dataclass
class IdentityProfile:
    participant_id: str
    name: str
    birth_date: str
    locals: list[LocalRef] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)


@dataclass
class IdentityMatch:
    match_id: str
    a: str  # participant_id 或 local key
    b: str
    reason: str
    confidence: float
    status: str = "PENDING"  # PENDING / CONFIRMED / REJECTED
    reviewer: str | None = None
    decided_at: datetime | None = None
    event_ids: list[str] = field(default_factory=list)


@dataclass
class Ruleset:
    code: str
    version: str
    level: str
    age_band_method: str
    doubles_method: str
    equipment_method: str
    extras: dict = field(default_factory=dict)


@dataclass
class SanctionedEvent:
    event_code: str
    name: str
    level: str
    starts_at: datetime
    ends_at: datetime
    organizer_id: str
    venue_code: str | None = None
    required_officials: list[dict] = field(default_factory=list)
    frozen_ruleset: Ruleset | None = None
    frozen_ruleset_event_id: str | None = None
    frozen_at: datetime | None = None


@dataclass
class Venue:
    venue_code: str
    name: str
    status: str
    event_ids: list[str] = field(default_factory=list)


@dataclass
class Official:
    official_id: str
    name: str
    certificate_no: str
    cert_level: str
    valid_from: datetime
    valid_to: datetime
    event_ids: list[str] = field(default_factory=list)


@dataclass
class Division:
    event_code: str
    division_code: str
    discipline: str
    age_band: str
    gender: str
    max_entries: int
    eligibility: dict[str, str] = field(default_factory=dict)  # participant_id -> basis(event_id)
    eligibility_revoked: dict[str, str] = field(default_factory=dict)
    lineup_frozen: bool = False
    frozen_entry_codes: list[str] = field(default_factory=list)
    frozen_participants: dict[str, list[str]] = field(default_factory=dict)  # entry -> 选手
    frozen_event_id: str | None = None
    alternate_seq: int = 0
    vacancies: list[str] = field(default_factory=list)  # 冻阵后退出空出的 entry_code


@dataclass
class Entry:
    event_code: str
    division_code: str
    entry_code: str
    participant_ids: list[str]
    is_alternate: bool = False
    status: str = "SUBMITTED"  # SUBMITTED/WITHDRAWN/PROMOTED/DQ
    partner_history: list[dict] = field(default_factory=list)
    result_event_ids: list[str] = field(default_factory=list)
    promoted: dict | None = None


@dataclass
class ResultVersion:
    event_id: str
    kind: str  # RECEIVED / CORRECTED
    placing: int
    reason: str
    at: datetime


@dataclass
class MatchResult:
    event_code: str
    division_code: str
    entry_code: str
    round_code: str
    versions: list[ResultVersion] = field(default_factory=list)

    @property
    def latest(self) -> ResultVersion:
        return self.versions[-1]


@dataclass
class DisciplinaryCase:
    case_id: str
    event_code: str
    division_code: str
    entry_code: str
    match_round: str
    effective_stage: str  # BEFORE_BATCH / AFTER_BATCH_FROZEN
    reason: str
    issued_event_id: str
    standing: bool = True  # DQ 是否仍然有效（申诉成立可撤销）
    appeals: list[str] = field(default_factory=list)


@dataclass
class PointLine:
    participant_id: str
    points: float
    result_event_id: str
    reason: str = ""


@dataclass
class PointBatch:
    batch_code: str
    event_code: str
    ruleset_code: str
    ruleset_version: str
    frozen_lineup_ref: str
    supersedes_batch: str | None
    status: str = "OPEN"  # OPEN / COMPUTED / FROZEN
    lines: list[PointLine] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # compute/adjust/freeze 留痕
    cause_event_ids: list[str] = field(default_factory=list)


@dataclass
class RankingSnapshot:
    release_code: str
    correction_seq: int
    supersedes_release: str | None
    as_of: datetime
    entries: list[dict]
    reason: str
    event_id: str
    at: datetime


class RehydratedState:
    """从事件存储重放出的全部投影。"""

    def __init__(self) -> None:
        self.levels: dict[str, dict] = {}
        self.events: dict[str, SanctionedEvent] = {}
        self.venues: dict[str, Venue] = {}
        self.officials: dict[str, Official] = {}
        self.locals: dict[str, LocalRegistration] = {}
        self.profiles: dict[str, IdentityProfile] = {}
        self.matches: dict[str, IdentityMatch] = {}
        self.rulesets: dict[str, Ruleset] = {}  # code -> 最新版本
        self.divisions: dict[tuple[str, str], Division] = {}
        self.entries: dict[tuple[str, str], Entry] = {}
        self.results: dict[tuple[str, str, str, str], MatchResult] = {}
        self.cases: dict[str, DisciplinaryCase] = {}
        self.pending_dq: set[tuple[str, str]] = set()  # 乱序到达、报名事件尚未见到的 DQ 标记
        self.appeals: dict[str, dict] = {}
        self.batches: dict[str, PointBatch] = {}
        self.rankings: dict[str, RankingSnapshot] = {}
        self.ranking_chain: dict[str, list[str]] = {}  # 首发 code -> [releases]
        self.applied_events: list[str] = []

    # ---------- 折叠 ----------
    def apply(self, e: StoredEvent) -> None:
        t, p, eid = e.event_type, e.payload, e.event_id
        self.applied_events.append(eid)

        if t == C.EV_LEVEL_DECLARED:
            self.levels[p["level"]] = {"name": p["name"], "event_id": eid}

        elif t == C.EV_EVENT_SANCTIONED:
            self.events[p["event_code"]] = SanctionedEvent(
                event_code=p["event_code"],
                name=p["name"],
                level=p["level"],
                starts_at=_dt(p["starts_at"]),
                ends_at=_dt(p["ends_at"]),
                organizer_id=p["organizer_id"],
                venue_code=p.get("venue_code"),
                required_officials=list(p.get("required_officials", [])),
            )

        elif t == C.EV_VENUE_VERIFIED:
            self.venues[p["venue_code"]] = Venue(
                venue_code=p["venue_code"], name=p["name"], status=p["status"],
                event_ids=[*self.venues.get(p["venue_code"], Venue("", "", "")).event_ids, eid],
            )

        elif t == C.EV_OFFICIAL_CERTIFIED:
            prev = self.officials.get(p["official_id"])
            self.officials[p["official_id"]] = Official(
                official_id=p["official_id"],
                name=p["name"],
                certificate_no=p["certificate_no"],
                cert_level=p["cert_level"],
                valid_from=_dt(p["valid_from"]),
                valid_to=_dt(p["valid_to"]),
                event_ids=[*(prev.event_ids if prev else []), eid],
            )

        elif t == C.EV_IDENTITY_REGISTERED:
            ref = LocalRef(p["source_id"], p["local_member_no"])
            pid = p.get("participant_id") or f"P-{ref.key.replace('/', '-')}"
            self.locals[ref.key] = LocalRegistration(
                ref=ref, participant_id=pid, name=p["name"], birth_date=p["birth_date"],
                event_ids=[eid],
            )
            prof = self.profiles.get(pid)
            if prof is None:
                self.profiles[pid] = IdentityProfile(
                    participant_id=pid, name=p["name"], birth_date=p["birth_date"],
                    locals=[ref], event_ids=[eid],
                )
            else:
                prof.locals.append(ref)
                prof.event_ids.append(eid)

        elif t == C.EV_MATCH_PROPOSED:
            self.matches[p.get("match_id") or e.aggregate_id] = IdentityMatch(
                match_id=p.get("match_id") or e.aggregate_id,
                a=p["candidate_a"], b=p["candidate_b"],
                reason=p["reason"], confidence=float(p["confidence"]),
                event_ids=[eid],
            )

        elif t == C.EV_MATCH_DECIDED:
            m = self.matches[p["match_id"]]
            m.status = p["decision"]  # CONFIRMED / REJECTED
            m.reviewer = p["reviewer"]
            m.decided_at = e.occurred_at
            m.event_ids.append(eid)
            if p["decision"] == "CONFIRMED":
                target = p.get("target_participant_id")
                self._link_match(m, target)

        elif t == C.EV_RULESET_DEFINED:
            self.rulesets[p["ruleset_code"]] = Ruleset(
                code=p["ruleset_code"], version=p["version"], level=p["level"],
                age_band_method=p["age_band_method"], doubles_method=p["doubles_method"],
                equipment_method=p["equipment_method"], extras=p.get("extras", {}),
            )

        elif t == C.EV_RULESET_FROZEN:
            ev = self.events[p["event_code"]]
            rs = self.rulesets[p["ruleset_code"]]
            if rs.version != p["ruleset_version"]:
                rs = Ruleset(rs.code, p["ruleset_version"], rs.level,
                             rs.age_band_method, rs.doubles_method, rs.equipment_method, rs.extras)
            ev.frozen_ruleset = rs
            ev.frozen_ruleset_event_id = eid
            ev.frozen_at = e.occurred_at

        elif t == C.EV_DIVISION_OPENED:
            key = (p["event_code"], p["division_code"])
            self.divisions[key] = Division(
                event_code=p["event_code"], division_code=p["division_code"],
                discipline=p["discipline"], age_band=p["age_band"], gender=p["gender"],
                max_entries=p["max_entries"],
            )

        elif t == C.EV_ELIGIBILITY_GRANTED:
            d = self.divisions[(p["event_code"], p["division_code"])]
            d.eligibility_revoked.pop(p["participant_id"], None)
            d.eligibility[p["participant_id"]] = eid

        elif t == C.EV_ELIGIBILITY_REVOKED:
            d = self.divisions[(p["event_code"], p["division_code"])]
            d.eligibility.pop(p["participant_id"], None)
            d.eligibility_revoked[p["participant_id"]] = eid

        elif t in (C.EV_ENTRY_SUBMITTED, C.EV_ALTERNATE_PROMOTED):
            if t == C.EV_ENTRY_SUBMITTED:
                entry = Entry(
                    event_code=p["event_code"], division_code=p["division_code"],
                    entry_code=p["entry_code"], participant_ids=list(p["participant_ids"]),
                    is_alternate=bool(p.get("is_alternate", False)),
                )
                if (p["event_code"], p["entry_code"]) in self.pending_dq:
                    entry.status = "DQ"
                    self.pending_dq.discard((p["event_code"], p["entry_code"]))
            else:
                entry = self.entries[(p["event_code"], p["entry_code"])]
                entry.status = "PROMOTED"
                entry.promoted = {"vacates": p["vacated_entry_code"], "sequence": p["sequence"], "event_id": eid}
                d = self.divisions[(p["event_code"], p["division_code"])]
                if p["vacated_entry_code"] in d.vacancies:
                    d.vacancies.remove(p["vacated_entry_code"])
                d.alternate_seq = max(d.alternate_seq, p["sequence"])
            self.entries[(p["event_code"], p["entry_code"])] = entry

        elif t == C.EV_ENTRY_WITHDRAWN:
            entry = self.entries[(p["event_code"], p["entry_code"])]
            entry.status = "WITHDRAWN"
            entry.partner_history.append({"kind": "WITHDRAW", "reason": p["reason"], "event_id": eid})

        elif t == C.EV_PARTNER_REPLACED:
            entry = self.entries[(p["event_code"], p["entry_code"])]
            entry.participant_ids = [
                p["in_participant_id"] if x == p["out_participant_id"] else x
                for x in entry.participant_ids
            ]
            entry.partner_history.append({
                "kind": "REPLACE", "out": p["out_participant_id"],
                "in": p["in_participant_id"], "event_id": eid,
            })

        elif t == C.EV_LINEUP_FROZEN:
            d = self.divisions[(p["event_code"], p["division_code"])]
            d.lineup_frozen = True
            d.frozen_entry_codes = list(p["entry_codes"])
            d.frozen_participants = {
                code: list(self.entries[(p["event_code"], code)].participant_ids)
                for code in p["entry_codes"]
            }
            d.frozen_event_id = eid

        elif t == C.EV_WITHDRAWAL_DECLARED:
            entry = self.entries[(p["event_code"], p["entry_code"])]
            entry.status = "WITHDRAWN"
            entry.partner_history.append({"kind": "WITHDRAW", "reason": p["reason"], "event_id": eid})
            if p.get("after_lineup_frozen"):
                d = self.divisions[(p["event_code"], entry.division_code)]
                d.vacancies.append(p["entry_code"])

        elif t in (C.EV_RESULT_RECEIVED, C.EV_RESULT_CORRECTED):
            key = (p["event_code"], p["division_code"], p["entry_code"], p["round"])
            res = self.results.setdefault(
                key, MatchResult(p["event_code"], p["division_code"], p["entry_code"], p["round"])
            )
            placing = p["placing"] if t == C.EV_RESULT_RECEIVED else p["corrected_placing"]
            res.versions.append(ResultVersion(
                event_id=eid, kind="RECEIVED" if t == C.EV_RESULT_RECEIVED else "CORRECTED",
                placing=placing, reason=p.get("reason", ""), at=e.occurred_at,
            ))
            entry = self.entries.get((p["event_code"], p["entry_code"]))
            if entry is not None:
                entry.result_event_ids.append(eid)

        elif t == C.EV_DISCIPLINARY_ISSUED:
            # 非 DQ 的一般处罚留档在案事件本身即可，这里不建投影。
            return

        elif t == C.EV_DQ_ISSUED:
            case_id = p["case_id"]
            self.cases[case_id] = DisciplinaryCase(
                case_id=case_id, event_code=p["event_code"], division_code=p["division_code"],
                entry_code=p["entry_code"], match_round=p["match_round"],
                effective_stage=p["effective_stage"], reason=p["reason"],
                issued_event_id=eid,
            )
            entry = self.entries.get((p["event_code"], p["entry_code"]))
            if entry is not None:
                entry.status = "DQ"
            else:
                # 乱序重放：DQ 事件先于报名事件折叠时挂起，报名建立后补标。
                self.pending_dq.add((p["event_code"], p["entry_code"]))

        elif t == C.EV_APPEAL_FILED:
            self.appeals[e.aggregate_id] = {
                "appeal_id": e.aggregate_id, "case_id": p["case_id"],
                "filed_by": p["filed_by"], "grounds": p["grounds"],
                "status": "FILED", "event_ids": [eid],
            }
            case = self.cases[p["case_id"]]
            case.appeals.append(e.aggregate_id)

        elif t == C.EV_APPEAL_DECIDED:
            appeal = self.appeals[p["appeal_id"]]
            appeal["status"] = p["decision"]  # UPHELD（申诉成立）/ DISMISSED
            appeal["decided_at"] = e.occurred_at
            appeal["event_ids"].append(eid)
            case = self.cases[p["case_id"]]
            if p["decision"] == "UPHELD":
                case.standing = False
                entry = self.entries[(case.event_code, case.entry_code)]
                if entry.status == "DQ":
                    entry.status = "PROMOTED" if entry.promoted else "SUBMITTED"
            elif p["decision"] == "DISMISSED":
                case.standing = True

        elif t in (C.EV_POINT_BATCH_OPENED, C.EV_POINT_BATCH_COMPUTED,
                   C.EV_POINT_BATCH_ADJUSTED, C.EV_POINT_BATCH_FROZEN):
            self._apply_batch_event(t, p, eid, e.occurred_at)

        elif t == C.EV_RANKING_RELEASED:
            snap = RankingSnapshot(
                release_code=p["release_code"], correction_seq=0,
                supersedes_release=None, as_of=_dt(p["as_of"]),
                entries=list(p["entries"]), reason="首发榜单", event_id=eid, at=e.occurred_at,
            )
            self.rankings[p["release_code"]] = snap
            self.ranking_chain[p["release_code"]] = [p["release_code"]]

        elif t == C.EV_RANKING_CORRECTED:
            chain_root = p["supersedes_release"]
            root = next((r for r, chain in self.ranking_chain.items() if chain_root in chain), chain_root)
            snap = RankingSnapshot(
                release_code=p["release_code"],
                correction_seq=p["correction_seq"],
                supersedes_release=chain_root,
                as_of=_dt(p["as_of"]) if "as_of" in p else self.rankings[chain_root].as_of,
                entries=list(p["entries"]), reason=p["reason"], event_id=eid, at=e.occurred_at,
            )
            self.rankings[p["release_code"]] = snap
            self.ranking_chain.setdefault(root, [root]).append(p["release_code"])

    def _link_match(self, m: IdentityMatch, target: str | None) -> None:
        """人工确认同一人：把两个身份下的本地会员号挂到保留档案，不删旧档。"""
        pa = self._resolve_participant(m.a)
        pb = self._resolve_participant(m.b)
        if pa is None or pb is None or pa == pb:
            return
        survivor_id = target or pa
        keep = self.profiles[survivor_id]
        gone_id = pb if survivor_id == pa else pa
        gone = self.profiles.pop(gone_id)
        for ref in gone.locals:
            if ref not in keep.locals:
                keep.locals.append(ref)
            self.locals[ref.key].participant_id = keep.participant_id
        keep.event_ids.extend(gone.event_ids)

    def _resolve_participant(self, token: str) -> str | None:
        if token in self.profiles:
            return token
        reg = self.locals.get(token)
        return reg.participant_id if reg else None

    def _apply_batch_event(self, t: str, p: dict, eid: str, at: datetime) -> None:
        if t == C.EV_POINT_BATCH_OPENED:
            self.batches[p["batch_code"]] = PointBatch(
                batch_code=p["batch_code"], event_code=p["event_code"],
                ruleset_code=p["ruleset_code"], ruleset_version=p["ruleset_version"],
                frozen_lineup_ref=p["frozen_lineup_ref"],
                supersedes_batch=p.get("supersedes_batch"),
                history=[{"kind": "OPEN", "event_id": eid, "at": at.isoformat()}],
            )
            return

        batch = self.batches[p["batch_code"]]
        if t == C.EV_POINT_BATCH_COMPUTED:
            batch.lines = [
                PointLine(l["participant_id"], float(l["points"]), l["result_event_id"],
                          l.get("reason", ""))
                for l in p["lines"]
            ]
            batch.status = "COMPUTED"
            batch.history.append({"kind": "COMPUTE", "event_id": eid, "at": at.isoformat()})
        elif t == C.EV_POINT_BATCH_ADJUSTED:
            batch.lines = [
                PointLine(l["participant_id"], float(l["points"]), l["result_event_id"],
                          l.get("reason", ""))
                for l in p["lines"]
            ]
            batch.cause_event_ids.extend(p.get("cause_event_ids", []))
            batch.history.append({
                "kind": "ADJUST", "reason": p["reason"],
                "causes": p.get("cause_event_ids", []), "event_id": eid, "at": at.isoformat(),
            })
        elif t == C.EV_POINT_BATCH_FROZEN:
            batch.status = "FROZEN"
            batch.history.append({"kind": "FREEZE", "event_id": eid, "at": at.isoformat()})

    # ---------- 查询 ----------
    def division(self, event_code: str, division_code: str) -> Division:
        return self.divisions[(event_code, division_code)]

    def entry(self, event_code: str, entry_code: str) -> Entry:
        return self.entries[(event_code, entry_code)]

    def is_eligible(self, event_code: str, division_code: str, participant_id: str) -> bool:
        d = self.divisions[(event_code, division_code)]
        return participant_id in d.eligibility

    def standing_dq_entries(self, event_code: str) -> set[str]:
        return {c.entry_code for c in self.cases.values()
                if c.event_code == event_code and c.standing}

    def event_batches(self, event_code: str) -> list[PointBatch]:
        return [b for b in self.batches.values() if b.event_code == event_code]

    def frozen_batch_for(self, event_code: str) -> PointBatch | None:
        frozen = [b for b in self.event_batches(event_code) if b.status == "FROZEN"]
        return frozen[-1] if frozen else None

    def latest_ranking(self) -> RankingSnapshot | None:
        ordered = sorted(self.rankings.values(), key=lambda s: (s.at, s.correction_seq))
        return ordered[-1] if ordered else None

    def ranking_corrections(self, release_code: str) -> list[RankingSnapshot]:
        root = next((r for r, chain in self.ranking_chain.items() if release_code in chain), release_code)
        return [self.rankings[c] for c in self.ranking_chain.get(root, [release_code])]

    def point_ledger(self, participant_id: str) -> list[dict]:
        """选手视角：每一笔积分来自哪个批次、哪条获认可比赛结果。

        被更正批次替代（supersedes_batch）的旧行保留但标注 VOIDED，
        保证"每笔积分都能解释"，且当前有效积分不把冲销行计入。
        """
        superseded = {b.supersedes_batch for b in self.batches.values()
                      if b.supersedes_batch}
        ledger = []
        for batch in self.batches.values():
            for line in batch.lines:
                if line.participant_id == participant_id:
                    ledger.append({
                        "batch_code": batch.batch_code,
                        "event_code": batch.event_code,
                        "points": line.points,
                        "result_event_id": line.result_event_id,
                        "reason": line.reason,
                        "ruleset": f"{batch.ruleset_code}@{batch.ruleset_version}",
                        "status": "VOIDED" if batch.batch_code in superseded
                                  else batch.status,
                        "superseded_by": next(
                            (b.batch_code for b in self.batches.values()
                             if b.supersedes_batch == batch.batch_code), None),
                    })
        return ledger

    def effective_points(self, participant_id: str) -> float:
        return sum(item["points"] for item in self.point_ledger(participant_id)
                   if item["status"] != "VOIDED")
