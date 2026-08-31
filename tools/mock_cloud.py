"""
Cloud falso. Hace de Spring Boot mientras el Spring de verdad no existe.

Imprime cada payload que le llega, para que veas exactamente que debe aceptar
tu controlador. En cuanto tengas el Spring corriendo, apaga esto y apunta el
Edge a el:

    CLOUD_URL=http://localhost:8080/api/v1/readings python edge/app.py

    python tools/mock_cloud.py            # escucha en :8080
    python tools/mock_cloud.py --quiet    # solo cuenta, no imprime
"""
import argparse
import json

from flask import Flask, jsonify, request

app = Flask(__name__)
STATE = {"count": 0, "rooms": {}, "quiet": False}


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


@app.get("/api/v1/rooms")
def rooms():
    return jsonify(rooms=list(STATE["rooms"].values()), total=STATE["count"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    STATE["quiet"] = a.quiet
    print("Mock cloud escuchando en http://127.0.0.1:{}".format(a.port))
    print("  POST /api/v1/readings   <- el Edge sube aqui")
    print("  GET  /api/v1/rooms      <- ultimo estado por sala\n")
    app.run(port=a.port, use_reloader=False)
