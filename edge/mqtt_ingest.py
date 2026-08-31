"""
Suscriptor MQTT del Edge.

El ESP32 publica sus lotes en el broker que corre en la misma Raspberry Pi, y
esto los recibe y los guarda por el mismo camino que el endpoint HTTP.

Dos topics:

    comfort/<site>/room/<room_code>/readings     el lote de 10 s
    comfort/<site>/device/<device_code>/status   'online' / 'offline'

El segundo es el testamento (Last Will and Testament): el dispositivo le deja
al broker un mensaje al conectarse, y el broker lo publica EN SU NOMBRE cuando
la conexion se cae. Asi una caida llega como una noticia en segundos, en vez de
deducirse de varios minutos de silencio.
"""
import json
import threading

import paho.mqtt.client as mqtt
from peewee import DatabaseError

import config
from ingest import InvalidBatch, set_link_status, store_batch
from models import db


def _topic(*parts):
    return "/".join((config.MQTT_PREFIX, config.SITE_CODE) + parts)


READINGS_TOPIC = _topic("room", "+", "readings")
STATUS_TOPIC = _topic("device", "+", "status")


def _on_connect(client, userdata, flags, reason_code, properties=None):
    log = userdata["log"]
    if reason_code != 0:
        log("[mqtt] conexion rechazada: {}".format(reason_code))
        return
    # QoS 1: al menos una vez. Es lo que ya hacia el POST con reintentos, pero
    # resuelto por el protocolo. QoS 2 evitaria duplicados a cambio de que el
    # ESP32 guarde estado por mensaje, y la deduplicacion ya esta aguas abajo.
    client.subscribe([(READINGS_TOPIC, 1), (STATUS_TOPIC, 1)])
    log("[mqtt] suscrito a {} y {}".format(READINGS_TOPIC, STATUS_TOPIC))


def _on_message(client, userdata, message):
    log = userdata["log"]
    try:
        if message.topic.endswith("/status"):
            _handle_status(message, log)
        else:
            _handle_reading(message, log)
    except InvalidBatch as exc:
        # Mensaje mal formado: reintentarlo no lo arregla. Se descarta con
        # registro y no se propaga, o el cliente reintentaria en bucle.
        log("[mqtt] lote invalido en {}: {} {}".format(
            message.topic, exc.message, exc.fields))
    except (DatabaseError, ValueError, TypeError) as exc:
        log("[mqtt] error procesando {}: {}".format(message.topic, exc))


def _handle_reading(message, log):
    body = json.loads(message.payload.decode("utf-8"))
    with db.connection_context():
        store_batch(body)


def _handle_status(message, log):
    device_code = message.topic.split("/")[-2]
    status = message.payload.decode("utf-8").strip().lower()
    if status not in ("online", "offline"):
        log("[mqtt] estado desconocido '{}' de {}".format(status, device_code))
        return
    with db.connection_context():
        known = set_link_status(device_code, status)
    if known and status == "offline":
        log("[mqtt] {} se ha caido (testamento del broker)".format(device_code))


def build_client(log=print):
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=config.MQTT_CLIENT_ID,
        # Sesion persistente: si el Edge se reinicia, el broker le guarda los
        # mensajes QoS 1 que llegaron mientras no estaba.
        clean_session=False,
        userdata={"log": log},
    )
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def start(log=print):
    """
    Arranca el suscriptor en un hilo propio. Si el broker no esta disponible,
    reintenta en segundo plano sin tumbar el resto del Edge -- el endpoint HTTP
    tiene que seguir funcionando.
    """
    client = build_client(log)

    def run():
        while True:
            try:
                client.connect(config.MQTT_HOST, config.MQTT_PORT,
                               keepalive=config.MQTT_KEEPALIVE_S)
                client.loop_forever(retry_first_connection=True)
            except OSError as exc:
                log("[mqtt] sin broker en {}:{} ({}); reintentando".format(
                    config.MQTT_HOST, config.MQTT_PORT, exc))
                import time
                time.sleep(5)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return client, thread
