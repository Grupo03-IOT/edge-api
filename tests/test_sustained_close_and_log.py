"""US22 cierre solo tras N minutos normales; TS25 eventos con sala y periodo."""
import datetime
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
import aggregator
import config
import events
import mqtt_ingest
from app import app
from models import Alert, MinuteAggregate, db, init_db

# En el futuro respecto de opened_at (hora real), como en operacion normal.
T0 = datetime.datetime(2099, 1, 1, 10, 0)


class AlertRulesCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        init_db(str(Path(self.temp.name) / "test.db"))
        self.patches = [patch.object(config, "NOISE_LIMIT_MINUTES", 2),
                        patch.object(config, "ALERT_CLOSE_MINUTES", 3),
                        patch.object(config, "NOISE_LIMIT_DBA", 65.0),
                        patch.object(config, "PPD_LIMIT_PCT", 20.0),
                        patch.object(aggregator.thresholds, "for_room",
                                     return_value=None)]
        for p in self.patches:
            p.start()
        self.minute = 0

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if not db.is_closed():
            db.close()
        self.temp.cleanup()

    def feed(self, laeq, ppd=5.0, room="sala-1", base=T0):
        ts = base + datetime.timedelta(minutes=self.minute)
        self.minute += 1
        agg = MinuteAggregate.create(
            room_id=room, minute_key=ts.strftime("%Y-%m-%dT%H:%M"), ts=ts,
            laeq=laeq, ppd=ppd, thermal_verdict="neutral")
        aggregator.evaluate_rules(agg)
        return ts

    def alert(self, rule):
        return Alert.get((Alert.room_id == "sala-1") & (Alert.rule == rule))


