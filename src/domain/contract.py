"""赛历资历互认领域契约：事件枚举、聚合枚举与信封/负载校验。

所有来源必须通过 :class:`EventEnvelope` 定义的字段接入。校验分两层：

1. 信封校验（字段、类型、时序键）——见 :func:`validate_envelope`；
2. 负载校验（各事件类型必填字段）——见 :func:`validate_payload`。

校验只负责"形状"，业务规则（冻结、递补、重算边界）在 domain 包内实现。
"""

from __future__ import annotations

from datetime import datetime

# --- 赛事层级（不同层级可用各自规则，全国计分时再冻结） ---
TOURNAMENT_LEVELS = (
    "COMMUNITY",        # 社区赛
    "PROVINCIAL_QUAL",  # 省运会资格赛
    "COMMERCIAL_INVITE",  # 商业邀请赛
    "NATIONAL",         # 全国巡回
)

# --- 聚合类型 ---
AGG_TOURNAMENT_LEVEL = "tournament_level"
AGG_SANCTIONED_EVENT = "sanctioned_event"
AGG_VENUE = "venue"
AGG_OFFICIAL = "official_credential"
AGG_IDENTITY = "participant_identity"
AGG_IDENTITY_MATCH = "identity_match"
AGG_RULESET = "ruleset"
AGG_DIVISION = "division"
AGG_ENTRY = "entry"
AGG_RESULT = "match_result"
AGG_DISCIPLINARY = "disciplinary_case"
AGG_APPEAL = "appeal"
AGG_POINT_BATCH = "point_batch"
AGG_RANKING = "ranking_release"

AGGREGATE_TYPES = (
    AGG_TOURNAMENT_LEVEL,
    AGG_SANCTIONED_EVENT,
    AGG_VENUE,
    AGG_OFFICIAL,
    AGG_IDENTITY,
    AGG_IDENTITY_MATCH,
    AGG_RULESET,
    AGG_DIVISION,
    AGG_ENTRY,
    AGG_RESULT,
    AGG_DISCIPLINARY,
    AGG_APPEAL,
    AGG_POINT_BATCH,
    AGG_RANKING,
)

# --- 事件类型 ---
EV_LEVEL_DECLARED = "TOURNAMENT_LEVEL_DECLARED"
EV_EVENT_SANCTIONED = "EVENT_SANCTIONED"
EV_VENUE_VERIFIED = "VENUE_VERIFIED"
EV_OFFICIAL_CERTIFIED = "OFFICIAL_CERTIFIED"
EV_IDENTITY_REGISTERED = "IDENTITY_REGISTERED"
EV_MATCH_PROPOSED = "IDENTITY_MATCH_PROPOSED"
EV_MATCH_DECIDED = "IDENTITY_MATCH_DECIDED"
EV_RULESET_DEFINED = "RULESET_DEFINED"
EV_RULESET_FROZEN = "RULESET_FROZEN_FOR_EVENT"
EV_DIVISION_OPENED = "DIVISION_OPENED"
EV_ELIGIBILITY_GRANTED = "ELIGIBILITY_GRANTED"
EV_ELIGIBILITY_REVOKED = "ELIGIBILITY_REVOKED"
EV_ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
EV_ENTRY_WITHDRAWN = "ENTRY_WITHDRAWN"
EV_PARTNER_REPLACED = "PARTNER_REPLACED"
EV_LINEUP_FROZEN = "LINEUP_FROZEN"
EV_WITHDRAWAL_DECLARED = "WITHDRAWAL_DECLARED"
EV_ALTERNATE_PROMOTED = "ALTERNATE_PROMOTED"
EV_RESULT_RECEIVED = "RESULT_RECEIVED"
EV_RESULT_CORRECTED = "RESULT_CORRECTED"
EV_DISCIPLINARY_ISSUED = "MATCH_DISCIPLINARY_ISSUED"
EV_APPEAL_FILED = "APPEAL_FILED"
EV_APPEAL_DECIDED = "APPEAL_DECIDED"
EV_DQ_ISSUED = "DQ_ISSUED"
EV_POINT_BATCH_OPENED = "POINT_BATCH_OPENED"
EV_POINT_BATCH_COMPUTED = "POINT_BATCH_COMPUTED"
EV_POINT_BATCH_ADJUSTED = "POINT_BATCH_ADJUSTED"
EV_POINT_BATCH_FROZEN = "POINT_BATCH_FROZEN"
EV_RANKING_RELEASED = "RANKING_RELEASED"
EV_RANKING_CORRECTED = "RANKING_CORRECTION_PUBLISHED"

