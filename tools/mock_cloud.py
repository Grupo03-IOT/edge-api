"""
Cloud falso. Hace de Spring Boot mientras el Spring de verdad no existe.

Imprime cada payload que le llega, para que veas exactamente que debe aceptar
tu controlador. En cuanto tengas el Spring corriendo, apaga esto y apunta el
Edge a el:

    CLOUD_URL=http://localhost:8080/api/v1/readings python edge/app.py

    python tools/mock_cloud.py            # escucha en :8080
    python tools/mock_cloud.py --quiet    # solo cuenta, no imprime
    python tools/mock_cloud.py --known-rooms sala-01,sala-03
        # rechaza con 404 MONITORING_ROOM_NOT_FOUND el lote que trae otra sala

Las alertas se guardan por source_key: reenviar el mismo evento actualiza la
alerta (por ejemplo, le pone closed_at), no la duplica. Es el contrato que debe
cumplir POST /api/v1/alerts/events del Cloud.
"""
import argparse
import json
import re

from flask import Flask, jsonify, request

app = Flask(__name__)
SEVERITIES = {"info", "warning", "critical"}
RULE = re.compile(r"[a-z][a-z0-9_]{0,39}")
STATE = {"count": 0, "rooms": {}, "quiet": False, "alerts": {},
         "known_rooms": None}


@app.post("/api/v1/readings")
def readings():
    body = request.get_json(silent=True) or {}
    items = body.get("readings", [])
    STATE["count"] += len(items)

    for it in items:
        STATE["rooms"][it.get("room_id")] = it

    if not STATE["quiet"] and items:
        print("\n=== POST /api/v1/readings  ({} registros) ===".format(len(items)))
        print(json.dumps(items[0], indent=2, ensure_ascii=False))
        if len(items) > 1:
            print("... y {} mas".format(len(items) - 1))
    else:
        print("recibidos {} (total {})".format(len(items), STATE["count"]))

    # el Spring debe responder 2xx; si no, el Edge reintenta
    return jsonify(accepted=len(items)), 202


def _error(status, code, message):
    """Misma forma que el ErrorResponse del Cloud (sin errors[]: el error es del lote)."""
    return jsonify(status=status, code=code, message=message, path=request.path), status


@app.post("/api/v1/alerts/events")
def alert_events():
    """
    Contrato de cloud-api #29. Valida el lote entero antes de guardar: si un
    evento falla, el lote se rechaza completo y no se guarda nada.
    """
    body = request.get_json(silent=True) or {}
    events = body.get("events") or []
    if not body.get("site_code"):
        return _error(400, "MONITORING_SITE_CODE_REQUIRED", "site_code is required")
    if not events:
        return _error(400, "ALERTING_ALERT_BATCH_EMPTY", "events cannot be empty")
    known = STATE["known_rooms"]
    for ev in events:
        if ev.get("severity") not in SEVERITIES:
            return _error(400, "ALERTING_UNKNOWN_ALERT_SEVERITY", "Unknown severity")
        if not RULE.fullmatch(str(ev.get("rule", ""))):
            return _error(400, "ALERTING_INVALID_ALERT_RULE", "Invalid rule")
        if ev.get("closed_at") and ev["closed_at"] < ev.get("opened_at", ""):
            return _error(400, "ALERTING_ALERT_CLOSED_BEFORE_OPENED", "closed_at < opened_at")
        if known is not None and ev.get("room_id") not in known:
            return _error(404, "MONITORING_ROOM_NOT_FOUND",
                          "Room {} not found".format(ev.get("room_id")))
    for ev in events:
        stored = STATE["alerts"].setdefault(ev["source_key"], dict(ev, status="OPEN"))
        if ev.get("closed_at") and stored.get("status") != "CLOSED":
            stored.update(closed_at=ev["closed_at"], status="CLOSED", closed_by="AUTO")
        print("alerta {} {} {} -> {}".format(ev["source_key"], ev.get("room_id"),
                                             ev.get("rule"), stored["status"]))
    return jsonify(accepted=len(events)), 200


@app.get("/api/v1/alerts")
def list_alerts():
    return jsonify(alerts=list(STATE["alerts"].values()))


@app.get("/api/v1/rooms")
def rooms():
    return jsonify(rooms=list(STATE["rooms"].values()), total=STATE["count"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--known-rooms", help="salas aceptadas, separadas por coma")
    a = ap.parse_args()
    STATE["quiet"] = a.quiet
    if a.known_rooms:
        STATE["known_rooms"] = set(a.known_rooms.split(","))
    print("Mock cloud escuchando en http://127.0.0.1:{}".format(a.port))
    print("  POST /api/v1/readings   <- el Edge sube aqui")
    print("  POST /api/v1/alerts/events <- alertas abiertas y cerradas")
    print("  GET  /api/v1/alerts     <- alertas recibidas")
    print("  GET  /api/v1/rooms      <- ultimo estado por sala\n")
    app.run(port=a.port, use_reloader=False)
