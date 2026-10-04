import json
import unittest
from pathlib import Path

from src.validator import validate_event
from src.domain.contract import (
    EVENT_TYPES, EVENT_AGGREGATE, AGGREGATE_TYPES,
    validate_envelope, validate_payload,
)


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        path = Path(__file__).parents[1] / "data" / "sample.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(validate_event(record), [])
        self.assertEqual(validate_payload(record), [])

    def test_stream_all_records_valid(self) -> None:
        path = Path(__file__).parents[1] / "data" / "sample_stream.json"
        if not path.exists():
            self.skipTest("sample_stream.json 尚未生成（python3 -m scripts.demo）")
        for record in json.loads(path.read_text(encoding="utf-8")):
            self.assertEqual(validate_payload(record), [], record["event_id"])

    def test_missing_envelope_fields(self) -> None:
        errors = validate_envelope({"event_id": "x"})
        self.assertTrue(any("event_type" in e for e in errors))

    def test_event_aggregate_must_match(self) -> None:
        record = {
            "event_id": "e1", "event_type": "VENUE_VERIFIED",
            "aggregate_type": "sanctioned_event",  # 必须是 venue
            "aggregate_id": "v1", "occurred_at": "2026-10-01T00:00:00+08:00",
            "recorded_at": "2026-10-01T00:01:00+08:00", "version": 1,
            "summary": "s", "source_id": "s", "source_seq": 0,
            "payload": {},
        }
        self.assertTrue(any("聚合必须是" in e for e in validate_envelope(record)))

    def test_every_event_has_aggregate_and_payload_rule(self) -> None:
        for et in EVENT_TYPES:
            self.assertIn(et, EVENT_AGGREGATE)
            self.assertIn(EVENT_AGGREGATE[et], AGGREGATE_TYPES)

    def test_bad_datetime_and_seq(self) -> None:
        record = {
            "event_id": "e1", "event_type": "VENUE_VERIFIED",
            "aggregate_type": "venue", "aggregate_id": "v1",
            "occurred_at": "not-a-time", "recorded_at": "2026-10-01T00:01:00+08:00",
            "version": 0, "summary": "s", "source_id": "s", "source_seq": -1,
            "payload": {},
        }
        errors = validate_envelope(record)
        self.assertTrue(any("occurred_at" in e for e in errors))
        self.assertTrue(any("version" in e for e in errors))
        self.assertTrue(any("source_seq" in e for e in errors))

    def test_payload_required_fields(self) -> None:
        record = {
            "event_id": "e1", "event_type": "VENUE_VERIFIED",
            "aggregate_type": "venue", "aggregate_id": "v1",
            "occurred_at": "2026-10-01T00:00:00+08:00",
            "recorded_at": "2026-10-01T00:01:00+08:00", "version": 1,
            "summary": "s", "source_id": "s", "source_seq": 0,
            "payload": {"venue_code": "V-1"},  # 缺 name/status
        }
        errors = validate_payload(record)
        self.assertTrue(any("name" in e for e in errors))
        self.assertTrue(any("status" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
