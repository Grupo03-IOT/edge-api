# ESP32 del montaje SenseWork ZenRoom

El programa esta en `zenroom_esp32/zenroom_esp32.ino`. Lee el montaje de la guia
enviada y envia por HTTP el contrato que acepta esta Edge API. El potenciometro
produce niveles entre 30 y 99,9 para el flujo normal de ruido: agregacion,
umbrales, analitica y subida al Cloud usan esos valores directamente.

## Pines

| Componente | GPIO |
|---|---|
| DHT22 | 23 |
| PIR | 27 |
| HC-SR04 TRIG / ECHO | 22 / 35 |
| Potenciometro | 34 |
| Buzzer | 26 |
| Pulsadores 1 / 2 / 3 | 13 / 25 / 32 |
| LED verde / ambar / rojo | 21 / 19 / 18 |

Mantener el divisor de ECHO descrito en la guia. El firmware configura los botones
con `INPUT_PULLUP`: pulsado es LOW.

La guia original dejaba los LED desconectados. Para utilizar ahora el indicador
rojo, conectar **GPIO18 -> resistencia de 220 ohm -> anodo del LED rojo** y su
**catodo -> GND comun**. El anodo suele ser la pata larga; el catodo corresponde
al lado plano del encapsulado. Verde y ambar usan el mismo esquema desde GPIO21
y GPIO19, cada uno con su propia resistencia de 220 ohm. Hacer estos cambios con
el USB desconectado y conservar aislados los carriles derechos de la guia.

## Que ocurre al conectarlo por USB

El ESP32 es una computadora pequena: el programa que se graba en su memoria se
llama **firmware**. Al recibir alimentacion por USB arranca ese programa guardado.
El cable permite alimentarlo, cargar un programa nuevo y leer mensajes por el
monitor serie. Los archivos abiertos en IntelliJ pertenecen a la laptop;
el programa de la placa se carga desde Arduino IDE.

La primera vez se configura y carga el firmware siguiendo los pasos siguientes.
Despues queda guardado: cada vez que se conecta el USB, la placa lo ejecuta sin
tener que cargarlo otra vez. Solo se vuelve a cargar al cambiar el programa,
la red Wi-Fi o la direccion del Edge configurada en `DeviceConfig.h`.

Con este programa, al arrancar configura sensores, botones, LED y buzzer; busca
la red Wi-Fi; lee el DHT22 y la presencia; obtiene la hora UTC; y guarda y envia
un lote cada diez segundos. Los LED y el aviso sonoro se evaluan localmente y
funcionan tambien mientras se espera Wi-Fi o la hora. Los envios usan **Wi-Fi**.
El USB mantiene la alimentacion y permite observar los mensajes de diagnostico.

En la laptop se inician PostgreSQL, Cloud y Edge cuando se quiere recibir y
consultar datos. Encender la placa no inicia esos programas en Windows. El
recorrido es **sensores -> ESP32 -> Wi-Fi -> Edge en la laptop -> Cloud**.

## Cargar desde Arduino IDE

1. Instalar el paquete **esp32 by Espressif Systems 3.x** y las bibliotecas
   **DHT sensor library by Adafruit** y **Adafruit Unified Sensor**.
2. Abrir el archivo `.ino`; seleccionar **ESP32 Dev Module** y el puerto de la placa.
   Conectar con un cable USB de datos. Elegir el COM que aparezca al conectarla.
3. Editar `DeviceConfig.h`: Wi-Fi, `DEVICE_ID`, `ROOM_ID` y la URL del Edge con la
   **IP LAN de la laptop**, por ejemplo `http://192.168.1.50:5000/api/v1/readings`.
   La laptop y la placa deben poder comunicarse por Wi-Fi; permitir TCP 5000 en
   la red privada. `localhost` desde la placa apunta a la propia placa.
4. Preparar una particion de archivos para la cola. En una placa nueva destinada
   a esta prueba, establecer `FORMAT_FILESYSTEM_ON_FAILURE=true` para el primer
   arranque: **formatea la particion si no puede montarla**. Despues volverlo a
   `false`. No habilitarlo si esa particion contiene archivos que se deben conservar.
   Usar una particion de Arduino con espacio para archivos, por ejemplo la
   predeterminada con SPIFFS (LittleFS utiliza esa particion).
5. Compilar, cargar y abrir el monitor serie a **115200**. Esperar hora UTC por NTP
   y una lectura valida del DHT22. Durante el primer minuto se ignora el PIR para
   permitir que se estabilice; el ultrasonido ya puede detectar presencia.

El boton **Verificar** (marca de verificacion) compila el programa. El boton
**Subir** (flecha) lo graba en el ESP32 por USB. Esperar a que termine la carga;
la placa se reinicia y empieza a ejecutarlo. Si la carga se queda en Connecting,
mantener BOOT mientras intenta conectar y soltarlo cuando empiece a escribir.
Abrir el monitor serie desde Herramientas o su icono y seleccionar 115200 baudios.

No se envian temperaturas inventadas si falla el DHT22: se omite la ventana y el
monitor serie indica el problema. El inicio de cada ventana se alinea a diez
segundos UTC. Ocho lecturas del potenciometro por segundo forman diez niveles
de un segundo y un histograma de 70 posiciones, compatibles con el Edge.