EVENT_TYPES = (
    EV_LEVEL_DECLARED,
    EV_EVENT_SANCTIONED,
    EV_VENUE_VERIFIED,
    EV_OFFICIAL_CERTIFIED,
    EV_IDENTITY_REGISTERED,
    EV_MATCH_PROPOSED,
    EV_MATCH_DECIDED,
    EV_RULESET_DEFINED,
    EV_RULESET_FROZEN,
    EV_DIVISION_OPENED,
    EV_ELIGIBILITY_GRANTED,
    EV_ELIGIBILITY_REVOKED,
    EV_ENTRY_SUBMITTED,
    EV_ENTRY_WITHDRAWN,
    EV_PARTNER_REPLACED,
    EV_LINEUP_FROZEN,
    EV_WITHDRAWAL_DECLARED,
    EV_ALTERNATE_PROMOTED,
    EV_RESULT_RECEIVED,
    EV_RESULT_CORRECTED,
    EV_DISCIPLINARY_ISSUED,
    EV_APPEAL_FILED,
    EV_APPEAL_DECIDED,
    EV_DQ_ISSUED,
    EV_POINT_BATCH_OPENED,
    EV_POINT_BATCH_COMPUTED,
    EV_POINT_BATCH_ADJUSTED,
    EV_POINT_BATCH_FROZEN,
    EV_RANKING_RELEASED,
    EV_RANKING_CORRECTED,
)

# 事件类型 -> 聚合类型（单一归属，保证一条事件只写一个聚合）
EVENT_AGGREGATE = {
    EV_LEVEL_DECLARED: AGG_TOURNAMENT_LEVEL,
    EV_EVENT_SANCTIONED: AGG_SANCTIONED_EVENT,
    EV_VENUE_VERIFIED: AGG_VENUE,
    EV_OFFICIAL_CERTIFIED: AGG_OFFICIAL,
    EV_IDENTITY_REGISTERED: AGG_IDENTITY,
    EV_MATCH_PROPOSED: AGG_IDENTITY_MATCH,
    EV_MATCH_DECIDED: AGG_IDENTITY_MATCH,
    EV_RULESET_DEFINED: AGG_RULESET,
    EV_RULESET_FROZEN: AGG_SANCTIONED_EVENT,
    EV_DIVISION_OPENED: AGG_DIVISION,
    EV_ELIGIBILITY_GRANTED: AGG_DIVISION,
    EV_ELIGIBILITY_REVOKED: AGG_DIVISION,
    EV_ENTRY_SUBMITTED: AGG_ENTRY,
    EV_ENTRY_WITHDRAWN: AGG_ENTRY,
    EV_PARTNER_REPLACED: AGG_ENTRY,
    EV_LINEUP_FROZEN: AGG_DIVISION,
    EV_WITHDRAWAL_DECLARED: AGG_SANCTIONED_EVENT,
    EV_ALTERNATE_PROMOTED: AGG_ENTRY,
    EV_RESULT_RECEIVED: AGG_RESULT,
    EV_RESULT_CORRECTED: AGG_RESULT,
    EV_DISCIPLINARY_ISSUED: AGG_DISCIPLINARY,
    EV_APPEAL_FILED: AGG_APPEAL,
    EV_APPEAL_DECIDED: AGG_APPEAL,
    EV_DQ_ISSUED: AGG_DISCIPLINARY,
    EV_POINT_BATCH_OPENED: AGG_POINT_BATCH,
    EV_POINT_BATCH_COMPUTED: AGG_POINT_BATCH,
    EV_POINT_BATCH_ADJUSTED: AGG_POINT_BATCH,
    EV_POINT_BATCH_FROZEN: AGG_POINT_BATCH,
    EV_RANKING_RELEASED: AGG_RANKING,
    EV_RANKING_CORRECTED: AGG_RANKING,
}

