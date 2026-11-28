"""
Edge API  --  Flask + Peewee + SQLite

Lo que corre en la Raspberry Pi (o en una laptop) DENTRO del coworking.
Recibe de N modulos ESP32, agrega, alerta en local y sube al cloud.

    python app.py

Variables de entorno utiles:
    CLOUD_URL=http://localhost:8080/api/v1/readings   (el Spring del equipo)
    EDGE_DB=edge.db
    EDGE_PORT=5000
"""
import datetime
import json
import logging

from flask import Flask, jsonify, request

import config
import events
import heartbeat
import device_commands
import mqtt_ingest
import thresholds
from aggregator import pending_alerts_query, start as start_worker, to_cloud_payload
from ingest import InvalidBatch, store_batch
from models import Alert, Device, MinuteAggregate, RawBatch, TelemetryBatch, db, init_db
from telemetry import store_telemetry

app = Flask(__name__)



@app.before_request
def _open_db():
    if db.is_closed():
        db.connect(reuse_if_open=True)


@app.teardown_request
def _close_db(_exc):
    if not db.is_closed():
        db.close()


# ------------------------------------------------------------- ingesta ----

@app.post("/api/v1/readings")
def ingest():
    """
    Recibe un lote de 10 s de un modulo ESP32, por HTTP.

    Se mantiene junto al camino MQTT a proposito: es la via que el enunciado
    espera para la integracion Embedded App -> Edge API, y sirve de respaldo si
    el broker no esta disponible. Ambos caminos guardan por store_batch(), asi
    que se comportan igual.
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify(error="JSON invalido o ausente"), 400
    try:
        seq, suspect, duplicate = store_batch(body)
    except InvalidBatch as exc:
        events.record_rejected(body, "{} {}".format(exc.message, exc.fields))
        return jsonify(error=exc.message, fields=exc.fields), 400
    return jsonify(status="duplicate" if duplicate else "ok", seq=seq,
                   clock_suspect=suspect), 200 if duplicate else 201


@app.post("/api/v1/telemetry")
def ingest_telemetry():
    """DHT22, PIR, distancia, ADC y botones desde el puente USB."""
    body = request.get_json(silent=True)
    try:
        seq, suspect, duplicate = store_telemetry(body)
    except InvalidBatch as exc:
        events.record_rejected(body, "{} {}".format(exc.message, exc.fields))
        return jsonify(error=exc.message, fields=exc.fields), 400
    return jsonify(status="duplicate" if duplicate else "ok", seq=seq,
                   clock_suspect=suspect), 200 if duplicate else 201


# ------------------------------------------------------------ consulta ----

@app.get("/api/v1/rooms")
def rooms():
    out = []
    for d in Device.select():
        last = (MinuteAggregate
                .select()
                .where(MinuteAggregate.room_id == d.room_id)
                .order_by(MinuteAggregate.ts.desc())
                .first())
        out.append({
            "room_id": d.room_id,
            "device_id": d.device_id,
            "last_seen": d.last_seen.isoformat() + "Z" if d.last_seen else None,
            "lost_batches": d.lost_batches,
            "latest": to_cloud_payload(last) if last else None,
        })
    return jsonify(rooms=out)


@app.get("/api/v1/rooms/<room_id>/minutes")
def minutes(room_id):
    limit = min(int(request.args.get("limit", 60)), 1440)
    rows = (MinuteAggregate
            .select()
            .where(MinuteAggregate.room_id == room_id)
            .order_by(MinuteAggregate.ts.desc())
            .limit(limit))
    return jsonify(room_id=room_id,
                   minutes=[to_cloud_payload(r) for r in reversed(list(rows))])


@app.get("/api/v1/alerts")
def alerts():
    """
    Alertas del local. Por defecto las abiertas; ?status=closed las cerradas y
    ?status=all todas. Cada una dice si el Cloud ya la tiene.
    """
    status = request.args.get("status", "open").lower()
    query = Alert.select()
    if status == "open":
        query = query.where(Alert.closed_at.is_null(True))
    elif status == "closed":
        query = query.where(Alert.closed_at.is_null(False))
    elif status != "all":
        return jsonify(error="status debe ser open, closed o all"), 400
    rows = query.order_by(Alert.opened_at.desc()).limit(500)
    return jsonify(alerts=[{
        "id": a.id, "source_key": a.source_key,
        "room_id": a.room_id, "rule": a.rule, "severity": a.severity,
        "message": a.message, "value": a.value,
        "opened_at": a.opened_at.isoformat() + "Z",
        "closed_at": a.closed_at.isoformat() + "Z" if a.closed_at else None,
        "uploaded": a.uploaded,
        "closure_uploaded": a.closure_uploaded,
        "upload_error": a.upload_error,
    } for a in rows])


@app.get("/api/v1/health")
def health():
    pending = (MinuteAggregate
               .select()
               .where(MinuteAggregate.uploaded == False)  # noqa: E712
               .count())
    return jsonify(
        status="ok",
        cloud_url=config.CLOUD_URL,
        devices=Device.select().count(),
        raw_batches=RawBatch.select().count(),
        telemetry_batches=TelemetryBatch.select().count(),
        cloud_credential_configured=bool(config.CLOUD_API_KEY),
        minutes=MinuteAggregate.select().count(),
        pending_upload=pending,
        open_alerts=Alert.select().where(Alert.closed_at.is_null(True)).count(),
        pending_alert_upload=pending_alerts_query().count(),
        failed_alert_upload=Alert.select().where(
            Alert.upload_error.is_null(False)).count(),
        recent_events=events.recent(),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    init_db(config.DB_PATH)
    print("Edge API  ->  http://{}:{}".format(config.HOST, config.PORT))
    print("Cloud     ->  {}".format(config.CLOUD_URL))
    print("Alertas   ->  {}".format(config.ALERTS_URL))
    start_worker()
    mqtt_client = None
    if config.MQTT_ENABLED:
        print("MQTT     ->  {}:{}  prefijo {}/{}".format(
            config.MQTT_HOST, config.MQTT_PORT, config.MQTT_PREFIX, config.SITE_CODE))
        mqtt_client, _ = mqtt_ingest.start()
    else:
        print("MQTT     ->  desactivado")
    # Cada vez que se refrescan los umbrales, cada dispositivo recibe su limite
    # de temperatura por el canal de ordenes (ver device_commands.py).
    thresholds.start(on_refresh=(
        (lambda: device_commands.sync(mqtt_client, print)) if mqtt_client else None))
    print("Umbrales ->  {}  cada {}s".format(
        config.THRESHOLDS_URL, config.THRESHOLDS_REFRESH_S))
    heartbeat.start()
    print("Latido   ->  {}  cada {}s como {} en {}".format(
        config.HEARTBEAT_URL, config.HEARTBEAT_S, config.EDGE_CODE, config.SITE_CODE))
    # El servidor de Flask avisa el mismo de que no es para produccion: es de un
    # solo proceso y no aguanta carga ni errores. waitress es WSGI de verdad y
    # no necesita configuracion. Si no esta instalado se sigue, pero avisando:
    # que arranque no significa que aguante.
    try:
        from waitress import serve
        print("Servidor ->  waitress, {} hilos".format(config.SERVER_THREADS))
        serve(app, host=config.HOST, port=config.PORT,
              threads=config.SERVER_THREADS)
    except ImportError:
        print("Servidor ->  Flask de desarrollo (waitress no instalado)")
        app.run(host=config.HOST, port=config.PORT, threaded=True,
                use_reloader=False)
