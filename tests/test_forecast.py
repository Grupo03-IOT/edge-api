"""El pronostico que publica el dispositivo llega al minuto agregado que sube a Cloud (TF) y convive con
la validacion y la deduplicacion de lotes (iot-connection)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
import aggregator
import mqtt_ingest
from ingest import store_batch
from models import ForecastEvent, MinuteAggregate, db, init_db


def lote(seq, segundo, minuto="12:00"):
    hist = [0] * 70
    hist[20] = 80
    return {"device_id": "zr-sala-03", "room_id": "sala-03", "boot_id": "b1", "seq": seq,
            "ts": f"2026-01-01T{minuto}:{segundo:02d}Z", "window_s": 10, "fw_version": "zenroom-forecast-v2",
            "acoustic": {"laeq_1s": [50.0] * 10, "lmin": 50.0, "lmax": 50.0, "hist": hist},
            "climate": {"temp_c": 24.0, "rh_pct": 55.0},
            "presence": {"state": "occupied", "occupied_s": 10.0, "transitions": 0}}


class PronosticoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        init_db(str(Path(self.temp.name) / "t.db"))

    def tearDown(self):
        if not db.is_closed():
            db.close()
        self.temp.cleanup()

    def test_el_pronostico_se_agrega_al_minuto(self):
        db.connect(reuse_if_open=True)
        for i in range(6):
            store_batch(lote(i + 1, i * 10))
        msg = SimpleNamespace(topic="comfort/site-1/device/zr-sala-03/forecast", payload=json.dumps(
            {"device_id": "zr-sala-03", "probability": 0.91, "horizon_min": 15, "temperature_limit_c": 25.0,
             "model_version": "zenroom-5x16x16-v1", "generated_at": "2026-01-01T12:00:55Z"}).encode())
        mqtt_ingest._handle_forecast(msg, lambda *_: None)
        store_batch(lote(7, 0, "12:01"))              # un lote del minuto siguiente cierra el anterior
        aggregator.aggregate_pending()
        db.connect(reuse_if_open=True)
        minuto = MinuteAggregate.get(MinuteAggregate.minute_key == "2026-01-01T12:00")
        self.assertAlmostEqual(0.91, minuto.forecast_probability)
        self.assertEqual("zenroom-5x16x16-v1", minuto.forecast_model_version)
        self.assertEqual(1, ForecastEvent.select().count())

    def test_un_pronostico_invalido_no_se_guarda(self):
        db.connect(reuse_if_open=True)
        store_batch(lote(1, 0))
        msg = SimpleNamespace(topic="comfort/site-1/device/zr-sala-03/forecast", payload=json.dumps(
            {"device_id": "zr-sala-03", "probability": 1.7, "horizon_min": 15, "temperature_limit_c": 25.0,
             "model_version": "x"}).encode())
        with self.assertRaises(Exception):
            mqtt_ingest._handle_forecast(msg, lambda *_: None)
        db.connect(reuse_if_open=True)
        self.assertEqual(0, ForecastEvent.select().count())


if __name__ == "__main__":
    unittest.main()
