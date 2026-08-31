# Broker MQTT

Mosquitto en la Raspberry Pi. Es por donde los ESP32 entregan sus lotes al Edge.

## Topics

| Topic | Quien publica | Que lleva |
|---|---|---|
| `comfort/<site>/room/<code>/readings` | ESP32 | el lote de 10 s, JSON |
| `comfort/<site>/device/<code>/status` | **el broker**, por testamento | `online` / `offline` |

El segundo es el Last Will and Testament: el ESP32 se lo deja al broker al
conectarse, y el broker lo publica en su nombre cuando la conexion se cae. Por
eso una caida llega como noticia en segundos, en vez de deducirse de varios
minutos de silencio.

## Arrancar

```bash
sudo apt install mosquitto mosquitto-clients

mosquitto_passwd -c broker/passwd esp32       # pide contrasena
mosquitto_passwd    broker/passwd edge-api

mkdir -p broker/data
mosquitto -c broker/mosquitto.conf -v
```

## Comprobar que llega algo

```bash
mosquitto_sub -h localhost -u edge-api -P <clave> -t 'comfort/#' -v
```

## Que debe hacer el firmware al conectar

```cpp
// el testamento, ANTES de conectar
client.connect(clientId, user, pass,
               "comfort/site-1/device/esp32-sala-01/status",  // topic
               1,        // QoS
               true,     // retained: quien se suscriba despues lo ve
               "offline");

// y al lograr conexion, anunciarse
client.publish("comfort/site-1/device/esp32-sala-01/status", "online", true);
```
