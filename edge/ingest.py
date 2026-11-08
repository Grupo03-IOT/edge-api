"""Validacion e ingesta compartidas por HTTP y MQTT."""
import datetime
import hashlib
import json
import math

from models import Device, MinuteAggregate, RawBatch, db

MIN_PLAUSIBLE_YEAR = 2020
MAX_FUTURE_S = 86400


class InvalidBatch(Exception):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.message = message
        self.fields = fields or []


def minute_key_of(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def batch_key(device_id, boot_id, seq, source_ts):
    # El boot distingue reinicios; la fecha original identifica clientes antiguos.
    identity = [device_id, boot_id or source_ts, seq]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def resolve_timestamp(raw_ts, now):
    try:
        ts = datetime.datetime.strptime(raw_ts, "%Y-%m-%dT%H:%M:%SZ")
        if ts.year < MIN_PLAUSIBLE_YEAR or (ts - now).total_seconds() > MAX_FUTURE_S:
            return now, True
        return ts, False
    except (ValueError, TypeError):
        return now, True


def validate_batch(body):
    if not isinstance(body, dict):
        raise InvalidBatch("el cuerpo no es un objeto JSON")
    errors = []

    def text(name, optional=False):
        value = body.get(name)
        if optional and value is None:
            return
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            errors.append(name)

    def number(value, path, low, high, integer=False):
        valid = (type(value) is int if integer else type(value) in (int, float))
        if (not valid or not low <= value <= high
                or (type(value) is float and not math.isfinite(value))):
            errors.append(path)
            return False
        return True

    for field in ("device_id", "room_id", "ts"):
        text(field)
    for field in ("boot_id", "fw_version"):
        text(field, optional=True)
    number(body.get("seq"), "seq", 0, 2**63 - 1, integer=True)
    number(body.get("window_s", 10), "window_s", 10, 10, integer=True)
    ac, cl, pr = (body.get(k) for k in ("acoustic", "climate", "presence"))
    for value, name in ((ac, "acoustic"), (cl, "climate"), (pr, "presence")):
        if not isinstance(value, dict):
            errors.append(name)
    if errors:
        raise InvalidBatch("campos invalidos o ausentes", errors)

    levels = ac.get("laeq_1s")
    if not isinstance(levels, list) or len(levels) != 10:
        errors.append("acoustic.laeq_1s")
    else:
        for i, value in enumerate(levels):
            number(value, f"acoustic.laeq_1s[{i}]", 0, 140)
    lo = number(ac.get("lmin"), "acoustic.lmin", 0, 140)
    hi = number(ac.get("lmax"), "acoustic.lmax", 0, 140)
    if lo and hi and ac["lmin"] > ac["lmax"]:
        errors.append("acoustic.lmin")
    hist = ac.get("hist")
    if not isinstance(hist, list) or len(hist) != 70:
        errors.append("acoustic.hist")
    else:
        for i, value in enumerate(hist):
            number(value, f"acoustic.hist[{i}]", 0, 100000, integer=True)
        if all(type(v) is int for v in hist) and sum(hist) == 0:
            errors.append("acoustic.hist")
    number(ac.get("hist_min_db", 30), "acoustic.hist_min_db", 30, 30)
    number(ac.get("hist_bin_db", 1), "acoustic.hist_bin_db", 1, 1)
    number(cl.get("temp_c"), "climate.temp_c", -40, 80)
    number(cl.get("rh_pct"), "climate.rh_pct", 0, 100)
    if pr.get("state") not in ("occupied", "vacant", "unknown"):
        errors.append("presence.state")
    number(pr.get("occupied_s"), "presence.occupied_s", 0, 10)
    number(pr.get("transitions"), "presence.transitions", 0, 1000, integer=True)
    if errors:
        raise InvalidBatch("campos invalidos o ausentes", errors)


def store_batch(body):
    """Devuelve (seq, clock_suspect, duplicate); un reintento no suma datos."""
    validate_batch(body)
    now = datetime.datetime.utcnow()
    ts, suspect = resolve_timestamp(body["ts"], now)
    seq, boot = body["seq"], body.get("boot_id")
    key = batch_key(body["device_id"], boot, seq, body["ts"])
    digest = hashlib.sha256(json.dumps(body, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()
    with db.atomic("IMMEDIATE"):
        previous = RawBatch.get_or_none(RawBatch.batch_key == key)
        if previous:
            if previous.payload_hash and previous.payload_hash != digest:
                raise InvalidBatch("la misma secuencia ya contiene otro lote", ["seq"])
            return seq, previous.clock_suspect, True

        device, _ = Device.get_or_create(
            device_id=body["device_id"], defaults={"room_id": body["room_id"]})
        # Un lote atrasado no hace retroceder ni el boot ni la secuencia.
        newer = device.last_batch_ts is None or ts >= device.last_batch_ts
        if newer:
            same_boot = boot == device.boot_id
            if same_boot and device.last_seq >= 0 and seq > device.last_seq + 1:
                device.lost_batches += seq - device.last_seq - 1
            device.last_seq = seq if not same_boot else max(seq, device.last_seq)
            if boot is None and same_boot and seq < device.last_seq:
                device.last_seq = seq
            device.boot_id = boot
            device.last_batch_ts = ts
            device.room_id = body["room_id"]
            device.fw_version = body.get("fw_version")
        device.last_seen = now
        device.link_status = "online"
        device.save()
        ac, cl, pr = body["acoustic"], body["climate"], body["presence"]
        RawBatch.create(
            device=device, room_id=body["room_id"], seq=seq, ts=ts,
            batch_key=key, payload_hash=digest,
            minute_key=minute_key_of(ts), received_at=now, clock_suspect=suspect,
            laeq_1s_json=json.dumps(ac["laeq_1s"]), lmax=ac["lmax"], lmin=ac["lmin"],
            hist_json=json.dumps(ac["hist"]), temp_c=cl["temp_c"], rh_pct=cl["rh_pct"],
            presence_state=pr["state"], occupied_s=pr["occupied_s"],
            transitions=pr["transitions"])
        MinuteAggregate.update(dirty=True, uploaded=False).where(
            (MinuteAggregate.room_id == body["room_id"])
            & (MinuteAggregate.minute_key == minute_key_of(ts))).execute()
    return seq, suspect, False


def set_link_status(device_code, status):
    device = Device.get_or_none(Device.device_id == device_code)
    if device is None:
        return False
    device.link_status = status
    device.save()
    return True
