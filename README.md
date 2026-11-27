# Edge API — confort acústico y térmico

La capa que vive **dentro del coworking**. Recibe lo que miden los ESP32, lo agrega a un
registro por minuto y por sala, evalúa alertas sin depender de internet, y sube los agregados
al cloud encolándolos si la conexión se cae.

Parte de la solución IoT del curso 1ASI0572, junto a
[`cloud-api`](https://github.com/Grupo03-IOT/cloud-api).

```
ESP32  ──HTTP o MQTT──▶  Edge API  ──HTTP cada minuto──▶  Cloud API
                            │
                            └── SQLite: crudo 14 días · agregados · cola de subida
```

## Por qué existe esta capa

El micrófono muestrea a **16 kHz**: 1 380 millones de muestras al día por sala. Al cloud suben
1 440 agregados por minuto. Y no es solo ancho de banda — transmitir esas muestras sería
grabar conversaciones de gente que no dio su consentimiento. **El Edge es donde el audio
muere por diseño**: entra sonido, sale un número.

## How to run locally (cómo correrlo en local)

Necesitas Python 3.11. El broker y el Cloud son opcionales: sin ellos el Edge sigue
recibiendo por HTTP y deja en cola lo que no puede subir.

**1. Entorno virtual y dependencias**

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows (Git Bash): source .venv/Scripts/activate
pip install -r requirements.txt pytest
```

En PowerShell: `.\.venv\Scripts\Activate.ps1`.

**2. Variables de entorno.** Se leen del proceso; un `.env` no se carga solo. Lo mínimo:

```bash
export CLOUD_URL=http://127.0.0.1:8080/api/v1/readings
export ALERTS_URL=http://127.0.0.1:8080/api/v1/alerts/events
export CLOUD_API_KEY=<credencial del Cloud con readings:write, thresholds:read, edges:heartbeat y alerts:write>
export SITE_CODE=site-1                   # el código del local creado en el Cloud
export EDGE_CODE=edge-1                   # prefijo del source_key de las alertas
export MQTT_ENABLED=false                 # true si levantas Mosquitto (paso 3)
```

El resto está en [Configuración](#configuración). Cómo emitir la credencial y el primer
administrador: `cloud-api/README.md`.

**3. Broker MQTT (si el ESP32 publica por Wi-Fi).** Mosquitto con usuario y clave, según
[`broker/README.md`](broker/README.md):

```bash
mosquitto_passwd -c broker/passwd esp32
mosquitto_passwd    broker/passwd edge-api
mkdir -p broker/data && mosquitto -c broker/mosquitto.conf -v
export MQTT_ENABLED=true MQTT_USERNAME=edge-api MQTT_PASSWORD=<clave>
```

El ESP32 apunta al IP de esta máquina en el puerto 1883.

**4. Arrancar el Edge**

```bash
python edge/app.py                        # http://localhost:5000
curl http://localhost:5000/api/v1/health
```

**5. Sin Cloud: el Cloud falso.** En otra terminal, `python tools/mock_cloud.py` escucha en
`:8080`, imprime los minutos y guarda las alertas por `source_key`. Con
`--known-rooms sala-01,sala-03` rechaza las alertas de otras salas como lo hace el Cloud
(`404 MONITORING_ROOM_NOT_FOUND`).

**6. Puente serie (ESP32 por USB, sin Wi-Fi ni broker).** Con el sketch original abierto en
el puerto USB:

```bash
python tools/serial_bridge.py --list-ports
python tools/serial_bridge.py --port COM5 --room sala-01 --device esp32-sala-01
```

Envía cada bloque a `POST /api/v1/telemetry` y guarda en `serial-outbox.json` lo que el
Edge no confirmó. El recorrido completo, con Cloud y web locales, está en
[`docs/usb-local.md`](docs/usb-local.md).

**7. Pruebas**

```bash
python -m pytest -q
```

No necesitan broker, Cloud ni red: cada prueba usa un SQLite temporal y simula el Cloud.

## Conectar el ESP32 de la guia

Si el dispositivo ya imprime temperatura, humedad, movimiento, distancia,
potenciometro y botones, puedes conservar ese sketch y conectarlo por **USB**.
Sigue [`docs/usb-local.md`](docs/usb-local.md): incluye Cloud, Edge y frontend
locales y el puente `tools/serial_bridge.py`. Este camino usa
`POST /api/v1/telemetry` y no necesita broker MQTT ni Wi-Fi en el ESP32.

El firmware para DHT22, PIR, HC-SR04, potenciometro, buzzer y tres pulsadores esta
en [`zenroom-embedded-app`](https://github.com/Grupo03-IOT/zenroom-embedded-app).
Respeta los GPIO de la guia y entrega los niveles del potenciometro por el contrato
normal de ruido.

En PowerShell, desde esta carpeta:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MQTT_ENABLED = 'false'
$env:CLOUD_API_KEY = 'TU_API_KEY'
$env:SITE_CODE = 'site-1'
$env:EDGE_CODE = 'edge-1'
.\.venv\Scripts\python.exe edge/app.py
```

Ejecutar `edge/app.py` como script tambien en IntelliJ, con la raiz del repositorio
como directorio de trabajo. El inicio prepara SQLite y los hilos de agregacion,
latido y umbrales. Las variables se leen del proceso; un `.env` no se carga solo.
El procedimiento del primer administrador y la API key esta en `cloud-api/README.md`.

## Probarlo sin hardware

`simulator/device_sim.py` finge ser el ESP32. **No es una maqueta**: replica el algoritmo que
correrá el firmware —ventanas de 125 ms → Leq de 1 s → histograma cada 10 s → envío—, así que
cuando llegue el hardware se desenchufa y no hay que tocar nada más: hablan el mismo contrato.

```bash
python simulator/device_sim.py --speed 400 --minutes 60
```

Simula `sala-01`, `sala-02` y `sala-03`. `--speed 400` es 400 veces más rápido que el tiempo
real: **una hora simulada tarda unos nueve segundos**, y con 60 minutos ya hay datos
suficientes para que la analítica del cloud —que exige 30 lecturas— devuelva números.

La ocupación sigue el horario de oficina de `--timezone` (por defecto `America/Lima`); los
`ts` que se envían siguen en UTC.

Vive aquí y no en su propio repositorio porque hoy su única razón de ser es probar el Edge.
La implementacion C++ para el montaje actual esta en `zenroom-embedded-app`.

## Endpoints

| Verbo | Ruta | Para qué |
|---|---|---|
| `POST` | `/api/v1/readings` | lo que envía el dispositivo cada 10 s |
| `POST` | `/api/v1/telemetry` | muestras fisicas del puente USB; no exige microfono |
| `GET` | `/api/v1/rooms` | salas con su último minuto |
| `GET` | `/api/v1/rooms/<id>/minutes` | la serie por minuto |
| `GET` | `/api/v1/alerts` | alertas abiertas; `?status=closed` o `?status=all`, con su estado de subida |
| `GET` | `/api/v1/health` | estado, cola pendiente, contadores y últimos eventos de procesamiento (`recent_events`) |

## Qué hay dentro

**`acoustics.py` y `comfort.py` no tocan entrada ni salida.** Son la parte valiosa y la única
que se puede probar sin levantar nada: llevan dentro las normas.

- `acoustics.py` — Leq **energético** y percentiles ISO 1996. Los decibelios no se promedian
  aritméticamente: para `[50, 50, 50, 80]` dB la media aritmética da 57,5 y la energética
  **74,0**. Y `L90` es el percentil 10, no el 90 — es el nivel superado el 90 % del tiempo,
  o sea el ruido de fondo.
- `comfort.py` — solver PMV/PPD de ISO 7730. El PPD toca su mínimo de **exactamente 5 %** en
  PMV≈0, como exige la norma. Asunciones, que van al informe y no se esconden: `tr = ta`,
  `vel = 0,1 m/s`, `met = 1,2`, `clo = 0,6`.

El resto sí toca el mundo: `app.py` expone HTTP, `ingest.py` y `mqtt_ingest.py` son las dos
puertas de entrada, `models.py` es la persistencia (Peewee sobre SQLite) y `aggregator.py`
cierra los minutos y mantiene la cola de subida.

## Dos decisiones que conviene conocer

**Un minuto se cierra por marca de agua, no por reloj de pared.** Se cierra cuando llegan
datos de un minuto posterior *para esa sala*. Cerrar por `utcnow()` parecía natural y produjo
450 lotes ingresados y **0 minutos agregados**: un ESP32 que aún no ha sincronizado por NTP
reporta 1970, y con reloj de pared sus minutos no cierran nunca.

**La entrega al cloud es *at-least-once*.** El Edge reintenta cuando no
confirma la subida, porque perder datos es peor que duplicarlos. En una corrida de 3 horas
simuladas con 3 salas produjo 540 minutos y el cloud recibió 543. **Deduplicar es
responsabilidad de quien recibe**, y el cloud actualiza por `(room_id, ts)`.

La entrada del Edge tambien deduplica: usar `boot_id` nuevo en cada arranque y
`seq` creciente dentro de ese arranque. Un lote nuevo devuelve 201; su reintento
exacto devuelve 200 con `status=duplicate`. Reutilizar la misma identidad con
otro contenido devuelve 400. Los clientes anteriores sin `boot_id` se identifican
por dispositivo, secuencia y fecha original. La identidad se conserva durante la
retencion de lotes crudos (14 dias por defecto).

Se validan objetos, tipos, valores finitos, rangos, diez niveles de un segundo y
70 posiciones de histograma. Un reloj no sincronizado conserva el tratamiento
`clock_suspect`; los reintentos mantienen esa identidad incluso si se usa la hora
de recepcion. Los lotes tardios invalidan el agregado existente, que se recalcula
y vuelve a la cola. Una confirmacion HTTP de una revision anterior no confirma
la nueva revision. El sobre enviado al Cloud incluye `site_code=SITE_CODE`.

La actualizacion de SQLite es aditiva y se realiza al iniciar. Conserva lecturas
y duplicados historicos; no corrige ni elimina automaticamente datos anteriores.

## Alertas hacia el Cloud

El Edge abre y cierra las alertas en local, sin internet, y las sube a
`POST /api/v1/alerts/events` (`ALERTS_URL`) en el mismo ciclo que los minutos:

```json
{ "site_code": "site-1", "events": [
  { "source_key": "edge-1:42", "room_id": "sala-03", "rule": "sustained_noise",
    "severity": "warning", "message": "Noise above 65 dBA for 2 min", "value": 66.4,
    "opened_at": "2026-11-08T14:10:00Z", "closed_at": null } ] }
```

- `source_key` es `EDGE_CODE:id local` y es la clave de idempotencia: reenviar actualiza, no
  duplica. Una alerta sube abierta y vuelve a subir con `closed_at` cuando se cierra; si se
  cerró antes de la primera subida, va una sola vez.
- Sin red, o con una respuesta que no es 2xx, nada se marca y se reintenta en la siguiente
  vuelta (`AGG_INTERVAL_S`).
- `site_code` va siempre (`SITE_CODE`): sin él, el Cloud responde
  `400 MONITORING_SITE_CODE_REQUIRED` cuando tiene más de un local.
- `404 MONITORING_ROOM_NOT_FOUND` y cualquier `400` (lote vacío, severidad o regla inválidas,
  cierre anterior a la apertura) son definitivos: el Cloud rechaza el lote entero y no guarda
  nada. Si el lote tenía una sola alerta, se marca fallida (`upload_error` con el código) y sale
  de la cola; si tenía varias, se reenvían una por una para que solo falle la culpable. Un 404
  con otro código, 401/403 y 5xx se reintentan.
- `GET /api/v1/health` informa `pending_alert_upload` y `failed_alert_upload`.
- Una alerta se cierra tras `ALERT_CLOSE_MINUTES` minutos normales consecutivos; un hueco o
  un minuto sin lectura del indicador no cuenta. `closed_at` es el fin del ultimo minuto de
  esa ventana y nunca es anterior a `opened_at` (con datos atrasados se iguala a la apertura).

**Migración de SQLite.** `init_db` añade a la tabla `alert` las columnas `closure_uploaded`
y `upload_error` con `ALTER TABLE ... ADD COLUMN` al arrancar. No hace falta borrar `edge.db`:
las alertas existentes se conservan y, como nunca se habían subido, entran en la cola.

## Eventos de procesamiento

Cada evento sale como una linea `key=value` en el logger `edge.processing`, y los ultimos 20
se consultan en `recent_events` de `GET /api/v1/health` (se guardan en memoria):

```
event=minute.processed room_id=sala-03 ts=2026-11-27T14:10:00Z logged_at=2026-11-27T14:11:05Z
event=batch.rejected room_id=sala-03 ts=2026-11-27T14:10:00Z cause="campos invalidos o ausentes ['seq']" logged_at=2026-11-27T14:10:21Z
```

| Evento | Cuando |
|---|---|
| `minute.processed` | se agrego el minuto de una sala |
| `minute.failed` | fallo la agregacion; lleva `cause` |
| `batch.rejected` | un lote o mensaje (HTTP o MQTT) no se pudo procesar; lleva `cause` |

`ts` es el minuto de la medicion y `logged_at` la hora del registro. Si el mensaje no trae
sala o fecha legibles, el campo vale `unknown`.

## Pruebas de la conexion

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Configuración

Todo por variables de entorno, con valores por defecto en `edge/config.py`. Las que se tocan:

| Variable | Por defecto | |
|---|---|---|
| `CLOUD_URL` | `http://127.0.0.1:8080/api/v1/readings` | a dónde sube |
| `CLOUD_API_KEY` | vacío | la credencial que emite el cloud. Sin ella, 401 |
| `ALERTS_URL` | `http://127.0.0.1:8080/api/v1/alerts/events` | a dónde suben las alertas. La credencial necesita `alerts:write` |
| `EDGE_PORT` | `5000` | |
| `EDGE_CODE` | `edge-1` | con qué nombre se presenta en el latido |
| `SITE_CODE` | `site-1` | el código del local en el cloud. Si no existe allí, el latido recibe 404 |
| `HEARTBEAT_URL` | `http://127.0.0.1:8080/api/v1/edges/heartbeat` | a dónde manda el latido, cada `HEARTBEAT_S` segundos (60) |
| `MQTT_ENABLED` | `true` | ponlo en `false` si no hay broker |
| `NOISE_LIMIT_DBA` | `65` | umbral local de aviso |
| `NOISE_LIMIT_MINUTES` | `2` | minutos consecutivos sobre el umbral para abrir la alerta de ruido |
| `ALERT_CLOSE_MINUTES` | el de `NOISE_LIMIT_MINUTES` | minutos normales consecutivos para cerrar una alerta de ruido o de confort térmico |
| `CLO` · `MET` · `AIR_VEL` | `0.6` · `1.2` · `0.1` | asunciones del modelo de confort |