class SustainedCloseTest(AlertRulesCase):
    """Con ALERT_CLOSE_MINUTES definida (3 en estos casos)."""

    def test_noise_stays_open_for_n_minus_1_normal_minutes(self):
        self.feed(70), self.feed(70)
        self.assertIsNone(self.alert("sustained_noise").closed_at)
        self.feed(50), self.feed(50)
        self.assertIsNone(self.alert("sustained_noise").closed_at)

    def test_noise_closes_at_n_normal_minutes_with_end_of_last_minute(self):
        self.feed(70), self.feed(70), self.feed(50), self.feed(50)
        last = self.feed(50)
        self.assertEqual(last + datetime.timedelta(minutes=1),
                         self.alert("sustained_noise").closed_at)

    def test_a_noisy_minute_resets_the_close_window(self):
        self.feed(70), self.feed(70), self.feed(50), self.feed(50)
        self.feed(70)
        self.feed(50), self.feed(50)
        self.assertIsNone(self.alert("sustained_noise").closed_at)

    def test_thermal_uses_the_same_close_window(self):
        self.feed(40, ppd=40.0)
        self.feed(40, ppd=5.0), self.feed(40, ppd=5.0)
        self.assertIsNone(self.alert("thermal_discomfort").closed_at)
        last = self.feed(40, ppd=5.0)
        self.assertEqual(last + datetime.timedelta(minutes=1),
                         self.alert("thermal_discomfort").closed_at)

    def test_closed_event_carries_closed_at_for_cloud(self):
        self.feed(40, ppd=40.0)
        for _ in range(3):
            last = self.feed(40, ppd=5.0)
        event = aggregator.alert_event(self.alert("thermal_discomfort"))
        self.assertEqual((last + datetime.timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"), event["closed_at"])

    def test_a_gap_in_the_minutes_does_not_count_as_normal_window(self):
        self.feed(70), self.feed(70), self.feed(50), self.feed(50)
        self.minute += 1
        self.feed(50)
        self.assertIsNone(self.alert("sustained_noise").closed_at)

    def test_a_minute_without_reading_is_not_normal(self):
        self.feed(70), self.feed(70)
        self.feed(None), self.feed(None), self.feed(None)
        self.assertIsNone(self.alert("sustained_noise").closed_at)

    def test_late_data_never_closes_before_the_opening(self):
        past = datetime.datetime(2020, 1, 1, 10, 0)
        self.feed(70, base=past), self.feed(70, base=past)
        for _ in range(3):
            self.feed(50, base=past)
        alert = self.alert("sustained_noise")
        self.assertEqual(alert.opened_at, alert.closed_at)


class RoomCloseWindowTest(AlertRulesCase):
    """Sin ALERT_CLOSE_MINUTES, la ventana de cierre es la del umbral de la sala."""

    RULES = ("sustained_noise", "thermal_discomfort")

    def setUp(self):
        super().setUp()
        self.override(None)

    def override(self, minutes):
        p = patch.object(config, "ALERT_CLOSE_MINUTES", minutes)
        p.start()
        self.addCleanup(p.stop)

    def room_thresholds(self, minutes):
        limits = {"laeq": 65.0, "ppd": 20.0}
        aggregator.thresholds.for_room.side_effect = (
            lambda room_id, metric: SimpleNamespace(
                warn_value=limits[metric], sustained_minutes=minutes))

    def assert_closes_at(self, rule, minutes):
        for _ in range(minutes - 1):
            self.feed(50)
        self.assertIsNone(self.alert(rule).closed_at)
        last = self.feed(50)
        self.assertEqual(last + datetime.timedelta(minutes=1),
                         self.alert(rule).closed_at)

    def test_room_with_5_minutes_closes_noise_at_5_not_at_4(self):
        self.room_thresholds(5)
        for _ in range(5):
            self.feed(70)
        self.assert_closes_at("sustained_noise", 5)

    def test_room_with_5_minutes_closes_thermal_at_5_not_at_4(self):
        self.room_thresholds(5)
        self.feed(40, ppd=40.0)
        self.assert_closes_at("thermal_discomfort", 5)

    def test_room_without_threshold_closes_with_the_default_window(self):
        self.feed(70, ppd=40.0), self.feed(70, ppd=40.0)
        self.feed(50)
        for rule in self.RULES:
            self.assertIsNone(self.alert(rule).closed_at)
        self.feed(50)
        for rule in self.RULES:
            self.assertIsNotNone(self.alert(rule).closed_at)

    def test_env_override_wins_over_the_room_window(self):
        self.room_thresholds(5)
        self.override(3)
        for _ in range(5):
            self.feed(70, ppd=40.0)
        self.feed(50), self.feed(50)
        for rule in self.RULES:
            self.assertIsNone(self.alert(rule).closed_at)
        self.feed(50)
        for rule in self.RULES:
            self.assertIsNotNone(self.alert(rule).closed_at)


class CloseMinutesSettingTest(unittest.TestCase):
    def load(self, value):
        self.addCleanup(importlib.reload, config)
        with patch.dict(os.environ, {"ALERT_CLOSE_MINUTES": value}):
            return importlib.reload(config).ALERT_CLOSE_MINUTES

    def test_override_only_when_the_variable_is_set(self):
        self.assertIsNone(self.load(""))
        self.assertEqual(7, self.load("7"))


class ProcessingEventLogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        init_db(str(Path(self.temp.name) / "test.db"))
        events.clear()
        self.client = app.test_client()

    def tearDown(self):
        if not db.is_closed():
            db.close()
        self.temp.cleanup()

    def send_telemetry_minute(self):
        """Un minuto ya cerrado, sin pasar por la ingesta."""
        row = SimpleNamespace(room_id="sala-2", minute_key="2020-01-01T10:06")
        query = patch.object(aggregator.TelemetryBatch, "select")
        select = query.start()
        self.addCleanup(query.stop)
        select.return_value.distinct.return_value = [row]

    def test_success_record_has_room_and_minute(self):
        self.send_telemetry_minute()
        with patch.object(aggregator, "aggregate_minute", return_value=True), \
                self.assertLogs("edge.processing", "INFO") as logs:
            self.assertEqual(1, aggregator.aggregate_pending())
        self.assertIn("event=minute.processed", logs.output[0])
        self.assertIn("room_id=sala-2", logs.output[0])
        self.assertIn("ts=2020-01-01T10:06:00Z", logs.output[0])

    def test_error_record_has_cause_room_and_minute(self):
        self.send_telemetry_minute()
        with patch.object(aggregator, "aggregate_minute",
                          side_effect=ValueError("boom")), \
                self.assertLogs("edge.processing", "ERROR") as logs, \
                self.assertRaises(ValueError):
            aggregator.aggregate_pending()
        self.assertIn('cause="ValueError: boom"', logs.output[0])
        entry = events.recent()[-1]
        self.assertEqual(
            ("minute.failed", "sala-2", "2020-01-01T10:06:00Z"),
            (entry["event"], entry["room_id"], entry["ts"]))

    def test_rejected_http_batch_is_recorded_with_cause(self):
        body = {"room_id": "sala-3", "ts": "2026-01-01T10:07:20Z"}
        for path in ("/api/v1/readings", "/api/v1/telemetry"):
            events.clear()
            with self.assertLogs("edge.processing", "ERROR"):
                self.assertEqual(400, self.client.post(path, json=body).status_code)
            entry = events.recent()[-1]
            self.assertEqual(("sala-3", "2026-01-01T10:07:00Z"),
                             (entry["room_id"], entry["ts"]))
            self.assertTrue(entry["cause"])

    def test_unreadable_mqtt_message_is_recorded_as_unknown(self):
        message = SimpleNamespace(topic="comfort/site-1/esp/readings",
                                  payload=b"not json")
        with self.assertLogs("edge.processing", "ERROR"):
            mqtt_ingest._on_message(None, {"log": lambda *_: None}, message)
        entry = events.recent()[-1]
        self.assertEqual(("unknown", "unknown"), (entry["room_id"], entry["ts"]))
        self.assertIn("JSONDecodeError", entry["cause"])

    def test_external_text_cannot_forge_log_lines(self):
        with self.assertLogs("edge.processing", "ERROR") as logs:
            events.record_rejected({"room_id": "a\nevent=minute.processed"}, "x")
        self.assertNotIn("\n", logs.output[0])

    def test_health_exposes_recent_events(self):
        events.record("minute.processed", "sala-1", "2020-01-01T10:00")
        entry = self.client.get("/api/v1/health").get_json()["recent_events"][-1]
        self.assertEqual(("sala-1", "2020-01-01T10:00:00Z"),
                         (entry["room_id"], entry["ts"]))


if __name__ == "__main__":
    unittest.main()
