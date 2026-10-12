"""
Agregacion por minuto + cola de subida al cloud.

Corre en un hilo aparte del API. Cada AGG_INTERVAL_S:
  1. busca minutos completos sin agregar y los agrega
  2. evalua reglas de alerta
  3. intenta subir al cloud lo que este pendiente
  4. purga lotes crudos vencidos
"""
import datetime
import json
import threading
import time

import requests
from peewee import fn

import config
import thresholds
from acoustics import leq, merge_hists, percentile_level, leq_from_hist
from comfort import pmv_ppd, verdict, ASSUMPTIONS
from models import Alert, Device, MinuteAggregate, RawBatch, db


def minute_key_of(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def aggregate_pending():
    """
    Agrega todo minuto que ya cerro y todavia no tiene registro.

    Un minuto se considera cerrado cuando se cumple UNA de dos condiciones:

      a) ya llegaron datos de un minuto POSTERIOR para esa sala (marca de agua)
      b) ese minuto quedo atras segun el reloj del Edge

    La (a) es la que manda. Depender solo del reloj de pared rompe en cuanto el
    reloj del dispositivo no coincide con el del Edge -- que es justo lo que
    pasa con el simulador acelerado, y lo que pasara en produccion si un ESP32
    arranca antes de sincronizar por NTP.
    """
    now_key = minute_key_of(datetime.datetime.utcnow())

    # marca de agua por sala: el ultimo minuto del que hemos visto datos
    watermarks = {
        row.room_id: row.max_key
        for row in (RawBatch
                    .select(RawBatch.room_id,
                            fn.MAX(RawBatch.minute_key).alias("max_key"))
                    .group_by(RawBatch.room_id))
    }

    rows = (RawBatch
            .select(RawBatch.room_id, RawBatch.minute_key)
            .distinct())

    made = 0
    for row in rows:
        closed = (row.minute_key < watermarks.get(row.room_id, "")
                  or row.minute_key < now_key)
        if not closed:
            continue
        exists = MinuteAggregate.get_or_none(
            (MinuteAggregate.room_id == row.room_id)
            & (MinuteAggregate.minute_key == row.minute_key))
        if exists:
            continue
        if aggregate_minute(row.room_id, row.minute_key):
            made += 1
    return made


def aggregate_minute(room_id, minute_key):
    batches = list(RawBatch
                   .select()
                   .where((RawBatch.room_id == room_id)
                          & (RawBatch.minute_key == minute_key))
                   .order_by(RawBatch.ts))
    if not batches:
        return None

    # --- acustica ---
    # Leq del minuto = energetico sobre TODOS los valores de 1 s. Nunca la media
    # aritmetica de los Leq de cada lote.
    all_1s = []
    for b in batches:
        all_1s.extend(b.laeq_1s)
    hist = merge_hists([b.hist for b in batches])

    laeq = leq(all_1s) if all_1s else leq_from_hist(hist)

    # --- clima: aqui SI es media aritmetica, la temperatura no es logaritmica ---
    temp = sum(b.temp_c for b in batches) / len(batches)
    rh = sum(b.rh_pct for b in batches) / len(batches)

    pmv, ppd = pmv_ppd(temp, rh, vel=config.AIR_VEL, met=config.MET, clo=config.CLO)

    # --- ocupacion ---
    occupied_s = sum(b.occupied_s for b in batches)
    expected_s = len(batches) * 10.0
    occupied_pct = 100.0 * occupied_s / expected_s if expected_s else 0.0

    agg = MinuteAggregate.create(
        room_id=room_id,
        minute_key=minute_key,
        ts=datetime.datetime.strptime(minute_key, "%Y-%m-%dT%H:%M"),
        laeq=round(laeq, 1) if laeq is not None else None,
        l10=percentile_level(hist, 10),
        l50=percentile_level(hist, 50),
        l90=percentile_level(hist, 90),
        lmax=round(max(b.lmax for b in batches), 1),
        lmin=round(min(b.lmin for b in batches), 1),
        temp_c=round(temp, 2),
        rh_pct=round(rh, 1),
        pmv=pmv,
        ppd=ppd,
        thermal_verdict=verdict(pmv),
        occupied_pct=round(occupied_pct, 1),
        transitions=sum(b.transitions for b in batches),
        batches=len(batches),
        expected=6,
    )
    evaluate_rules(agg)
    return agg


def evaluate_rules(agg):
    """Alertas locales. Se resuelven en el Edge para responder sin internet."""
    _rule_sustained_noise(agg)
    _rule_discomfort(agg)


def _rule_sustained_noise(agg):
    """
    Ruido por encima del limite durante varios minutos seguidos.

    Lo de "seguidos" no es prudencia tecnica: un portazo son 75 dB durante medio
    segundo y no es un problema de nadie. Lo que molesta es el ruido sostenido.
    """
    limite, minutos = _limit(agg.room_id, "laeq",
                             config.NOISE_LIMIT_DBA, config.NOISE_LIMIT_MINUTES)
    recent = list(MinuteAggregate
                  .select()
                  .where(MinuteAggregate.room_id == agg.room_id)
                  .order_by(MinuteAggregate.ts.desc())
                  .limit(minutos))
    over = (len(recent) >= minutos
            and all(m.laeq is not None and m.laeq > limite for m in recent))
    _toggle(agg, "sustained_noise", over,
            "Noise above {:.0f} dBA for {} min".format(limite, minutos),
            agg.laeq)


def _rule_discomfort(agg):
    limite, _ = _limit(agg.room_id, "ppd", config.PPD_LIMIT_PCT, 1)
    over = agg.ppd is not None and agg.ppd > limite
    _toggle(agg, "thermal_discomfort", over,
            "Thermal discomfort: PPD {:.0f}% ({})".format(
                agg.ppd or 0, agg.thermal_verdict),
            agg.ppd)


def _limit(room_id, metric, por_defecto, minutos_por_defecto):
    """
    El umbral configurado para esa sala, o el valor por defecto.

    Una sala recien auto-registrada todavia no esta clasificada y no tiene
    umbral propio. Sigue vigilada con la regla generica: dejar de vigilarla
    hasta que alguien la clasifique seria el peor comportamiento posible.
    """
    t = thresholds.for_room(room_id, metric)
    if t is None:
        return por_defecto, minutos_por_defecto
    return t.warn_value, max(1, t.sustained_minutes)


def _toggle(agg, rule, condition, message, value):
    """Abre la alerta si no estaba abierta; la cierra cuando deja de cumplirse."""
    open_alert = Alert.get_or_none(
        (Alert.room_id == agg.room_id) & (Alert.rule == rule)
        & (Alert.closed_at.is_null(True)))
    if condition and not open_alert:
        Alert.create(room_id=agg.room_id, rule=rule, message=message, value=value)
    elif not condition and open_alert:
        open_alert.closed_at = datetime.datetime.utcnow()
        open_alert.save()


# ---------------------------------------------------------------- cloud ----

def _device_state(room_id):
    """
    Estado del modulo que reporta por esta sala, para que el cloud pueda saber
    si un sensor se cayo.

    El cloud NO puede deducir lotes perdidos por su cuenta: solo ve un resumen
    por minuto, no la secuencia completa. Por eso el Edge -- que si la ve --
    manda la cuenta ya hecha y el cloud la refleja tal cual.
    """
    device = (Device
              .select()
              .where(Device.room_id == room_id)
              .order_by(Device.last_seen.desc())
              .first())
    if not device:
        return None
    return {
        "code": device.device_id,
        "fw_version": device.fw_version,
        "last_seq": device.last_seq,
        "lost_batches": device.lost_batches,
        "last_seen": (device.last_seen.strftime("%Y-%m-%dT%H:%M:%SZ")
                      if device.last_seen else None),
    }


def to_cloud_payload(agg):
    """Contrato Edge -> Cloud. Esto es lo que debe aceptar el Spring Boot."""
    return {
        "room_id": agg.room_id,
        "device": _device_state(agg.room_id),
        "ts": agg.ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_s": 60,
        "acoustic": {
            "laeq": agg.laeq, "l10": agg.l10, "l50": agg.l50,
            "l90": agg.l90, "lmax": agg.lmax, "lmin": agg.lmin,
        },
        "climate": {"temp_c": agg.temp_c, "rh_pct": agg.rh_pct},
        "comfort": {
            "pmv": agg.pmv, "ppd": agg.ppd, "verdict": agg.thermal_verdict,
            "assumptions": ASSUMPTIONS,
        },
        "occupancy": {
            "occupied_pct": agg.occupied_pct,
            "transitions": agg.transitions,
        },
        "quality": {"batches": agg.batches, "expected": agg.expected},
    }


def upload_pending():
    """
    Sube al cloud lo pendiente. Si falla, NO se pierde: queda uploaded=False
    y se reintenta en la siguiente vuelta. Esa cola es la resiliencia ante
    caidas de internet.
    """
    pending = list(MinuteAggregate
                   .select()
                   .where(MinuteAggregate.uploaded == False)  # noqa: E712
                   .order_by(MinuteAggregate.ts)
                   .limit(config.UPLOAD_BATCH))
    if not pending:
        return 0, 0

    payload = {"readings": [to_cloud_payload(a) for a in pending]}
    headers = {"Content-Type": "application/json"}
    if config.CLOUD_API_KEY:
        headers["X-API-Key"] = config.CLOUD_API_KEY

    try:
        r = requests.post(config.CLOUD_URL, json=payload,
                          headers=headers, timeout=config.CLOUD_TIMEOUT)
        if 200 <= r.status_code < 300:
            ids = [a.id for a in pending]
            with db.atomic("IMMEDIATE"):
                MinuteAggregate.update(uploaded=True).where(
                    MinuteAggregate.id.in_(ids)).execute()
            return len(pending), 0
        raise RuntimeError("HTTP {}".format(r.status_code))
    except Exception:
        with db.atomic("IMMEDIATE"):
            MinuteAggregate.update(
                upload_attempts=MinuteAggregate.upload_attempts + 1
            ).where(MinuteAggregate.id.in_([a.id for a in pending])).execute()
        return 0, len(pending)


def purge_old_raw():
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(
        days=config.RAW_RETENTION_DAYS)
    return RawBatch.delete().where(RawBatch.received_at < cutoff).execute()


# ----------------------------------------------------------------- loop ----

def worker(stop_event, log=print):
    last_purge = [time.monotonic()]
    while not stop_event.is_set():
        try:
            with db.connection_context():
                # IMMEDIATE y no la transaccion normal: una que empieza leyendo
                # y luego escribe NO espera su turno --falla en el acto si otro
                # escribio mientras tanto, por mucho busy_timeout que haya--.
                # Pidiendo el lock de escritura desde el principio, si esta
                # ocupado se espera en vez de reventar.
                with db.atomic("IMMEDIATE"):
                    made = aggregate_pending()

                # La subida va FUERA de la transaccion: dentro tendria el lock
                # tomado durante una llamada de red.
                sent, queued = upload_pending()
                if made or sent or queued:
                    log("[agg] +{} minutos | subidos {} | en cola {}".format(
                        made, sent, queued))

                # La purga es retencion de 14 dias, no trabajo de cada ciclo:
                # borrar por fecha bloquea la escritura, y hacerlo seis veces
                # por minuto dejaba fuera al resto de hilos.
                if time.monotonic() - last_purge[0] > config.PURGE_INTERVAL_S:
                    last_purge[0] = time.monotonic()
                    with db.atomic("IMMEDIATE"):
                        borrados = purge_old_raw()
                    if borrados:
                        log("[agg] purgados {} lotes crudos".format(borrados))
        except Exception as exc:                       # el hilo no debe morir
            log("[agg] error: {}".format(exc))
        stop_event.wait(config.AGG_INTERVAL_S)


def start(log=print):
    stop_event = threading.Event()
    t = threading.Thread(target=worker, args=(stop_event, log), daemon=True)
    t.start()
    return stop_event, t
