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

Vive aquí y no en su propio repositorio porque hoy su única razón de ser es probar el Edge.
Cuando exista el firmware en C++, ambos se irán al repositorio del dispositivo.

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

**La entrega al cloud es *at-least-once*, no *exactly-once*.** El Edge reintenta cuando no
confirma la subida, porque perder datos es peor que duplicarlos. En una corrida de 3 horas
simuladas con 3 salas produjo 540 minutos y el cloud recibió 543. **Deduplicar es
responsabilidad de quien recibe**, y el cloud lo hace por `(room_id, ts)`.

## Configuración

Todo por variables de entorno, con valores por defecto en `edge/config.py`. Las que se tocan:

| Variable | Por defecto | |
|---|---|---|
| `CLOUD_URL` | `http://127.0.0.1:8080/api/v1/readings` | a dónde sube |
| `EDGE_PORT` | `5000` | |
| `MQTT_ENABLED` | `true` | ponlo en `false` si no hay broker |
| `NOISE_LIMIT_DBA` | `65` | umbral local de aviso |
| `CLO` · `MET` · `AIR_VEL` | `0.6` · `1.2` · `0.1` | asunciones del modelo de confort |
