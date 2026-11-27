"""
Ordenes del Edge hacia los dispositivos.

Los umbrales los configura el administrador en el cloud y el Edge los descarga
(ver thresholds.py). Esos umbrales gobiernan las alertas del local, pero el
dispositivo tambien necesita uno: el limite de temperatura contra el que su red
neuronal pronostica si la sala se va a calentar. Sin esta orden ese limite se
quedaba en el valor de fabrica del firmware y cambiar el umbral en la web no
tenia ningun efecto en el dispositivo.

El canal es el que el firmware ya escucha:

    comfort/<site>/device/<device_code>/cmd      {"temp_limit": 26.0}

Se publica retenido y con QoS 1: si el dispositivo se reinicia o se conecta mas
tarde, el broker le entrega la ultima orden en cuanto se suscribe.
"""
import json
import threading

import config
from models import Device, db
import thresholds

# Lo ultimo enviado a cada dispositivo. Evita repetir la misma orden cada vez
# que se refrescan los umbrales; se vacia si el Edge se reinicia, y entonces se
# reenvia una vez.
_sent = {}
_lock = threading.Lock()


def cmd_topic(device_code):
    return "/".join((config.MQTT_PREFIX, config.SITE_CODE, "device", device_code, "cmd"))


def temp_limit_for(room_id):
    """
    El limite de temperatura que le toca a esa sala.

    Es el nivel de advertencia de temperatura de su tipo de sala. Si la sala no
    esta clasificada o su tipo no define temperatura, se usa el valor por
    defecto del firmware, para que el dispositivo siempre tenga uno. Se acota al
    rango que el firmware acepta: fuera de el, lo ignoraria.
    """
    t = thresholds.for_room(room_id, "temp_c")
    value = t.warn_value if t is not None else config.TEMP_LIMIT_DEFAULT_C
    return min(config.TEMP_LIMIT_MAX_C, max(config.TEMP_LIMIT_MIN_C, float(value)))


def sync(client, log=print):
    """
    Envia a cada dispositivo su limite si cambio desde la ultima vez.

    Devuelve cuantas ordenes publico. Una orden que no se pudo publicar no se
    da por enviada y se reintenta en el siguiente refresco.
    """
    with db.connection_context():
        devices = [(d.device_id, d.room_id) for d in Device.select()]
    enviadas = 0
    for device_code, room_id in devices:
        limit = temp_limit_for(room_id)
        with _lock:
            if _sent.get(device_code) == limit:
                continue
        payload = json.dumps({"temp_limit": round(limit, 2)})
        try:
            info = client.publish(cmd_topic(device_code), payload, qos=1, retain=True)
            ok = getattr(info, "rc", 0) == 0
        except Exception as exc:  # el broker puede no estar: no tumba al Edge
            log("[cmd] no se pudo enviar a {}: {}".format(device_code, exc))
            continue
        if ok:
            with _lock:
                _sent[device_code] = limit
            enviadas += 1
            log("[cmd] {} <- limite de temperatura {:.1f} C ({})".format(
                device_code, limit, room_id))
    return enviadas


def reset():
    """Olvida lo enviado. Pensado para las pruebas."""
    with _lock:
        _sent.clear()
