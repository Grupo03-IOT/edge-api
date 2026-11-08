import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
import aggregator
from app import app
from ingest import store_batch
from models import Device, MinuteAggregate, RawBatch, db, init_db


def batch(seq=1, second=0, boot="boot-1", level=50):
    hist = [0] * 70
    hist[int(level - 30)] = 80
    return {"device_id": "esp32-1", "room_id": "sala-01", "boot_id": boot,
            "seq": seq, "ts": f"2026-01-01T12:00:{second:02d}Z", "window_s": 10,
            "acoustic": {"laeq_1s": [level] * 10, "lmin": level, "lmax": level,
                         "hist": hist},
            "climate": {"temp_c": 23, "rh_pct": 50},
            "presence": {"state": "occupied", "occupied_s": 10, "transitions": 0}}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "test.db")
        init_db(self.path)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        if not db.is_closed():
            db.close()
        self.temp.cleanup()

    def connect(self):
        db.connect(reuse_if_open=True)

    def test_http_duplicate_and_payload_conflict(self):
        body = batch()
        self.assertEqual(201, self.client.post("/api/v1/readings", json=body).status_code)
        reply = self.client.post("/api/v1/readings", json=body)
        self.assertEqual(200, reply.status_code)
        self.assertEqual("duplicate", reply.json["status"])
        changed = copy.deepcopy(body)
        changed["climate"]["temp_c"] = 24
        self.assertEqual(400, self.client.post("/api/v1/readings", json=changed).status_code)
        self.connect()
        self.assertEqual(1, RawBatch.select().count())

    def test_malformed_fields_return_400_and_write_nothing(self):
        variants = []
        for key, value in (("climate", {}), ("climate", None), ("seq", True),
                           ("seq", "1"), ("seq", 10**1000), ("device_id", []),
                           ("window_s", 20), ("ts", None)):
            body = batch()
            body[key] = value
            variants.append(body)
        for section, key, value in (("acoustic", "hist", [1]),
                ("acoustic", "laeq_1s", [float("nan")] * 10),
                ("acoustic", "hist", [0] * 70),
                ("climate", "rh_pct", 101), ("presence", "occupied_s", 11),
                ("presence", "transitions", -1)):
            body = batch()
            body[section][key] = value
            variants.append(body)
        for body in [[], None] + variants:
            with self.subTest(body=str(body)[:100]):
                self.assertEqual(400, self.client.post("/api/v1/readings", json=body).status_code)
        self.connect()
        self.assertEqual(0, Device.select().count())
        self.assertEqual(0, RawBatch.select().count())

    def test_reboot_and_out_of_order_batches(self):
        store_batch(batch(1, 0))
        store_batch(batch(3, 20))
        store_batch(batch(2, 10))
        self.assertEqual(3, Device.get().last_seq)
        store_batch(batch(1, 30, boot="boot-2"))
        store_batch(batch(2, 40, boot="boot-2"))
        # Una entrega del boot anterior no cambia el boot activo.
        store_batch(batch(4, 25, boot="boot-1"))
        self.assertEqual("boot-2", Device.get().boot_id)
        self.assertEqual(2, Device.get().last_seq)
        self.assertEqual(6, RawBatch.select().count())

    def test_clock_fallback_remains_idempotent(self):
        body = batch()
        body["ts"] = "1970-01-01T00:00:00Z"
        first = store_batch(body)
        self.assertTrue(first[1])
        self.assertTrue(store_batch(body)[2])
        self.assertEqual(1, RawBatch.select().count())

    def test_legacy_sender_can_reset_sequence(self):
        first, restarted = batch(10, 0), batch(1, 10)
        first.pop("boot_id")
        restarted.pop("boot_id")
        store_batch(first)
        store_batch(restarted)
        self.assertTrue(store_batch(restarted)[2])
        self.assertEqual(1, Device.get().last_seq)

    def test_late_batch_recalculates_and_reuploads(self):
        for i in range(5):
            store_batch(batch(i + 1, i * 10))
        aggregator.aggregate_pending()
        old = MinuteAggregate.get()
        self.assertEqual(5, old.batches)
        MinuteAggregate.update(uploaded=True).execute()
        store_batch(batch(6, 50, level=80))
        self.assertTrue(MinuteAggregate.get().dirty)
        self.assertFalse(MinuteAggregate.get().uploaded)
        aggregator.aggregate_pending()
        new = MinuteAggregate.get()
        self.assertEqual(old.id, new.id)
        self.assertEqual(6, new.batches)
        self.assertEqual(2, new.revision)
        self.assertGreater(new.laeq, 70)
        with patch("aggregator.requests.post", return_value=SimpleNamespace(status_code=202)) as post:
            self.assertEqual((1, 0), aggregator.upload_pending())
            sent = post.call_args.kwargs["json"]
            self.assertEqual(aggregator.config.SITE_CODE, sent["site_code"])
            self.assertEqual(6, sent["readings"][0]["quality"]["batches"])
        self.assertTrue(MinuteAggregate.get().uploaded)

    def test_upload_does_not_acknowledge_a_newer_revision(self):
        store_batch(batch())
        aggregator.aggregate_pending()

        def receive_late_batch(*args, **kwargs):
            store_batch(batch(2, 10, level=80))
            aggregator.aggregate_pending()
            return SimpleNamespace(status_code=202)

        with patch("aggregator.requests.post", side_effect=receive_late_batch):
            aggregator.upload_pending()
        self.assertFalse(MinuteAggregate.get().uploaded)

    def test_failed_cloud_upload_keeps_the_queue(self):
        store_batch(batch())
        aggregator.aggregate_pending()
        with patch("aggregator.requests.post", return_value=SimpleNamespace(status_code=503)):
            self.assertEqual((0, 1), aggregator.upload_pending())
        self.assertFalse(MinuteAggregate.get().uploaded)
        self.assertEqual(1, MinuteAggregate.get().upload_attempts)

    def test_schema_upgrade_preserves_readings(self):
        legacy = batch()
        legacy.pop("boot_id")
        store_batch(legacy)
        db.execute_sql("DROP INDEX rawbatch_batch_key")
        for table, columns in (("rawbatch", ("batch_key", "payload_hash")),
                ("device", ("boot_id", "last_batch_ts")),
                ("minuteaggregate", ("dirty", "revision"))):
            for column in columns:
                db.execute_sql(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        db.close()
        init_db(self.path)
        self.assertEqual(1, RawBatch.select().count())
        self.assertTrue(store_batch(legacy)[2])
        store_batch(batch(2, 10))
        self.assertEqual(2, RawBatch.select().count())

    def test_mqtt_uses_the_same_validation_and_deduplication(self):
        import mqtt_ingest
        message = SimpleNamespace(payload=json.dumps(batch()).encode())
        mqtt_ingest._handle_reading(message, lambda _: None)
        mqtt_ingest._handle_reading(message, lambda _: None)
        self.connect()
        self.assertEqual(1, RawBatch.select().count())


if __name__ == "__main__":
    unittest.main()
