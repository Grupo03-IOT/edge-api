"""
Guardado de lotes crudos.

Vive aparte del API HTTP a proposito: el mismo lote puede llegar por POST o por
MQTT, y ambos caminos tienen que hacer exactamente lo mismo. Duplicar esta
logica seria garantizar que las dos vias se comporten distinto con el tiempo.
"""
import datetime
import json

from models import Device, RawBatch

REQUIRED_FIELDS = ("device_id", "room_id", "seq", "ts",
                   "acoustic", "climate", "presence")

# El ESP32 no tiene bateria de RTC: si arranca y NTP falla, reporta una fecha
# de 1970 o del 2000. Ese es el fallo real que hay que atrapar -- no un desfase
# de minutos, que puede ser legitimo.
MIN_PLAUSIBLE_YEAR = 2020
MAX_FUTURE_S = 86400


class InvalidBatch(Exception):
    """El lote no cumple el contrato. Reintentarlo no lo va a arreglar."""

    def __init__(self, message, fields=None):
        super().__init__(message)
        self.message = message
        self.fields = fields or []


def minute_key_of(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def resolve_timestamp(raw_ts, now):
    """
    Devuelve (ts, sospechoso). Si el reloj del dispositivo es implausible se
    usa el nuestro y se marca, para no ensuciar la serie en silencio.
    """
    try:
        ts = datetime.datetime.strptime(raw_ts, "%Y-%m-%dT%H:%M:%SZ")
        if ts.year < MIN_PLAUSIBLE_YEAR or (ts - now).total_seconds() > MAX_FUTURE_S:
            return now, True
        return ts, False
    except (ValueError, KeyError, TypeError):
        return now, True


def store_batch(body):
    """
    Guarda un lote de 10 s. Lo llaman el endpoint HTTP y el suscriptor MQTT.

    Devuelve (seq, clock_suspect).
    Lanza InvalidBatch si el cuerpo no cumple el contrato.
    """
    if not isinstance(body, dict):
        raise InvalidBatch("el cuerpo no es un objeto JSON")

    missing = [k for k in REQUIRED_FIELDS if k not in body]
    if missing:
        raise InvalidBatch("faltan campos", missing)

    now = datetime.datetime.utcnow()
    ts, suspect = resolve_timestamp(body.get("ts"), now)

    device, _ = Device.get_or_create(
        device_id=body["device_id"],
        defaults={"room_id": body["room_id"]})

    # deteccion de lotes perdidos por el numero de secuencia
    seq = int(body["seq"])
    if device.last_seq >= 0 and seq > device.last_seq + 1:
        device.lost_batches += seq - device.last_seq - 1

    device.room_id = body["room_id"]
    device.fw_version = body.get("fw_version")
    device.last_seen = now
    device.last_seq = seq
    device.link_status = "online"
    device.save()

    ac, cl, pr = body["acoustic"], body["climate"], body["presence"]

    RawBatch.create(
        device=device,
        room_id=body["room_id"],
        seq=seq,
        ts=ts,
        minute_key=minute_key_of(ts),
        received_at=now,
        clock_suspect=suspect,
        laeq_1s_json=json.dumps(ac.get("laeq_1s", [])),
        lmax=ac.get("lmax", 0.0),
        lmin=ac.get("lmin", 0.0),
        hist_json=json.dumps(ac.get("hist", [])),
        temp_c=cl.get("temp_c"),
        rh_pct=cl.get("rh_pct"),
        presence_state=pr.get("state", "unknown"),
        occupied_s=pr.get("occupied_s", 0.0),
        transitions=pr.get("transitions", 0),
    )

    return seq, suspect


def set_link_status(device_code, status):
    """
    Aplica el estado que anuncia el testamento MQTT del dispositivo.

    El broker publica 'offline' en nombre del ESP32 cuando pierde la conexion,
    asi que aqui la caida llega como un mensaje en vez de deducirse del
    silencio. Solo se aplica a dispositivos ya conocidos: un testamento de un
    codigo que nunca ha reportado no crea una fila.
    """
    device = Device.get_or_none(Device.device_id == device_code)
    if device is None:
        return False
    device.link_status = status
    device.save()
    return True
