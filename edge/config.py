"""Configuracion del Edge. Todo por variables de entorno."""
import os

DB_PATH = os.getenv("EDGE_DB", "edge.db")
HOST = os.getenv("EDGE_HOST", "0.0.0.0")
PORT = int(os.getenv("EDGE_PORT", "5000"))

# A donde sube el Edge los agregados por minuto.
# Aqui apunta el Spring Boot del equipo. Si no responde, se encolan.
CLOUD_URL = os.getenv("CLOUD_URL", "http://127.0.0.1:8080/api/v1/readings")
CLOUD_TOKEN = os.getenv("CLOUD_TOKEN", "")
CLOUD_TIMEOUT = float(os.getenv("CLOUD_TIMEOUT", "5"))
UPLOAD_BATCH = int(os.getenv("UPLOAD_BATCH", "60"))   # cuantos minutos por POST

AGG_INTERVAL_S = int(os.getenv("AGG_INTERVAL_S", "10"))
RAW_RETENTION_DAYS = int(os.getenv("RAW_RETENTION_DAYS", "14"))

# De donde bajan los umbrales configurados en el cloud. Se piden aparte de la
# subida: un lote rechazado no debe dejar ademas al Edge sin configuracion.
THRESHOLDS_URL = os.getenv(
    "THRESHOLDS_URL", "http://127.0.0.1:8080/api/v1/room-thresholds")
THRESHOLDS_REFRESH_S = int(os.getenv("THRESHOLDS_REFRESH_S", "60"))

# Umbrales por defecto. Se aplican a la sala que todavia no tiene los suyos:
# una sala recien descubierta nunca se queda sin vigilancia.
NOISE_LIMIT_DBA = float(os.getenv("NOISE_LIMIT_DBA", "65"))
NOISE_LIMIT_MINUTES = int(os.getenv("NOISE_LIMIT_MINUTES", "2"))
PPD_LIMIT_PCT = float(os.getenv("PPD_LIMIT_PCT", "20"))

# Asunciones del modelo de confort (ver comfort.ASSUMPTIONS)
CLO = float(os.getenv("CLO", "0.6"))
MET = float(os.getenv("MET", "1.2"))
AIR_VEL = float(os.getenv("AIR_VEL", "0.1"))

# --- MQTT: el salto ESP32 -> Edge ---------------------------------------
# El broker (Mosquitto) corre en la misma maquina que este Edge.
MQTT_ENABLED = os.getenv("MQTT_ENABLED", "true").lower() == "true"
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "edge-api")
# El broker da por muerto al cliente si no oye nada en 1.5x este tiempo,
# y entonces ejecuta su testamento.
MQTT_KEEPALIVE_S = int(os.getenv("MQTT_KEEPALIVE_S", "30"))

MQTT_PREFIX = os.getenv("MQTT_PREFIX", "comfort")
SITE_CODE = os.getenv("SITE_CODE", "site-1")
