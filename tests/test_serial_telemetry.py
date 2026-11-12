import copy
import datetime
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
import aggregator
from app import app
from models import Device, MinuteAggregate, TelemetryBatch, db, init_db
from telemetry import store_telemetry

spec = importlib.util.spec_from_file_location("serial_bridge", ROOT / "tools/serial_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

SERIAL_BLOCK = """------------------------
Temperatura: 28.60 C
Humedad: 75.60 %
Movimiento: SI
Distancia: 154.14 cm
Potenciometro: 873
Botones: 0 | 0 | 0
"""


def reading(seq=1, second=0):
    parser = bridge.SensorParser()
    for line in SERIAL_BLOCK.splitlines():
        data = parser.feed(line)
    return {"device_id": "esp32-sala-01", "room_id": "sala-01", "boot_id": "usb-1",
            "seq": seq, "ts": f"2026-01-01T12:00:{second:02d}Z", "window_s": 2, **data}


class SerialParserTest(unittest.TestCase):
    def test_reads_the_original_firmware_output_and_keeps_adc_as_adc(self):
        body = reading()
        self.assertEqual(28.6, body["climate"]["temp_c"])
        self.assertEqual(75.6, body["climate"]["rh_pct"])
        self.assertEqual(154.14, body["sensors"]["distance_cm"])
        self.assertEqual(873, body["sensors"]["potentiometer"])
        self.assertTrue(body["sensors"]["movement"])
        self.assertFalse(body["sensors"]["button1"])
        self.assertNotIn("acoustic", body)

    def test_errors_are_missing_values_and_buttons_are_booleans(self):
        parser = bridge.SensorParser()
        for line in SERIAL_BLOCK.replace("28.60 C", "Error").replace("154.14 cm", "Sin lectura").replace(
                "0 | 0 | 0", "1 | 0 | 1").splitlines():
            data = parser.feed(line)
        self.assertIsNone(data["climate"]["temp_c"])
        self.assertIsNone(data["sensors"]["distance_cm"])
        self.assertTrue(data["sensors"]["button1"])
        self.assertTrue(data["sensors"]["button3"])

    def test_partial_and_corrupt_blocks_never_reuse_previous_readings(self):
        parser = bridge.SensorParser()
        for line in SERIAL_BLOCK.splitlines():
            parser.feed(line)
        self.assertIsNone(parser.feed("Botones: 1 | 1 | 1"))
        for line in SERIAL_BLOCK.replace("873", "bad ADC").splitlines():
            data = parser.feed(line)
        self.assertIsNone(data)


class SerialTelemetryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "telemetry.db")
        init_db(self.path)
        self.client = app.test_client()

    def tearDown(self):
        if not db.is_closed():
            db.close()
        self.temp.cleanup()

    def test_end_to_end_serial_to_cloud_payload_and_local_room(self):
        for i in range(30):
            self.assertEqual(201, self.client.post("/api/v1/telemetry", json=reading(i + 1, i * 2)).status_code)
        db.connect(reuse_if_open=True)
        aggregator.aggregate_pending()
        minute = MinuteAggregate.get()
        self.assertEqual(30, minute.batches)
        self.assertEqual(30, minute.expected)
        self.assertIsNone(minute.laeq)
        self.assertEqual(100, minute.occupied_pct)
        with patch("aggregator.requests.post", return_value=SimpleNamespace(status_code=202)) as post:
            self.assertEqual((1, 0), aggregator.upload_pending())
        payload = post.call_args.kwargs["json"]["readings"][0]
        self.assertEqual(154.14, payload["sensors"]["distance_cm"])
        self.assertEqual("2026-01-01T12:00:58Z", payload["sensors"]["sampled_at"])
        self.assertIsNone(payload["acoustic"]["laeq"])
        room = self.client.get("/api/v1/rooms").json["rooms"][0]
        self.assertEqual(873, room["latest"]["sensors"]["potentiometer"])
        self.assertEqual(30, self.client.get("/api/v1/health").json["telemetry_batches"])

    def test_retries_conflicts_and_late_readings(self):
        body = reading()
        self.assertEqual(201, self.client.post("/api/v1/telemetry", json=body).status_code)
        self.assertEqual(200, self.client.post("/api/v1/telemetry", json=body).status_code)
        changed = copy.deepcopy(body)
        changed["sensors"]["potentiometer"] = 0
        self.assertEqual(400, self.client.post("/api/v1/telemetry", json=changed).status_code)
        db.connect(reuse_if_open=True)
        aggregator.aggregate_pending()
        old = MinuteAggregate.get()
        MinuteAggregate.update(uploaded=True).execute()
        late = reading(2, 2)
        late["sensors"]["movement"] = False
        store_telemetry(late)
        self.assertTrue(MinuteAggregate.get().dirty)
        aggregator.aggregate_pending()
        new = MinuteAggregate.get()
        self.assertEqual(old.id, new.id)
        self.assertEqual(50, new.occupied_pct)
        self.assertFalse(new.uploaded)
        self.assertFalse(json.loads(new.sensors_json)["movement"])
        self.assertEqual(2, TelemetryBatch.select().count())

    def test_invalid_types_and_ranges_are_rejected_before_writing(self):
        for field, value in (("potentiometer", 4096), ("potentiometer", True),
                             ("button1", 0), ("movement", "SI"), ("distance_cm", -1),
                             ("distance_cm", float("nan"))):
            body = reading()
            body["sensors"][field] = value
            self.assertEqual(400, self.client.post("/api/v1/telemetry", json=body).status_code)
        for field, value in (("seq", True), ("window_s", 0), ("boot_id", ""), ("climate", [])):
            body = reading()
            body[field] = value
            self.assertEqual(400, self.client.post("/api/v1/telemetry", json=body).status_code)
        db.connect(reuse_if_open=True)
        self.assertEqual(0, TelemetryBatch.select().count())
        self.assertEqual(0, Device.select().count())

    def test_failed_dht_and_no_echo_preserve_other_sensors(self):
        body = reading()
        body["climate"] = {"temp_c": None, "rh_pct": None}
        body["sensors"]["distance_cm"] = None
        store_telemetry(body)
        aggregator.aggregate_pending()
        payload = aggregator.to_cloud_payload(MinuteAggregate.get())
        self.assertIsNone(payload["comfort"]["ppd"])
        self.assertIsNone(payload["climate"]["temp_c"])
        self.assertIsNone(payload["sensors"]["distance_cm"])
        self.assertEqual(873, payload["sensors"]["potentiometer"])

    def test_schema_upgrade_preserves_existing_minutes(self):
        store_telemetry(reading())
        aggregator.aggregate_pending()
        db.execute_sql('ALTER TABLE minuteaggregate DROP COLUMN sensors_json')
        db.close()
        init_db(self.path)
        self.assertEqual(1, MinuteAggregate.select().count())
        self.assertEqual(1, TelemetryBatch.select().count())
        self.assertIsNone(MinuteAggregate.get().sensors_json)


if __name__ == "__main__":
    unittest.main()
