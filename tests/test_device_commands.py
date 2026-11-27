import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
import config
import device_commands
from models import Device, RoomThreshold, db, init_db


class FakeClient:
    """Cliente MQTT de mentira: guarda lo que se le pide publicar."""

    def __init__(self, rc=0, fail=False):
        self.rc = rc
        self.fail = fail
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        if self.fail:
            raise OSError("sin broker")
        self.published.append((topic, json.loads(payload), qos, retain))
        return type("Info", (), {"rc": self.rc})()


class DeviceCommandsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        init_db(str(Path(self.temp.name) / "test.db"))
        device_commands.reset()
        with db.connection_context():
            Device.create(device_id="esp32-1", room_id="sala-01")
            Device.create(device_id="esp32-2", room_id="sala-02")

    def tearDown(self):
        if not db.is_closed():
            db.close()
        self.temp.cleanup()

    def set_threshold(self, room, warn):
        with db.connection_context():
            RoomThreshold.delete().where(
                (RoomThreshold.room_id == room) & (RoomThreshold.metric == "temp_c")).execute()
            RoomThreshold.create(room_id=room, metric="temp_c", warn_value=warn,
                                 critical_value=warn + 2, sustained_minutes=5)

    def test_sala_con_umbral_recibe_su_limite_retenido_y_con_qos_1(self):
        self.set_threshold("sala-01", 26.0)
        client = FakeClient()
        device_commands.sync(client)
        enviado = {t: (body, qos, retain) for t, body, qos, retain in client.published}
        topic = "{}/{}/device/esp32-1/cmd".format(config.MQTT_PREFIX, config.SITE_CODE)
        self.assertEqual(({"temp_limit": 26.0}, 1, True), enviado[topic])

    def test_sala_sin_umbral_recibe_el_valor_por_defecto(self):
        client = FakeClient()
        device_commands.sync(client)
        limites = {t.split("/")[-2]: body["temp_limit"] for t, body, _, _ in client.published}
        self.assertEqual(config.TEMP_LIMIT_DEFAULT_C, limites["esp32-2"])
        self.assertEqual(2, len(limites))

    def test_no_repite_la_orden_si_el_limite_no_cambio(self):
        self.set_threshold("sala-01", 26.0)
        client = FakeClient()
        self.assertEqual(2, device_commands.sync(client))
        self.assertEqual(0, device_commands.sync(client))
        self.assertEqual(2, len(client.published))

    def test_reenvia_solo_al_dispositivo_cuyo_umbral_cambio(self):
        self.set_threshold("sala-01", 26.0)
        client = FakeClient()
        device_commands.sync(client)
        client.published.clear()
        self.set_threshold("sala-01", 24.0)
        self.assertEqual(1, device_commands.sync(client))
        topic, body, _, _ = client.published[0]
        self.assertTrue(topic.endswith("/device/esp32-1/cmd"))
        self.assertEqual({"temp_limit": 24.0}, body)

    def test_acota_al_rango_que_el_firmware_acepta(self):
        self.set_threshold("sala-01", 35.0)   # el cloud puede traer 35 de ejemplo
        self.set_threshold("sala-02", 12.0)
        client = FakeClient()
        device_commands.sync(client)
        limites = {t.split("/")[-2]: body["temp_limit"] for t, body, _, _ in client.published}
        self.assertEqual(config.TEMP_LIMIT_MAX_C, limites["esp32-1"])
        self.assertEqual(config.TEMP_LIMIT_MIN_C, limites["esp32-2"])

    def test_si_el_broker_falla_no_da_la_orden_por_enviada(self):
        malo = FakeClient(fail=True)
        self.assertEqual(0, device_commands.sync(malo))
        bueno = FakeClient()
        self.assertEqual(2, device_commands.sync(bueno))

    def test_si_la_publicacion_devuelve_error_se_reintenta(self):
        self.assertEqual(0, device_commands.sync(FakeClient(rc=4)))
        self.assertEqual(2, device_commands.sync(FakeClient()))


if __name__ == "__main__":
    unittest.main()
