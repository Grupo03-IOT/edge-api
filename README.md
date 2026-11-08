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

## Levantarlo

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export CLOUD_URL=http://127.0.0.1:8080/api/v1/readings
export MQTT_ENABLED=false        # true necesita Mosquitto; ver broker/
python edge/app.py
```

Queda en `http://localhost:5000`. Sin el cloud levantado también funciona: los minutos se
quedan en la cola hasta que responda.

> El cloud **debe tener un local creado** antes de recibir nada, o rechaza la subida con
> `MONITORING_NO_SITE_AVAILABLE`.

## Conectar el ESP32 de la guia

El firmware para DHT22, PIR, HC-SR04, potenciometro, buzzer y tres pulsadores esta
en [`firmware/`](firmware/README.md). Respeta los GPIO de la guia y entrega los
niveles del potenciometro por el contrato normal de ruido.

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
La implementacion C++ para el montaje actual esta en `firmware/zenroom_esp32`.

## Endpoints

| Verbo | Ruta | Para qué |
|---|---|---|
| `POST` | `/api/v1/readings` | lo que envía el dispositivo cada 10 s |
| `GET` | `/api/v1/rooms` | salas con su último minuto |
| `GET` | `/api/v1/rooms/<id>/minutes` | la serie por minuto |
| `GET` | `/api/v1/alerts` | alertas abiertas |
| `GET` | `/api/v1/health` | estado, cola pendiente y contadores |

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

## Pruebas de la conexion

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Configuración

Todo por variables de entorno, con valores por defecto en `edge/config.py`. Las que se tocan:

| Variable | Por defecto | |
|---|---|---|
| `CLOUD_URL` | `http://127.0.0.1:8080/api/v1/readings` | a dónde sube |
| `CLOUD_API_KEY` | vacío | la credencial que emite el cloud. Sin ella, 401 |
| `EDGE_PORT` | `5000` | |
| `EDGE_CODE` | `edge-1` | con qué nombre se presenta en el latido |
| `SITE_CODE` | `site-1` | el código del local en el cloud. Si no existe allí, el latido recibe 404 |
| `HEARTBEAT_URL` | `http://127.0.0.1:8080/api/v1/edges/heartbeat` | a dónde manda el latido, cada `HEARTBEAT_S` segundos (60) |
| `MQTT_ENABLED` | `true` | ponlo en `false` si no hay broker |
| `NOISE_LIMIT_DBA` | `65` | umbral local de aviso |
| `CLO` · `MET` · `AIR_VEL` | `0.6` · `1.2` · `0.1` | asunciones del modelo de confort |
