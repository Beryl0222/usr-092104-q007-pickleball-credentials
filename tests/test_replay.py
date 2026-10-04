"""离线/乱序上报、幂等与重放重建测试。"""

import unittest

from src.domain import AssociationService
from src.domain.contract import (
    EV_EVENT_SANCTIONED, EV_VENUE_VERIFIED, EV_RESULT_RECEIVED, EV_RESULT_CORRECTED,
)
from src.domain.errors import ContractError
from src.domain.events import EventStore

EVENT_SANCTIONED = EV_EVENT_SANCTIONED
VENUE_VERIFIED = EV_VENUE_VERIFIED
RESULT_RECEIVED = EV_RESULT_RECEIVED
RESULT_CORRECTED = EV_RESULT_CORRECTED


def envelope(*, event_id, event_type, aggregate_type, aggregate_id,
             occurred_at, source_id, source_seq, payload,
             recorded_at="2026-10-19T08:00:00+08:00", version=1,
             idempotency_key=None) -> dict:
    rec = {
        "event_id": event_id, "event_type": event_type,
        "aggregate_type": aggregate_type, "aggregate_id": aggregate_id,
        "occurred_at": occurred_at, "recorded_at": recorded_at,
        "version": version, "summary": event_id,
        "source_id": source_id, "source_seq": source_seq, "payload": payload,
    }
    if idempotency_key:
        rec["idempotency_key"] = idempotency_key
    return rec


class StoreReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EventStore()

    def test_gap_detection_after_offline_burst(self) -> None:
        rec = envelope(
            event_id="e-2", event_type=VENUE_VERIFIED, aggregate_type="venue",
            aggregate_id="V-9", occurred_at="2026-10-18T10:00:00+08:00",
            source_id="stn-x", source_seq=2,
            payload={"venue_code": "V-9", "name": "缺号场", "status": "PASS"},
        )
        result = self.store.append(rec)
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "gap_after_accept")
        self.assertEqual(self.store.source_gaps("stn-x"), [0, 1])

        # 补齐后缺口闭合
        for seq, eid, at in [(0, "e-0", "2026-10-17T09:00:00+08:00"),
                             (1, "e-1", "2026-10-17T10:00:00+08:00")]:
            self.store.append(envelope(
                event_id=eid, event_type=VENUE_VERIFIED, aggregate_type="venue",
                aggregate_id=f"V-{seq}", occurred_at=at,
                source_id="stn-x", source_seq=seq,
                payload={"venue_code": f"V-{seq}", "name": eid, "status": "PASS"},
            ))
        self.assertEqual(self.store.source_gaps("stn-x"), [])

    def test_out_of_order_replays_by_occurred_at(self) -> None:
        late_first = envelope(
            event_id="late-venue", event_type=VENUE_VERIFIED, aggregate_type="venue",
            aggregate_id="V-L", occurred_at="2026-10-18T17:00:00+08:00",
            source_id="stn-off", source_seq=2,
            payload={"venue_code": "V-L", "name": "晚到场地", "status": "PASS"},
        )
        early = envelope(
            event_id="early-sanction", event_type=EVENT_SANCTIONED,
            aggregate_type="sanctioned_event", aggregate_id="E-L",
            occurred_at="2026-10-17T09:00:00+08:00", source_id="stn-off", source_seq=0,
            payload={"event_code": "E-L", "level": "COMMUNITY", "name": "乱序站",
                     "starts_at": "2026-10-25T09:00:00+08:00",
                     "ends_at": "2026-10-25T17:00:00+08:00", "organizer_id": "o"},
        )
        svc = AssociationService()
        svc.ingest(late_first)
        svc.ingest(early)
        order = [e.event_id for e in svc.store.all_events()]
        self.assertEqual(order.index("early-sanction") < order.index("late-venue"), True)
        self.assertIn("E-L", svc.state.events)
        self.assertIn("V-L", svc.state.venues)

    def test_same_event_id_retransmission_is_idempotent(self) -> None:
        rec = envelope(
            event_id="dup-1", event_type=VENUE_VERIFIED, aggregate_type="venue",
            aggregate_id="V-D", occurred_at="2026-10-18T10:00:00+08:00",
            source_id="stn-d", source_seq=0,
            payload={"venue_code": "V-D", "name": "重传场", "status": "PASS"},
        )
        self.assertEqual(self.store.append(rec).accepted, True)
        r2 = self.store.append(dict(rec))  # 原样重传
        self.assertFalse(r2.accepted)
        self.assertEqual(r2.reason, "duplicate_retransmission")
        self.assertEqual(len(self.store), 1)

    def test_same_event_id_different_payload_rejected(self) -> None:
        rec = envelope(
            event_id="dup-2", event_type=VENUE_VERIFIED, aggregate_type="venue",
            aggregate_id="V-D2", occurred_at="2026-10-18T10:00:00+08:00",
            source_id="stn-d", source_seq=0,
            payload={"venue_code": "V-D2", "name": "原", "status": "PASS"},
        )
        self.store.append(rec)
        tampered = dict(rec)
        tampered["payload"] = {"venue_code": "V-D2", "name": "篡改", "status": "FAIL"}
        with self.assertRaises(ContractError):
            self.store.append(tampered)

    def test_idempotency_key_blocks_reissue(self) -> None:
        svc = AssociationService()
        p = {
            "event_code": "E-1", "division_code": "MD", "entry_code": "EN-1",
            "round": "F", "placing": 1,
        }
        rec1 = envelope(event_id="r-1", event_type=RESULT_RECEIVED,
                        aggregate_type="match_result", aggregate_id="m-1",
                        occurred_at="2026-10-18T16:00:00+08:00",
                        source_id="stn-1", source_seq=0, payload=p,
                        idempotency_key="result:E-1:EN-1:F")
        self.assertEqual(svc.ingest(rec1), "accepted")
        # 换 event_id 但同幂等键——典型的"补传一次想改结果"——必须被挡
        rec2 = envelope(event_id="r-2", event_type=RESULT_RECEIVED,
                        aggregate_type="match_result", aggregate_id="m-1",
                        occurred_at="2026-10-18T16:05:00+08:00",
                        source_id="stn-1", source_seq=1,
                        payload={**p, "placing": 2},
                        idempotency_key="result:E-1:EN-1:F")
        self.assertEqual(svc.ingest(rec2), "duplicate_idempotency_key")
        self.assertEqual(len(svc.store), 1)

    def test_correction_uses_separate_event_not_reissue(self) -> None:
        # 更正走 RESULT_CORRECTED：新版本追加，旧版本保留可追溯
        store = EventStore()
        recv = envelope(
            event_id="r-1", event_type=RESULT_RECEIVED, aggregate_type="match_result",
            aggregate_id="m-1", occurred_at="2026-10-18T16:00:00+08:00",
            source_id="stn-1", source_seq=0,
            payload={"event_code": "E-1", "division_code": "MD", "entry_code": "EN-1",
                     "round": "F", "placing": 2},
        )
        corr = envelope(
            event_id="r-2", event_type=RESULT_CORRECTED, aggregate_type="match_result",
            aggregate_id="m-1", occurred_at="2026-10-18T20:00:00+08:00",
            source_id="stn-1", source_seq=1,
            payload={"event_code": "E-1", "division_code": "MD", "entry_code": "EN-1",
                     "round": "F", "corrected_placing": 1, "reason": "记分颠倒"},
        )
        store.append(recv)
        store.append(corr)
        svc = AssociationService(store)
        result = svc.state.results[("E-1", "MD", "EN-1", "F")]
        self.assertEqual([v.placing for v in result.versions], [2, 1])
        self.assertEqual(result.versions[0].kind, "RECEIVED")
        self.assertEqual(result.latest.event_id, "r-2")

    def test_rebuild_after_load_matches_live(self) -> None:
        svc = AssociationService()
        svc.verify_venue("V-1", "馆", "PASS")
        dumped = svc.store.dump()
        restored = AssociationService(EventStore.load(dumped))
        self.assertEqual(restored.state.venues["V-1"].status, "PASS")
        self.assertEqual([e.event_id for e in restored.store.all_events()],
                         [e.event_id for e in svc.store.all_events()])


if __name__ == "__main__":
    unittest.main()
