"""
Modelos del Edge  --  Peewee + SQLite (stack impuesto por el enunciado).

Dos niveles de detalle a proposito:
  RawBatch        lo que manda el ESP32 cada 10 s. Detalle fino, retencion corta.
  MinuteAggregate 1 registro por minuto por sala. Es lo unico que sube al cloud.

Esa reduccion ES la justificacion de la capa Edge.
"""
import datetime
import json

from peewee import (
    Model, SqliteDatabase, CharField, IntegerField, FloatField,
    DateTimeField, BooleanField, TextField, ForeignKeyField,
)

# busy_timeout: SQLite admite un solo escritor a la vez, y aqui hay tres hilos
# que escriben --agregador, refresco de umbrales y la ingesta--. Sin esto, el
# que encuentra el lock ocupado falla al instante con "database is locked"; con
# esto espera hasta 5 s, que es mas de lo que tarda cualquier ciclo.
db = SqliteDatabase(None, pragmas={
    "journal_mode": "wal",
    "busy_timeout": 30000,
    "foreign_keys": 1,
})


class BaseModel(Model):
    class Meta:
        database = db


class Device(BaseModel):
    """Un modulo ESP32. Se separa de la sala a proposito: si se quema un
    dispositivo y se cambia, la sala conserva su historia."""
    device_id = CharField(primary_key=True)
    room_id = CharField(index=True)
    fw_version = CharField(null=True)
    last_seen = DateTimeField(null=True)
    last_seq = IntegerField(default=-1)
    boot_id = CharField(null=True)
    last_batch_ts = DateTimeField(null=True)
    lost_batches = IntegerField(default=0)
    # Lo anuncia el testamento MQTT del dispositivo: el broker publica 'offline'
    # en su nombre cuando pierde la conexion. Es una noticia, no una deduccion.
    link_status = CharField(default="unknown")


class RawBatch(BaseModel):
    """Lote crudo de 10 s. Se purga a los RAW_RETENTION_DAYS dias."""
    device = ForeignKeyField(Device, backref="batches")
    room_id = CharField(index=True)
    seq = IntegerField()
    batch_key = CharField(null=True, unique=True)
    payload_hash = CharField(null=True)
    ts = DateTimeField(index=True)          # hora declarada por el dispositivo
    minute_key = CharField(index=True)      # 'YYYY-MM-DDTHH:MM' -> clave de agregacion
    received_at = DateTimeField(default=datetime.datetime.utcnow, index=True)
    clock_suspect = BooleanField(default=False)

    laeq_1s_json = TextField()              # lista de floats
    lmax = FloatField()
    lmin = FloatField()
    hist_json = TextField()                 # 70 bins de 1 dB desde 30 dB

    temp_c = FloatField()
    rh_pct = FloatField()

    presence_state = CharField()
    occupied_s = FloatField(default=0.0)
    transitions = IntegerField(default=0)

    @property
    def laeq_1s(self):
        return json.loads(self.laeq_1s_json)

    @property
    def hist(self):
        return json.loads(self.hist_json)


class TelemetryBatch(BaseModel):
    """Muestra del monitor serial; no requiere microfono ni datos acusticos."""
    device = ForeignKeyField(Device, backref="telemetry")
    room_id = CharField(index=True)
    ts = DateTimeField(index=True)
    minute_key = CharField(index=True)
    batch_key = CharField(unique=True)
    payload_hash = CharField()
    payload_json = TextField()
    received_at = DateTimeField(default=datetime.datetime.utcnow, index=True)
    clock_suspect = BooleanField(default=False)


class MinuteAggregate(BaseModel):
    """1 registro/minuto/sala. Esto es lo que viaja al cloud."""
    room_id = CharField(index=True)
    minute_key = CharField(index=True)
    ts = DateTimeField(index=True)

    laeq = FloatField(null=True)
    l10 = FloatField(null=True)
    l50 = FloatField(null=True)
    l90 = FloatField(null=True)
    lmax = FloatField(null=True)
    lmin = FloatField(null=True)

    temp_c = FloatField(null=True)
    rh_pct = FloatField(null=True)

    pmv = FloatField(null=True)
    ppd = FloatField(null=True)
    thermal_verdict = CharField(null=True)

    occupied_pct = FloatField(default=0.0)
    transitions = IntegerField(default=0)

    batches = IntegerField(default=0)       # recibidos
    expected = IntegerField(default=6)      # 60 s / 10 s

    forecast_probability = FloatField(null=True)
    forecast_horizon_min = IntegerField(null=True)
    forecast_temperature_limit_c = FloatField(null=True)
    forecast_model_version = CharField(null=True)
    forecast_generated_at = DateTimeField(null=True)
    sensors_json = TextField(null=True)

    # cola de subida al cloud: si no hay internet, esto se queda en False
    # y se reintenta. El local no se queda ciego.
    uploaded = BooleanField(default=False, index=True)
    upload_attempts = IntegerField(default=0)
    dirty = BooleanField(default=False)
    revision = IntegerField(default=1)

    class Meta:
        indexes = ((("room_id", "minute_key"), True),)   # unico


