# Sensores reales por USB y las tres aplicaciones en local

El ESP32 conserva el sketch original y todos sus GPIO. No necesita Wi-Fi.
El puente lee los bloques de Serial a 115200 y los envia al Edge de la laptop.
El programa sigue controlando el buzzer de la misma forma.

## 1. Cloud local: PostgreSQL y Spring

Desde `cloud-api`, con Docker Desktop abierto y JDK 21:

```powershell
docker compose up -d db
$env:JAVA_HOME = 'C:\Users\Sandra\.jdks\ms-21.0.10'
.\gradlew.bat bootRun
```

El servicio debe responder en `http://127.0.0.1:8080/actuator/health`.
Sigue `cloud-api/README.md` para registrar una cuenta, habilitar el administrador,
crear el local `site-1` y emitir una credencial con `readings:write`,
`thresholds:read` y `edges:heartbeat`. Todo se hace contra el Cloud **local**.
Las cuentas y claves de Azure pertenecen a otra base y no se copian solas.

## 2. Edge local

En otra terminal, desde `edge-api`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MQTT_ENABLED = 'false'
$env:CLOUD_URL = 'http://127.0.0.1:8080/api/v1/readings'
$env:HEARTBEAT_URL = 'http://127.0.0.1:8080/api/v1/edges/heartbeat'
$env:THRESHOLDS_URL = 'http://127.0.0.1:8080/api/v1/room-thresholds'
$env:CLOUD_API_KEY = 'TU_CLAVE_EMITIDA_POR_CLOUD_LOCAL'
$env:SITE_CODE = 'site-1'
$env:EDGE_DB = 'edge-local.db'
.\.venv\Scripts\python.exe edge/app.py
```

Usar `edge-local.db` separa esta prueba de las bases y colas anteriores.
`http://127.0.0.1:5000/api/v1/health` muestra `telemetry_batches`, los minutos
pendientes y si se configuro una credencial, sin revelar la clave.

## 3. Puente USB

Conecta el ESP32. **Cierra Serial Monitor y Serial Plotter de Arduino** para
liberar su puerto. En otra terminal de `edge-api`:

```powershell
.\.venv\Scripts\python.exe tools/serial_bridge.py --list-ports
.\.venv\Scripts\python.exe tools/serial_bridge.py --port COM5 --room sala-01
```

Sustituye COM5 por el puerto que aparezca. Si solo hay uno, el puente lo detecta
automaticamente. `--device` permite identificar otro ESP32 y `--window-s` debe
coincidir con el intervalo del sketch (2 por defecto). Abrir el puerto puede
reiniciar el ESP32; espera a que vuelva a imprimir bloques completos.

Los reintentos conservan fecha e identidad y no duplican muestras. Si Edge
se detiene, la cola se guarda en `serial-outbox.json` (ignorada por Git) y se
reanuda al reiniciar el puente. No ejecutes dos puentes con la misma cola.
No reutilices una cola pendiente al cambiar de equipo, sala o instalacion.

## 4. Pagina local

Desde `frontend-web`:

```powershell
npm run dev:local
```

Abre `http://127.0.0.1:5173`, entra con la cuenta **local**, selecciona el local
`site-1` y abre `sala-01` en Salas. Selecciona actualizar cada 10 s o 30 s.
Cloud registra la sala y el dispositivo automaticamente con el primer minuto.

La web muestra temperatura y humedad medias del minuto, ocupacion estimada a
partir del PIR y la ultima muestra de movimiento, distancia, ADC y botones.
La primera lectura puede tardar hasta **dos minutos**: cierre del minuto,
agregacion y actualizacion de la web. El historial conserva un registro/minuto.
Los errores de DHT y la ausencia de eco se conservan como datos ausentes.
El potenciometro se muestra en **ADC 0–4095**, no como ruido medido en dBA.

Con el sketch actual, una pulsacion breve entre las impresiones de 2 s puede
no aparecer; no hay eventos de pulsacion ni estado del buzzer en el texto serial.
Para registrar cada evento haria falta ampliar el sketch, algo que esta conexion
USB no requiere.

## Despues de verificar el hardware

Revisar los cambios de los tres repositorios antes de integrarlos a `main`.
Cloud necesita la migracion `V4__physical_sensors.sql` para conservar estos
campos. Publicar Cloud antes del frontend. Para enviar a Azure, cambiar las
tres URL de Edge y usar una credencial emitida por esa instalacion.
La laptop y el puente deben permanecer encendidos mientras el ESP32 use USB.
