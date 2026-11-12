"""Lecturas del montaje DHT22/PIR/HC-SR04, sin inventar datos acusticos."""
import datetime
import hashlib
import json
import math

import config
from comfort import pmv_ppd, verdict
from ingest import InvalidBatch, batch_key, minute_key_of, resolve_timestamp
from models import Device, MinuteAggregate, TelemetryBatch, db


def validate_telemetry(body):
    if not isinstance(body, dict):
        raise InvalidBatch("el cuerpo no es un objeto JSON")
    errors = []
    for field in ("device_id", "room_id", "boot_id", "ts"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            errors.append(field)
    if type(body.get("seq")) is not int or not 0 <= body["seq"] < 2**63:
        errors.append("seq")
    window = body.get("window_s")
    if type(window) is not int or not 1 <= window <= 60:
        errors.append("window_s")
    climate, sensors = body.get("climate"), body.get("sensors")
    if not isinstance(climate, dict):
        errors.append("climate")
    if not isinstance(sensors, dict):
        errors.append("sensors")
    if errors:
        raise InvalidBatch("campos invalidos o ausentes", errors)

    def number(value, path, low, high, nullable=False, integer=False):
        if nullable and value is None:
            return
        valid = type(value) is int if integer else type(value) in (int, float)
        if not valid or not low <= value <= high or not math.isfinite(value):
            errors.append(path)

    number(climate.get("temp_c"), "climate.temp_c", -40, 80, nullable=True)
    number(climate.get("rh_pct"), "climate.rh_pct", 0, 100, nullable=True)
    number(sensors.get("distance_cm"), "sensors.distance_cm", 0, 10000, nullable=True)
    number(sensors.get("potentiometer"), "sensors.potentiometer", 0, 4095, integer=True)
    for field in ("movement", "button1", "button2", "button3"):
        if type(sensors.get(field)) is not bool:
            errors.append("sensors." + field)
    if errors:
        raise InvalidBatch("campos invalidos o ausentes", errors)


def store_telemetry(body):
    validate_telemetry(body)
    now = datetime.datetime.utcnow()
    ts, suspect = resolve_timestamp(body["ts"], now)
    key = batch_key(body["device_id"], body["boot_id"], body["seq"], body["ts"])
    digest = hashlib.sha256(json.dumps(body, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
    with db.atomic("IMMEDIATE"):
        previous = TelemetryBatch.get_or_none(TelemetryBatch.batch_key == key)
        if previous:
            if previous.payload_hash != digest:
                raise InvalidBatch("la misma secuencia ya contiene otra lectura", ["seq"])
            return body["seq"], previous.clock_suspect, True
        device, _ = Device.get_or_create(device_id=body["device_id"],
                                        defaults={"room_id": body["room_id"]})
        if device.last_batch_ts is None or ts >= device.last_batch_ts:
            same_boot = device.boot_id == body["boot_id"]
            if same_boot and device.last_seq >= 0 and body["seq"] > device.last_seq + 1:
                device.lost_batches += body["seq"] - device.last_seq - 1
            device.last_seq = max(device.last_seq, body["seq"]) if same_boot else body["seq"]
            device.boot_id = body["boot_id"]
            device.last_batch_ts = ts
            device.room_id = body["room_id"]
        device.last_seen = now
        device.link_status = "online"
        device.save()
        TelemetryBatch.create(device=device, room_id=body["room_id"], ts=ts,
                              minute_key=minute_key_of(ts), batch_key=key,
                              payload_hash=digest, payload_json=json.dumps(body),
                              received_at=now, clock_suspect=suspect)
        MinuteAggregate.update(dirty=True, uploaded=False).where(
            (MinuteAggregate.room_id == body["room_id"])
            & (MinuteAggregate.minute_key == minute_key_of(ts))).execute()
    return body["seq"], suspect, False


def latest_sensors(room_id, minute_key):
    row = (TelemetryBatch.select().where(
        (TelemetryBatch.room_id == room_id) & (TelemetryBatch.minute_key == minute_key))
        .order_by(TelemetryBatch.ts.desc(), TelemetryBatch.id.desc()).first())
    if row is None:
        return None
    sensors = json.loads(row.payload_json)["sensors"].copy()
    sensors["sampled_at"] = row.ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps(sensors)


def aggregate_values(room_id, minute_key):
    rows = list(TelemetryBatch.select().where(
        (TelemetryBatch.room_id == room_id) & (TelemetryBatch.minute_key == minute_key)))
    if not rows:
        return None
    bodies = [json.loads(row.payload_json) for row in rows]

    def average(field):
        pairs = [(b["climate"].get(field), b["window_s"]) for b in bodies
                 if b["climate"].get(field) is not None]
        return sum(v * w for v, w in pairs) / sum(w for _, w in pairs) if pairs else None

    temp, rh = average("temp_c"), average("rh_pct")
    pmv, ppd = (pmv_ppd(temp, rh, vel=config.AIR_VEL, met=config.MET, clo=config.CLO)
                if temp is not None and rh is not None else (None, None))
    seconds = sum(b["window_s"] for b in bodies)
    occupied = sum(b["window_s"] for b in bodies if b["sensors"]["movement"])
    return dict(room_id=room_id, minute_key=minute_key,
                ts=datetime.datetime.strptime(minute_key, "%Y-%m-%dT%H:%M"),
                laeq=None, l10=None, l50=None, l90=None, lmax=None, lmin=None,
                temp_c=round(temp, 2) if temp is not None else None,
                rh_pct=round(rh, 1) if rh is not None else None,
                pmv=pmv, ppd=ppd, thermal_verdict=verdict(pmv) if pmv is not None else None,
                occupied_pct=round(100 * occupied / seconds, 1), transitions=0,
                batches=len(bodies), expected=math.ceil(60 * len(bodies) / seconds),
                sensors_json=latest_sensors(room_id, minute_key))
