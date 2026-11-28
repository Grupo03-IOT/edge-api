"""
Eventos de procesamiento de mediciones: que paso, en que sala y en que periodo.

Cada evento sale como una linea key=value en el logger "edge.processing" y se
guarda en memoria para /api/v1/health. ts es el minuto de la medicion, no la
hora del registro (esa es logged_at).
"""
import collections
import datetime
import json
import logging
import re
import threading

logger = logging.getLogger("edge.processing")

UNKNOWN = "unknown"
MAX_TEXT = 200
_PLAIN = re.compile(r"[\w:.\-/+]+")
_recent = collections.deque(maxlen=50)
_lock = threading.Lock()


def record(event, room_id, minute_key, cause=None):
    """Registra un evento. Con cause es un error; sin ella, un exito."""
    entry = {"event": event,
             "room_id": _text(room_id) or UNKNOWN,
             "ts": minute_key + ":00Z" if minute_key else UNKNOWN}
    if cause is not None:
        entry["cause"] = _text(cause) or UNKNOWN
    entry["logged_at"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        _recent.append(entry)
    # Lo que trae espacios, comillas o saltos de linea va entrecomillado: un
    # room_id llega de fuera y no debe poder falsificar lineas del registro.
    line = " ".join(
        "{}={}".format(k, v if _PLAIN.fullmatch(v) else json.dumps(v))
        for k, v in entry.items())
    logger.log(logging.INFO if cause is None else logging.ERROR, line)
    return entry


def record_rejected(body, cause):
    """Lote o mensaje que no se pudo procesar: sala y minuto si venian en el."""
    body = body if isinstance(body, dict) else {}
    room_id = body.get("room_id")
    try:
        ts = datetime.datetime.strptime(body.get("ts"), "%Y-%m-%dT%H:%M:%SZ")
        minute_key = ts.strftime("%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        minute_key = None
    return record("batch.rejected",
                  room_id if isinstance(room_id, str) else None,
                  minute_key, cause)


def recent(limit=20):
    with _lock:
        return list(_recent)[-limit:]


def clear():
    with _lock:
        _recent.clear()


def _text(value):
    return None if value is None else str(value)[:MAX_TEXT]