class Alert(BaseModel):
    """Reglas evaluadas en el Edge, con respuesta en <1 s y sin internet."""
    room_id = CharField(index=True)
    rule = CharField()
    severity = CharField(default="warning")
    message = CharField()
    value = FloatField(null=True)
    opened_at = DateTimeField(default=datetime.datetime.utcnow)
    closed_at = DateTimeField(null=True)
    uploaded = BooleanField(default=False, index=True)


class ForecastEvent(BaseModel):
    """Latest validated device forecast, retained per device and room minute."""
    device = ForeignKeyField(Device, backref="forecasts")
    room_id = CharField(index=True)
    minute_key = CharField(index=True)
    probability = FloatField()
    horizon_min = IntegerField()
    temperature_limit_c = FloatField()
    model_version = CharField()
    generated_at = DateTimeField(index=True)

    class Meta:
        indexes = ((('device', 'minute_key'), True),)


class RoomThreshold(BaseModel):
    """
    Copia local de lo que el cloud configuro para esta sala.

    Se guarda en disco, y no solo en memoria, para que un reinicio sin internet
    siga vigilando con lo ultimo que se supo en vez de caer a los valores por
    defecto.
    """
    room_id = CharField(index=True)
    metric = CharField()                    # laeq · l10 · ppd · occupied_pct · temp_c
    warn_value = FloatField()
    critical_value = FloatField(null=True)
    sustained_minutes = IntegerField(default=1)
    updated_at = DateTimeField(default=datetime.datetime.utcnow)

    class Meta:
        indexes = ((("room_id", "metric"), True),)


ALL_MODELS = [Device, RawBatch, TelemetryBatch, MinuteAggregate, ForecastEvent, Alert, RoomThreshold]


def init_db(path):
    db.init(path)
    db.connect(reuse_if_open=True)
    # Migracion aditiva para conservar las bases usadas en demostraciones.
    from playhouse.migrate import SqliteMigrator, migrate
    migrator = SqliteMigrator(db)
    additions = {Device: ("boot_id", "last_batch_ts"),
                 RawBatch: ("batch_key", "payload_hash"),
                 MinuteAggregate: ("dirty", "revision", "sensors_json")}
    tables = db.get_tables()
    for model, names in additions.items():
        table = model._meta.table_name
        if table in tables:
            columns = {c.name for c in db.get_columns(table)}
            for name in names:
                if name not in columns:
                    migrate(migrator.add_column(table, name, model._meta.fields[name]))
    db.create_tables(ALL_MODELS)
    # Peewee's create_tables is non-migrating. Add nullable columns so an
    # existing Edge database can adopt forecasts without losing telemetry.
    columns = {row[1] for row in db.execute_sql(
        "PRAGMA table_info(minuteaggregate)").fetchall()}
    additions = {
        "forecast_probability": "REAL",
        "forecast_horizon_min": "INTEGER",
        "forecast_temperature_limit_c": "REAL",
        "forecast_model_version": "TEXT",
        "forecast_generated_at": "DATETIME",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            db.execute_sql("ALTER TABLE minuteaggregate ADD COLUMN {} {}".format(name, sql_type))
    db.execute_sql("CREATE UNIQUE INDEX IF NOT EXISTS rawbatch_batch_key "
                   "ON rawbatch (batch_key)")
    # Reconocer reintentos de clientes antiguos recibidos antes de actualizar.
    # Los duplicados historicos se conservan; no se borran lecturas existentes.
    from ingest import batch_key
    with db.atomic("IMMEDIATE"):
        for row in RawBatch.select().where(RawBatch.batch_key.is_null(True)):
            if row.clock_suspect:
                continue  # la marca de tiempo original no se guardaba
            key = batch_key(row.device_id, None, row.seq,
                            row.ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if not RawBatch.select().where(RawBatch.batch_key == key).exists():
                RawBatch.update(batch_key=key).where(RawBatch.id == row.id).execute()
    # create_tables no anade indices a una tabla que ya existe, y sin este la
    # purga recorre la tabla entera cada vez.
    db.execute_sql(
        "CREATE INDEX IF NOT EXISTS rawbatch_received_at "
        "ON rawbatch (received_at)")
    return db
