# 匹克球赛历资历互认

赛事协会赛历与资历互认后端的领域内核：为**赛事层级、办赛授权、场地核验、裁判资质、
运动员身份、组别资格、报名名单、比赛结果、处罚申诉、积分批次与榜单快照**建立可追溯关系。
所有来源（社区站、省运会资格赛、商业邀请赛、地方离线上报通道）统一通过仓库定义的
**事件信封**接入，状态只能由事件重放重建。

## 核心不变量

1. **事件溯源，只追加。** 任何状态都是事件重放的结果；更正不覆盖旧记录，只追加新版本。
2. **原发生时间决定业务时序。** 信封同时携带 `occurred_at`（源头原发生时间）、
   `recorded_at`（平台接收时间，仅审计）、`source_id` + `source_seq`（来源序号）。
   离线/乱序上报先入库，重放一律按 `occurred_at, source_id, source_seq` 排序；
   来源序号缺口（如 0、2，缺 1）可通过 `store.source_gaps()` 查出。
3. **幂等键防重复。** `event_id` 原样重传静默跳过；同 `idempotency_key` 的第二条事件
   一律拒绝——"补传一次更正"不能重发原事件，必须发 `RESULT_CORRECTED` /
   `POINT_BATCH_ADJUSTED` 或开更正批次。
4. **同名运动员不自动合并。** 各来源本地会员号各自建档；同名只能产生
   `IDENTITY_MATCH_PROPOSED`（PENDING 待核对），经人工 `CONFIRMED` 才合并，
   旧档案留痕，`REJECTED` 保持分离。
5. **各层级可有各自规则，进入全国体系前必须冻结。** 开积分批次要求赛事已
   `RULESET_FROZEN_FOR_EVENT` 且每个组别 `LINEUP_FROZEN`；批次记录所用规则版本与
   冻结阵容引用。
6. **冻结后边界明确：**
   - 阵容冻结后不能新增报名或换搭档，只能整队退赛 → 候补按**连续顺位**递补；
   - 积分批次冻结后永不改写，任何变化开 `supersedes_batch` 指向旧批次的**更正批次**；
   - 赛后 DQ：冻结批次原样保留，更正批次冲销/补发；已发榜的再发布榜单**连续更正版**。
7. **榜单只追加更正版。** `RANKING_RELEASED` 首发，`RANKING_CORRECTION_PUBLISHED`
   以连续 `correction_seq` 接续，旧快照保留，裁决可沿更正链追溯。

## 目录

- `contracts/domain.schema.json`：事件信封 JSON Schema（30 种事件、14 类聚合、信封字段）。
- `src/domain/`
  - `contract.py`：事件/聚合枚举、事件↔聚合归属、各事件负载必填字段、信封校验。
  - `events.py`：`EventStore` 只追加日志——幂等去重、来源缺口检测、按业务时序重放、JSON 持久化。
  - `state.py`：`RehydratedState` 纯折叠投影（身份目录、赛历、组别/报名、结果版本链、
    DQ/申诉、积分批次、榜单更正链、选手积分台账）。
  - `policies.py`：赛前就绪核验、全国计分门槛、报名/替换/冻结/递补边界、
    DQ 重算边界、积分防重复、榜单更正规则。
  - `service.py`：`AssociationService` 命令面（规则校验 → 产生事件 → 入库 → 重放）
    与 `ingest()` 外部事件接入面。
  - `errors.py`：`ContractError`（信封不合法）、`RuleViolation`（业务边界，携带全部原因）。
- `scripts/demo.py`：端到端联调演示，落盘 `data/sample_stream.json`（43 条事件）。
- `tests/`：契约、乱序重放、身份核对、就绪/冻结、阵容递补、积分/DQ/申诉/榜单共 43 个用例。

## 事件与聚合

| 领域 | 事件 |
|---|---|
| 层级/授权 | `TOURNAMENT_LEVEL_DECLARED`、`EVENT_SANCTIONED` |
| 场地/裁判 | `VENUE_VERIFIED`、`OFFICIAL_CERTIFIED` |
| 身份 | `IDENTITY_REGISTERED`、`IDENTITY_MATCH_PROPOSED`、`IDENTITY_MATCH_DECIDED` |
| 规则/组别 | `RULESET_DEFINED`、`RULESET_FROZEN_FOR_EVENT`、`DIVISION_OPENED`、`ELIGIBILITY_GRANTED/REVOKED` |
| 报名/阵容 | `ENTRY_SUBMITTED/WITHDRAWN`、`PARTNER_REPLACED`、`LINEUP_FROZEN`、`WITHDRAWAL_DECLARED`、`ALTERNATE_PROMOTED` |
| 结果/处罚 | `RESULT_RECEIVED`、`RESULT_CORRECTED`、`MATCH_DISCIPLINARY_ISSUED`、`DQ_ISSUED`、`APPEAL_FILED/DECIDED` |
| 积分 | `POINT_BATCH_OPENED/COMPUTED/ADJUSTED/FROZEN` |
| 榜单 | `RANKING_RELEASED`、`RANKING_CORRECTION_PUBLISHED` |

信封必填：`event_id, event_type, aggregate_type, aggregate_id, occurred_at, recorded_at,
version, summary, source_id, source_seq, payload`；可选 `idempotency_key, correlation_id, causes`。

## 使用示例

```python
from src.domain import AssociationService

svc = AssociationService()
svc.sanction_event(event_code="E-1", level="COMMUNITY", name="社区第14站", ...)

# 承办方开赛前知道缺什么
svc.readiness("E-1")
# {"ready": False, "missing": ["场地 V-1 未核验", "REFEREE 缺合格裁判：需 1 名（B 级及以上、证书在有效期），仅 0 名可上岗"]}

# 同名运动员：只是 PENDING 匹配
svc.propose_identity_match(match_id="M-1", candidate_a=pid_a, candidate_b=pid_b,
                           reason="同名+同生日", confidence=0.81)
svc.decide_identity_match(match_id="M-1", decision="CONFIRMED", reviewer="officer-li")

# 全国计分时规则与阵容必须已冻结
svc.freeze_ruleset_for_event(event_code="E-1", ruleset_code="RS-COMMUNITY", ruleset_version="2026.1")
svc.freeze_lineup(event_code="E-1", division_code="MD-19+", entry_codes=["EN-1", "EN-2"])

# 选手视角：每笔积分来自哪条获认可结果、按哪个规则版本计算
svc.point_ledger("P-stn-014-M-99")
# [{"batch_code": ..., "result_event_id": ..., "ruleset": "RS-COMMUNITY@2026.1", "status": "FROZEN", ...}]
```

冻结后赛后 DQ 的重算边界由 `issue_dq` 返回：

```python
out = svc.issue_dq(case_id="CASE-9", event_code="E-1", division_code="MD-19+",
                   entry_code="EN-3", match_round="F", reason="器材抽检超标")
# {"stage": "AFTER_BATCH_FROZEN", "must_open_correction_batch": True,
#  "must_publish_ranking_correction": True, "frozen_batch": "PB-...", ...}
```

外部来源（含离线乱序数据）走接入面，不做业务拦截：

```python
svc.ingest_many(raw_records)          # 按 occurred_at 重放重建
svc.store.source_gaps("stn-offline-71")  # [1] —— 第 1 号事件仍未到
```

## 本地检查

```bash
python3 -m unittest discover -s tests   # 43 个用例
python3 -m scripts.demo                 # 重新生成 data/sample_stream.json
```
