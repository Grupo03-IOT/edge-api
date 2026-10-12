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
    lost_batches = IntegerField(default=0)
    # Lo anuncia el testamento MQTT del dispositivo: el broker publica 'offline'
    # en su nombre cuando pierde la conexion. Es una noticia, no una deduccion.
    link_status = CharField(default="unknown")


class RawBatch(BaseModel):
    """Lote crudo de 10 s. Se purga a los RAW_RETENTION_DAYS dias."""
    device = ForeignKeyField(Device, backref="batches")
    room_id = CharField(index=True)
    seq = IntegerField()
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

    # cola de subida al cloud: si no hay internet, esto se queda en False
    # y se reintenta. El local no se queda ciego.
    uploaded = BooleanField(default=False, index=True)
    upload_attempts = IntegerField(default=0)

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


ALL_MODELS = [Device, RawBatch, MinuteAggregate, Alert, RoomThreshold]


def init_db(path):
    db.init(path)
    db.connect(reuse_if_open=True)
    db.create_tables(ALL_MODELS)
    # create_tables no anade indices a una tabla que ya existe, y sin este la
    # purga recorre la tabla entera cada vez.
    db.execute_sql(
        "CREATE INDEX IF NOT EXISTS rawbatch_received_at "
        "ON rawbatch (received_at)")
    return db
