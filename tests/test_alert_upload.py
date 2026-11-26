"""Subida de alertas al Cloud: POST /api/v1/alerts/events con source_key."""
import datetime
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
sys.path.insert(0, str(ROOT / "tools"))
import aggregator
import config
import mock_cloud
from app import app
from models import Alert, db, init_db


def reply(status, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body if body is not None else {})


def room(code):
    return SimpleNamespace(room_id=code)


class AlertUploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        init_db(str(Path(self.temp.name) / "test.db"))
        self.edge_code = patch.object(config, "EDGE_CODE", "edge-lima")
        self.edge_code.start()
        mock_cloud.STATE.update(alerts={}, known_rooms=None)
        self.cloud = mock_cloud.app.test_client()

    def tearDown(self):
        self.edge_code.stop()
        if not db.is_closed():
            db.close()
        self.temp.cleanup()

    def open_alert(self, code="sala-03", rule="sustained_noise"):
        aggregator._toggle(room(code), rule, True, "Noise above 60 dBA for 2 min", 66.4)
        return Alert.get((Alert.room_id == code) & (Alert.rule == rule))

    def close_alert(self, code="sala-03", rule="sustained_noise"):
        aggregator._toggle(room(code), rule, False, "", None)

    def to_mock_cloud(self, url, json, headers, timeout):
        """Transporte que entrega el POST al Cloud falso de tools/."""
        self.assertEqual(config.ALERTS_URL, url)
        r = self.cloud.post("/api/v1/alerts/events", json=json)
        return reply(r.status_code, r.get_json())

    def test_open_then_close_is_sent_twice_with_the_same_source_key(self):
        alert = self.open_alert()
        with patch.object(aggregator.requests, "post", side_effect=self.to_mock_cloud) as post:
            self.assertEqual((1, 0, 0), aggregator.upload_alerts())
            event = post.call_args.kwargs["json"]["events"][0]
            self.assertEqual("edge-lima:{}".format(alert.id), event["source_key"])
            self.assertEqual("sala-03", event["room_id"])
            self.assertEqual(66.4, event["value"])
            self.assertTrue(event["opened_at"].endswith("Z"))
            self.assertIsNone(event["closed_at"])
            self.assertEqual((0, 0, 0), aggregator.upload_alerts())

            self.close_alert()
            self.assertEqual((1, 0, 0), aggregator.upload_alerts())
            closed = post.call_args.kwargs["json"]["events"][0]
            self.assertEqual(event["source_key"], closed["source_key"])
            self.assertIsNotNone(closed["closed_at"])
            self.assertEqual((0, 0, 0), aggregator.upload_alerts())

        stored = Alert.get_by_id(alert.id)
        self.assertTrue(stored.uploaded and stored.closure_uploaded)
        self.assertEqual(1, len(mock_cloud.STATE["alerts"]))
        self.assertEqual("CLOSED", mock_cloud.STATE["alerts"][event["source_key"]]["status"])

    def test_resending_the_same_event_does_not_duplicate_in_the_cloud(self):
        alert = self.open_alert()
        event = aggregator.alert_event(alert)
        for _ in range(2):
            r = self.cloud.post("/api/v1/alerts/events", json={"events": [event]})
            self.assertEqual(200, r.status_code)
        self.assertEqual(1, len(mock_cloud.STATE["alerts"]))

    def test_an_alert_closed_before_its_first_upload_goes_in_one_event(self):
        alert = self.open_alert()
        self.close_alert()
        with patch.object(aggregator.requests, "post", side_effect=self.to_mock_cloud) as post:
            self.assertEqual((1, 0, 0), aggregator.upload_alerts())
        self.assertEqual(1, post.call_count)
        stored = Alert.get_by_id(alert.id)
        self.assertTrue(stored.uploaded and stored.closure_uploaded)

    def test_offline_keeps_the_alert_queued_and_retries_later(self):
        alert = self.open_alert()
        with patch.object(aggregator.requests, "post", side_effect=OSError("no route")):
            self.assertEqual((0, 1, 0), aggregator.upload_alerts())
        with patch.object(aggregator.requests, "post", return_value=reply(503)):
            self.assertEqual((0, 1, 0), aggregator.upload_alerts())
        self.assertFalse(Alert.get_by_id(alert.id).uploaded)
        with patch.object(aggregator.requests, "post", side_effect=self.to_mock_cloud):
            self.assertEqual((1, 0, 0), aggregator.upload_alerts())
        self.assertTrue(Alert.get_by_id(alert.id).uploaded)

    def test_unauthorized_is_retried_not_marked_failed(self):
        alert = self.open_alert()
        with patch.object(aggregator.requests, "post",
                          return_value=reply(403, {"code": "IAM_SCOPE_MISSING"})):
            self.assertEqual((0, 1, 0), aggregator.upload_alerts())
        self.assertIsNone(Alert.get_by_id(alert.id).upload_error)

    def test_unknown_room_is_marked_failed_and_does_not_block_the_others(self):
        mock_cloud.STATE["known_rooms"] = {"sala-01"}
        unknown = self.open_alert("sala-99")
        known = self.open_alert("sala-01")
        with patch.object(aggregator.requests, "post", side_effect=self.to_mock_cloud):
            self.assertEqual((1, 0, 1), aggregator.upload_alerts())
            self.assertEqual((0, 0, 0), aggregator.upload_alerts())
        self.assertEqual("MONITORING_ROOM_NOT_FOUND", Alert.get_by_id(unknown.id).upload_error)
        self.assertFalse(Alert.get_by_id(unknown.id).uploaded)
        self.assertTrue(Alert.get_by_id(known.id).uploaded)
        # El cierre de una alerta rechazada tampoco se reintenta.
        self.close_alert("sala-99")
        with patch.object(aggregator.requests, "post") as post:
            self.assertEqual((0, 0, 0), aggregator.upload_alerts())
        post.assert_not_called()

    def test_whole_batch_rejected_without_detail_is_retried_one_by_one(self):
        unknown = self.open_alert("sala-99")
        known = self.open_alert("sala-01")

        def cloud(url, json, headers, timeout):
            if any(e["room_id"] == "sala-99" for e in json["events"]):
                return reply(404, {"code": "MONITORING_ROOM_NOT_FOUND"})
            return reply(200, {"accepted": len(json["events"])})

        with patch.object(aggregator.requests, "post", side_effect=cloud) as post:
            self.assertEqual((1, 0, 1), aggregator.upload_alerts())
        self.assertEqual(3, post.call_count)
        self.assertEqual("MONITORING_ROOM_NOT_FOUND", Alert.get_by_id(unknown.id).upload_error)
        self.assertTrue(Alert.get_by_id(known.id).uploaded)

    def test_rejected_list_in_a_2xx_reply_marks_only_those(self):
        unknown = self.open_alert("sala-99")
        known = self.open_alert("sala-01")
        body = {"accepted": 1, "rejected": [
            {"source_key": unknown.source_key, "code": "MONITORING_ROOM_NOT_FOUND"}]}
        with patch.object(aggregator.requests, "post", return_value=reply(200, body)):
            self.assertEqual((1, 0, 1), aggregator.upload_alerts())
        self.assertEqual("MONITORING_ROOM_NOT_FOUND", Alert.get_by_id(unknown.id).upload_error)
        self.assertTrue(Alert.get_by_id(known.id).uploaded)

    def test_a_close_during_the_http_call_stays_queued(self):
        alert = self.open_alert()

        def cloud_while_closing(url, json, headers, timeout):
            Alert.update(closed_at=datetime.datetime.utcnow()).where(
                Alert.id == alert.id).execute()
            return reply(200, {"accepted": 1})

        with patch.object(aggregator.requests, "post", side_effect=cloud_while_closing):
            aggregator.upload_alerts()
        stored = Alert.get_by_id(alert.id)
        self.assertTrue(stored.uploaded)
        self.assertFalse(stored.closure_uploaded)
        self.assertEqual(1, aggregator.pending_alerts_query().count())

    def test_api_key_and_site_code_are_sent(self):
        self.open_alert()
        with patch.object(config, "CLOUD_API_KEY", "k-123"), \
                patch.object(aggregator.requests, "post",
                             return_value=reply(200, {"accepted": 1})) as post:
            aggregator.upload_alerts()
        self.assertEqual("k-123", post.call_args.kwargs["headers"]["X-API-Key"])
        self.assertEqual(config.SITE_CODE, post.call_args.kwargs["json"]["site_code"])

    def test_edge_endpoint_lists_closed_alerts_with_status_all(self):
        self.open_alert("sala-01")
        self.close_alert("sala-01")
        self.open_alert("sala-02")
        db.close()
        client = app.test_client()
        self.assertEqual(1, len(client.get("/api/v1/alerts").json["alerts"]))
        everything = client.get("/api/v1/alerts?status=all").json["alerts"]
        self.assertEqual(2, len(everything))
        closed = client.get("/api/v1/alerts?status=closed").json["alerts"]
        self.assertEqual("sala-01", closed[0]["room_id"])
        self.assertIsNotNone(closed[0]["closed_at"])
        self.assertFalse(closed[0]["uploaded"])
        self.assertEqual(400, client.get("/api/v1/alerts?status=x").status_code)
        health = client.get("/api/v1/health").json
        self.assertEqual(2, health["pending_alert_upload"])
        self.assertEqual(0, health["failed_alert_upload"])


class AlertMigrationTest(unittest.TestCase):
    def test_an_old_database_gains_the_columns_and_keeps_its_alerts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "old.db")
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE alert (id INTEGER PRIMARY KEY, room_id TEXT, rule TEXT,"
                        " severity TEXT, message TEXT, value REAL, opened_at DATETIME,"
                        " closed_at DATETIME, uploaded INTEGER)")
            con.execute("INSERT INTO alert VALUES (1, 'sala-01', 'sustained_noise', 'warning',"
                        " 'm', 70, '2026-11-08 14:10:00', NULL, 0)")
            con.commit()
            con.close()
            init_db(path)
            try:
                columns = {c.name for c in db.get_columns("alert")}
                self.assertTrue({"closure_uploaded", "upload_error"} <= columns)
                old = Alert.get_by_id(1)
                self.assertFalse(old.closure_uploaded)
                self.assertIsNone(old.upload_error)
                self.assertEqual(1, aggregator.pending_alerts_query().count())
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