# 各事件负载必填字段（形状校验；语义规则在策略层）
PAYLOAD_REQUIRED: dict[str, tuple[str, ...]] = {
    EV_LEVEL_DECLARED: ("level", "name"),
    EV_EVENT_SANCTIONED: ("event_code", "level", "name", "starts_at", "ends_at", "organizer_id"),
    EV_VENUE_VERIFIED: ("venue_code", "name", "status"),
    EV_OFFICIAL_CERTIFIED: ("official_id", "name", "certificate_no", "cert_level", "valid_from", "valid_to"),
    EV_IDENTITY_REGISTERED: ("local_member_no", "source_id", "name", "birth_date"),
    EV_MATCH_PROPOSED: ("candidate_a", "candidate_b", "reason", "confidence"),
    EV_MATCH_DECIDED: ("match_id", "decision", "reviewer"),
    EV_RULESET_DEFINED: ("ruleset_code", "level", "version", "age_band_method", "doubles_method", "equipment_method"),
    EV_RULESET_FROZEN: ("event_code", "ruleset_code", "ruleset_version"),
    EV_DIVISION_OPENED: ("event_code", "division_code", "discipline", "age_band", "gender", "max_entries"),
    EV_ELIGIBILITY_GRANTED: ("event_code", "division_code", "participant_id", "basis"),
    EV_ELIGIBILITY_REVOKED: ("event_code", "division_code", "participant_id", "reason"),
    EV_ENTRY_SUBMITTED: ("event_code", "division_code", "entry_code", "participant_ids"),
    EV_ENTRY_WITHDRAWN: ("event_code", "division_code", "entry_code", "reason"),
    EV_PARTNER_REPLACED: ("event_code", "division_code", "entry_code", "out_participant_id", "in_participant_id"),
    EV_LINEUP_FROZEN: ("event_code", "division_code", "entry_codes"),
    EV_WITHDRAWAL_DECLARED: ("event_code", "entry_code", "after_lineup_frozen", "reason"),
    EV_ALTERNATE_PROMOTED: ("event_code", "division_code", "entry_code", "vacated_entry_code", "sequence"),
    EV_RESULT_RECEIVED: ("event_code", "division_code", "entry_code", "round", "placing"),
    EV_RESULT_CORRECTED: ("event_code", "division_code", "entry_code", "round", "corrected_placing", "reason"),
    EV_DISCIPLINARY_ISSUED: ("event_code", "entry_code", "rule", "sanction", "issued_at"),
    EV_APPEAL_FILED: ("case_id", "filed_by", "grounds"),
    EV_APPEAL_DECIDED: ("case_id", "appeal_id", "decision", "decided_at"),
    EV_DQ_ISSUED: ("event_code", "division_code", "entry_code", "match_round", "effective_stage", "reason"),
    EV_POINT_BATCH_OPENED: ("batch_code", "event_code", "ruleset_code", "ruleset_version", "frozen_lineup_ref"),
    EV_POINT_BATCH_COMPUTED: ("batch_code", "lines"),
    EV_POINT_BATCH_ADJUSTED: ("batch_code", "lines", "reason", "cause_event_ids"),
    EV_POINT_BATCH_FROZEN: ("batch_code",),
    EV_RANKING_RELEASED: ("release_code", "as_of", "entries"),
    EV_RANKING_CORRECTED: ("release_code", "correction_seq", "supersedes_release", "entries", "reason"),
}

ENVELOPE_REQUIRED = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "recorded_at",
    "version",
    "summary",
    "source_id",
    "source_seq",
    "payload",
)


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def validate_envelope(record: dict) -> list[str]:
    """校验事件信封形状，返回错误信息列表（空列表表示通过）。"""
    errors: list[str] = []
    for name in ENVELOPE_REQUIRED:
        if name not in record:
            errors.append(f"缺少字段：{name}")
    if errors:
        return errors

    if not isinstance(record["event_id"], str) or not record["event_id"]:
        errors.append("event_id 必须是非空字符串")
    if record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知事件类型：{record['event_type']}")
    if record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{record['aggregate_type']}")
    elif expected := EVENT_AGGREGATE.get(record["event_type"]):
        if record["aggregate_type"] != expected:
            errors.append(
                f"事件 {record['event_type']} 的聚合必须是 {expected}，"
                f"实际为 {record['aggregate_type']}"
            )
    if not isinstance(record["aggregate_id"], str) or not record["aggregate_id"]:
        errors.append("aggregate_id 必须是非空字符串")
    if not isinstance(record["version"], int) or isinstance(record["version"], bool) or record["version"] < 1:
        errors.append("version 必须是正整数")
    if not isinstance(record["source_seq"], int) or isinstance(record["source_seq"], bool) or record["source_seq"] < 0:
        errors.append("source_seq 必须是非负整数")
    if not isinstance(record["source_id"], str) or not record["source_id"]:
        errors.append("source_id 必须是非空字符串")
    if not isinstance(record["summary"], str) or not record["summary"]:
        errors.append("summary 必须是非空字符串")
    if not isinstance(record["payload"], dict):
        errors.append("payload 必须是对象")
    if _parse_dt(record["occurred_at"]) is None:
        errors.append("occurred_at 必须是 ISO-8601 日期时间")
    if _parse_dt(record["recorded_at"]) is None:
        errors.append("recorded_at 必须是 ISO-8601 日期时间")

    idem = record.get("idempotency_key")
    if idem is not None and (not isinstance(idem, str) or not idem):
        errors.append("idempotency_key 若非空必须是非空字符串")
    causes = record.get("causes", [])
    if not isinstance(causes, list) or not all(isinstance(c, str) for c in causes):
        errors.append("causes 必须是字符串数组")
    return errors


def validate_payload(record: dict) -> list[str]:
    """校验事件负载的必填字段（浅校验）。"""
    errors = validate_envelope(record)
    if errors:
        return errors
    payload = record["payload"]
    for field in PAYLOAD_REQUIRED.get(record["event_type"], ()):  # type: ignore[arg-type]
        if field not in payload:
            errors.append(f"负载缺少字段：{field}")
    return errors