El monitor serie muestra `ruido=` y `ADC=` junto a `T=` y `RH=`. Al girar el
potenciometro deben variar el ADC (0-4095) y el ruido (30-99,9). Temperatura y
humedad pertenecen al DHT22 y no cambian por girar el potenciometro. Para probar
sus extremos, mantener cada posicion al menos diez segundos, de forma que
tambien quede registrada en un lote del Edge.

## Botones para las pruebas manuales

| Boton | Accion | Que permite comprobar |
|---|---|---|
| 1, GPIO13 | Tono de 250 ms | Cableado, polaridad y salida PWM del buzzer |
| 2, GPIO25 | Detener el tono y silenciar avisos automaticos durante 60 s | Reconocer un aviso sin cambiar el color ni las lecturas |
| 3, GPIO32 | Alternar automatica -> ocupada -> vacia -> automatica | Probar la ocupacion sin mover los sensores |

La presencia manual caduca en 60 segundos. Su resultado se envia en `presence`
por el mismo contrato que la presencia de sensores. No hace falta registrar
pulsaciones ni agregar endpoints para estas pruebas. El boton 1 sigue disponible
durante el silencio porque es una orden explicita de prueba.

En automatico, PIR o una distancia valida menor a 150 cm detectan presencia y
la mantienen durante 30 s; ajustar esas constantes a la ubicacion fisica.
Es una aproximacion a presencia, no un conteo de personas.

## LED rojo y buzzer de hasta cuatro segundos

El firmware usa una misma evaluacion de confort para los LED y el buzzer. Los
limites proceden de la demostracion de la seccion 5.6 del
[reporte de Drive](https://docs.google.com/document/d/1Do_EZFLR-pAPAt3UhMtksx--obsYepHLIUdLKyBqsJU/edit?usp=drivesdk).

| Estado | Condicion | Indicador |
|---|---|---|
| Sin datos | DHT22 sin lectura valida | Los tres LED apagados |
| Rojo | Ruido >60 con presencia, temperatura <18 o >28 C, humedad <25 o >70 % | Rojo intermitente y aviso sonoro al entrar |
| Moderado | Ruido >55 con presencia, temperatura <20 o >26 C, humedad <30 o >60 %, sin llegar a rojo | Ambar fijo |
| Optimo | Las demas lecturas validas | Verde fijo |

El rojo se enciende inmediatamente al entrar en el estado critico y parpadea
cada medio segundo. El buzzer suena **una vez, durante un maximo de cuatro
segundos por entrada a rojo**. Si la sala sale de rojo antes, se corta el tono.
Mantener el rojo o su parpadeo no repite ni prolonga el sonido. Para un nuevo
aviso debe salir del estado rojo y volver a entrar.

`ENABLE_AUTOMATIC_BUZZER=true` deja habilitado este comportamiento. El boton 2
interrumpe el tono y silencia las nuevas entradas a rojo durante 60 segundos;
al vencer el silencio, una misma alerta roja no vuelve a sonar por si sola.
El boton 1 conserva su prueba manual de 250 ms. Las peticiones repetidas no
extienden un tono activo, y entre tonos se mantiene una pausa de 250 ms.

Una tarea propia controla el tiempo del buzzer, para que una espera de red o
almacenamiento no mantenga el tono encendido. El aviso se decide en el ESP32;
el Edge conserva sus reglas de alerta y sus umbrales descargados del Cloud.

`BUZZER_ACTIVE_LOW=true` corresponde al modulo pasivo de disparo bajo de la guia.
Si el modulo real difiere, confirmar su tipo y ajustar la polaridad. El reposo
mantiene el nivel inactivo y el tono se produce con PWM de Arduino-ESP32 3.x.

## Cola y reintentos

Cada lote se guarda como archivo y solo se elimina cuando el Edge confirma un
HTTP 2xx. La tarea de red trabaja por separado para que una espera HTTP no
detenga los botones ni el muestreo. Tras una caida de Wi-Fi o un reinicio, se
reintentan los mismos archivos con su `boot_id`, `seq` y `ts` originales.
La cola admite 120 lotes (20 minutos); si se llena o falla el almacenamiento,
el monitor serie informa los lotes nuevos que no pudieron guardarse.
Ante un HTTP 400 repetido, revisar el contrato/configuracion: el archivo se
conserva y no se confirma ni se descarta silenciosamente.

## Prueba del recorrido completo

1. Preparar administrador, local y credencial siguiendo el README de `cloud-api`.
2. Ejecutar el Edge con `MQTT_ENABLED=false`, la credencial y `SITE_CODE` correcto.
3. Encender la placa; observar `Lote ... guardado` y consultar
   `http://localhost:5000/api/v1/health`: aumentan `raw_batches` y luego `minutes`.
4. Girar el potenciometro y usar el boton 3 para comprobar cambios de ocupacion.
5. Interrumpir el Edge y volver a iniciarlo: la placa conserva y reenvia la cola;
   un reintento confirmado aparece como `duplicate` y no suma otro lote.
6. Consultar las salas en el Cloud y sus lecturas usando el UUID devuelto por el
   Cloud. `pending_upload` del Edge debe bajar cuando el Cloud acepta los minutos.

API de PWM utilizada: [LEDC de Espressif](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/ledc.html).
Entrada del potenciometro: [ADC de Espressif](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/adc.html).
Carga por USB: [guia de Arduino IDE](https://support.arduino.cc/hc/en-us/articles/4733418441116-Upload-a-sketch-in-Arduino-IDE).
