import threading

import requests
from peewee import fn

import config
from models import Device, MinuteAggregate, db


def payload():
    with db.connection_context():
        pending = MinuteAggregate.select().where(~MinuteAggregate.uploaded).count()
        rooms = Device.select(fn.COUNT(fn.DISTINCT(Device.room_id))).scalar() or 0
    return {
        "code": config.EDGE_CODE,
        "site_code": config.SITE_CODE,
        "version": config.EDGE_VERSION,
        "pending_upload": pending,
        "rooms": rooms,
    }


def send(log=print):
    headers = {}
    if config.CLOUD_API_KEY:
        headers["X-API-Key"] = config.CLOUD_API_KEY
    try:
        r = requests.post(config.HEARTBEAT_URL, json=payload(), headers=headers,
                          timeout=config.CLOUD_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as ex:
        log("Latido: no se pudo enviar ({})".format(ex))
        return False


def _worker(stop_event, log):
    while not stop_event.is_set():
        send(log)
        stop_event.wait(config.HEARTBEAT_S)


def start(log=print):
    stop_event = threading.Event()
    t = threading.Thread(target=_worker, args=(stop_event, log), daemon=True)
    t.start()
    return stop_event, t
