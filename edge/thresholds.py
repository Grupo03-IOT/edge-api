"""
Los umbrales que el cloud configuro, traidos y cacheados aqui.

El cloud decide la politica; el Edge la aplica. La descarga es un GET aparte
--no viaja en la respuesta de la subida-- para que un lote rechazado no deje
ademas al Edge con la configuracion vieja: son dos cosas que no tienen por que
romperse juntas.

Llega el estado completo en cada refresco, no un incremento. Asi no hay nada
que invalidar: lo que hay es lo ultimo que dijo el cloud.
"""
import datetime
import threading

import requests

import config
from models import RoomThreshold, db


def for_room(room_id, metric):
    """
    El umbral de esa sala para esa metrica, o None si no tiene.

    Devolver None es la respuesta correcta cuando la sala no esta clasificada:
    quien llama aplica su valor por defecto en vez de dejar de vigilar.
    """
    return RoomThreshold.get_or_none(
        (RoomThreshold.room_id == room_id) & (RoomThreshold.metric == metric))


def refresh(log=print):
    """
    Trae los umbrales y reemplaza la copia local.

    Se borra y se reescribe entero, dentro de una transaccion: el cloud manda
    el estado completo, asi que fusionar solo abriria la puerta a conservar un
    umbral que alla se borro.
    """
    headers = {}
    if config.CLOUD_TOKEN:
        headers["Authorization"] = "Bearer " + config.CLOUD_TOKEN

    try:
        r = requests.get(config.THRESHOLDS_URL, headers=headers,
                         timeout=config.CLOUD_TIMEOUT)
        r.raise_for_status()
        rooms = r.json()
    except Exception as ex:
        # Sin internet se sigue con lo ultimo conocido. No es un fallo del Edge.
        log("Umbrales: no se pudieron refrescar ({})".format(ex))
        return 0

    ahora = datetime.datetime.utcnow()
    filas = [
        {"room_id": room.get("room_code"),
         "metric": t.get("metric"),
         "warn_value": t.get("warn_value"),
         "critical_value": t.get("critical_value"),
         "sustained_minutes": t.get("sustained_minutes") or 1,
         "updated_at": ahora}
        for room in rooms
        for t in room.get("thresholds", [])
        if t.get("enabled", True) and t.get("warn_value") is not None
    ]

    with db.atomic():
        RoomThreshold.delete().execute()
        if filas:
            RoomThreshold.insert_many(filas).execute()
    return len(filas)


def _worker(stop_event, log):
    while not stop_event.is_set():
        cuantos = refresh(log)
        if cuantos:
            log("Umbrales: {} vigentes".format(cuantos))
        stop_event.wait(config.THRESHOLDS_REFRESH_S)


def start(log=print):
    """Refresca en segundo plano. No tumba el Edge si el cloud no responde."""
    stop_event = threading.Event()
    t = threading.Thread(target=_worker, args=(stop_event, log), daemon=True)
    t.start()
    return stop_event, t
