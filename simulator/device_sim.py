"""
Simulador del modulo ESP32.

Hace EXACTAMENTE lo que hara el firmware en C++, con los mismos pasos y el mismo
contrato, para que cuando llegue el hardware solo haya que desenchufar esto:

    ventanas de 125 ms -> Leq de 1 s (energetico) -> histograma cada 10 s -> POST

Uso:
    python device_sim.py                                   # 3 salas, tiempo real
    python device_sim.py --speed 120                       # 120x mas rapido
    python device_sim.py --rooms sala-01,sala-02 --speed 60
    python device_sim.py --edge-url http://192.168.1.50:5000/api/v1/readings

--speed sirve para generar horas de historia en minutos y tener con que probar
los dashboards del cloud sin esperar un dia entero.
"""
import argparse
import datetime
import json
import math
import random
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Falta requests:  pip install requests")

HIST_MIN_DB = 30.0
HIST_BIN_DB = 1.0
HIST_BINS = 70
WINDOWS_PER_SECOND = 8          # ventanas de 125 ms (ponderacion temporal Fast)
SECONDS_PER_BATCH = 10


# ------------------------------------------------------- utilidades dB ----

def leq(levels):
    """Promedio ENERGETICO. Los dB no se promedian aritmeticamente."""
    total = sum(10.0 ** (lv / 10.0) for lv in levels)
    return 10.0 * math.log10(total / len(levels))


def hist_index(level):
    return max(0, min(HIST_BINS - 1, int((level - HIST_MIN_DB) / HIST_BIN_DB)))


# ------------------------------------------------------------- modelo ----

def occupancy_pressure(dt):
    """Probabilidad de que alguien entre a la sala, segun hora y dia."""
    if dt.weekday() >= 5:
        return 0.02
    h = dt.hour
    if 9 <= h < 13:  return 0.60
    if 13 <= h < 14: return 0.20
    if 14 <= h < 19: return 0.65
    if 8 <= h < 9:   return 0.25
    if 19 <= h < 21: return 0.10
    return 0.01


class Room:
    def __init__(self, room_id, index):
        self.room_id = room_id
        self.device_id = "esp32-" + room_id
        self.seq = 0
        self.occupied = False
        self.session_left_s = 0
        # transiciones DENTRO de la ventana actual de 10 s, no acumuladas:
        # el contrato dice "transiciones en este lote". Si se manda el
        # acumulado, el Edge lo suma 6 veces al agregar el minuto.
        self.transitions = 0

        # cada sala tiene su caracter: una da a la calle, otra tiene el aire
        # acondicionado encima, otra esta al fondo y es la silenciosa
        self.base_empty = 36.0 + index * 2.5
        self.base_occupied = 54.0 + index * 2.0
        self.temp = 21.5 + index * 0.4
        self.rh = 58.0 + index * 2.0
        self.setpoint = 21.0 + index * 0.5

    def step_occupancy(self, dt, elapsed_s):
        if self.occupied:
            self.session_left_s -= elapsed_s
            if self.session_left_s <= 0:
                self.occupied = False
                self.transitions += 1
        else:
            p = occupancy_pressure(dt) * (elapsed_s / 600.0)
            if random.random() < p:
                self.occupied = True
                self.session_left_s = random.uniform(20 * 60, 90 * 60)
                self.transitions += 1

    def step_climate(self, elapsed_s):
        # con gente adentro la sala se calienta; el aire acondicionado no
        # siempre da abasto -> de aqui sale la "deriva termica" que detecta
        # salas mal climatizadas
        target_t = self.setpoint + (3.2 if self.occupied else 0.0)
        target_h = 55.0 + (9.0 if self.occupied else 0.0)
        k = 1.0 - math.exp(-elapsed_s / 900.0)          # constante ~15 min
        self.temp += (target_t - self.temp) * k + random.gauss(0, 0.015)
        self.rh += (target_h - self.rh) * k + random.gauss(0, 0.08)
        self.rh = max(25.0, min(85.0, self.rh))

    def window_levels(self):
        """8 ventanas de 125 ms, como las calculara el ESP32."""
        out = []
        for _ in range(WINDOWS_PER_SECOND):
            if self.occupied:
                lv = random.gauss(self.base_occupied, 4.0)
                if random.random() < 0.04:              # risa, silla, portazo
                    lv += random.uniform(6, 15)
            else:
                lv = random.gauss(self.base_empty, 1.5)
                if random.random() < 0.01:              # ruido de pasillo
                    lv += random.uniform(4, 10)
            out.append(max(HIST_MIN_DB, min(99.9, lv)))
        return out

    def build_batch(self, ts):
        """Un lote de 10 s, listo para POST al Edge."""
        laeq_1s, hist = [], [0] * HIST_BINS
        lmax, lmin = -999.0, 999.0

        for _ in range(SECONDS_PER_BATCH):
            windows = self.window_levels()
            for lv in windows:
                hist[hist_index(lv)] += 1
                lmax = max(lmax, lv)
                lmin = min(lmin, lv)
            laeq_1s.append(round(leq(windows), 1))

        self.seq += 1
        transitions = self.transitions
        self.transitions = 0          # se reinicia al cerrar el lote
        return {
            "device_id": self.device_id,
            "room_id": self.room_id,
            "fw_version": "0.1.0-sim",
            "seq": self.seq,
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_s": SECONDS_PER_BATCH,
            "acoustic": {
                "laeq_1s": laeq_1s,
                "lmax": round(lmax, 1),
                "lmin": round(lmin, 1),
                "hist_min_db": HIST_MIN_DB,
                "hist_bin_db": HIST_BIN_DB,
                "hist": hist,
            },
            "climate": {
                "temp_c": round(self.temp, 2),
                "rh_pct": round(self.rh, 1),
            },
            "presence": {
                "state": "occupied" if self.occupied else "vacant",
                "occupied_s": float(SECONDS_PER_BATCH) if self.occupied else 0.0,
                "transitions": transitions,
            },
        }


def connect_mqtt(rooms, args):
    """
    Un cliente MQTT por sala, igual que habra un ESP32 por sala.

    Cada uno declara su testamento antes de conectar: si el proceso muere de
    mala manera, el broker publica 'offline' en su nombre. Esa es la diferencia
    con HTTP -- la caida se anuncia, no se deduce del silencio.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("Falta paho-mqtt:  pip install paho-mqtt")

    clients = {}
    for room in rooms:
        status_topic = "{}/{}/device/{}/status".format(
            args.mqtt_prefix, args.site, room.device_id)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=room.device_id)
        if args.mqtt_user:
            client.username_pw_set(args.mqtt_user, args.mqtt_pass)
        client.will_set(status_topic, "offline", qos=1, retain=True)
        client.connect(args.mqtt_host, args.mqtt_port, keepalive=30)
        client.loop_start()
        client.publish(status_topic, "online", qos=1, retain=True)
        clients[room.room_id] = client
    return clients


# --------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(description="Simulador de modulos ESP32")
    ap.add_argument("--transport", choices=("http", "mqtt"), default="http",
                    help="por donde entrega los lotes al Edge")
    ap.add_argument("--edge-url", default="http://127.0.0.1:5000/api/v1/readings")
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user", default="esp32")
    ap.add_argument("--mqtt-pass", default="esp32")
    ap.add_argument("--mqtt-prefix", default="comfort")
    ap.add_argument("--site", default="site-1")
    ap.add_argument("--rooms", default="sala-01,sala-02,sala-03")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="factor de aceleracion del tiempo simulado")
    ap.add_argument("--start", default=None,
                    help="hora simulada de inicio, ISO. Por defecto: ahora")
    ap.add_argument("--minutes", type=int, default=0,
                    help="minutos simulados a generar; 0 = sin fin")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="imprime el payload en vez de enviarlo")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    names = [r.strip() for r in args.rooms.split(",") if r.strip()]
    rooms = [Room(name, i) for i, name in enumerate(names)]

    sim_t = (datetime.datetime.fromisoformat(args.start) if args.start
             else datetime.datetime.utcnow())

    destino = (args.edge_url if args.transport == "http"
               else "mqtt://{}:{}".format(args.mqtt_host, args.mqtt_port))
    print("Simulando {} salas -> {} ({})".format(len(rooms), destino, args.transport))
    print("Velocidad x{}   inicio {}Z".format(args.speed, sim_t.isoformat()))
    print("Ctrl+C para parar\n")

    session = requests.Session()
    mqtt_clients = {}
    if args.transport == "mqtt" and not args.dry_run:
        mqtt_clients = connect_mqtt(rooms, args)
    sent = failed = 0
    deadline = args.minutes * 60 if args.minutes else None
    simulated = 0

    try:
        while deadline is None or simulated < deadline:
            for room in rooms:
                room.step_occupancy(sim_t, SECONDS_PER_BATCH)
                room.step_climate(SECONDS_PER_BATCH)
                batch = room.build_batch(sim_t)

                if args.dry_run:
                    print(json.dumps(batch, indent=2)[:900])
                elif args.transport == "mqtt":
                    topic = "{}/{}/room/{}/readings".format(
                        args.mqtt_prefix, args.site, room.room_id)
                    info = mqtt_clients[room.room_id].publish(
                        topic, json.dumps(batch), qos=1)
                    if info.rc == 0:
                        sent += 1
                    else:
                        failed += 1
                else:
                    try:
                        r = session.post(args.edge_url, json=batch, timeout=5)
                        if 200 <= r.status_code < 300:
                            sent += 1
                        else:
                            failed += 1
                    except requests.RequestException:
                        failed += 1     # el ESP32 real bufferea; aqui solo contamos

            if sent and sent % (len(rooms) * 6) == 0:
                states = " ".join(
                    "{}:{}{:.0f}dB/{:.1f}C".format(
                        rm.room_id.split("-")[-1],
                        "*" if rm.occupied else ".",
                        rm.base_occupied if rm.occupied else rm.base_empty,
                        rm.temp)
                    for rm in rooms)
                print("{}Z  lotes {}  fallidos {}  | {}".format(
                    sim_t.strftime("%H:%M"), sent, failed, states))

            sim_t += datetime.timedelta(seconds=SECONDS_PER_BATCH)
            simulated += SECONDS_PER_BATCH
            if not args.dry_run:
                time.sleep(SECONDS_PER_BATCH / args.speed)

    except KeyboardInterrupt:
        print("\nDetenido.")

    finally:
        for client in mqtt_clients.values():
            # Desconexion limpia: el broker NO ejecuta el testamento.
            # Para ver el testamento en accion hay que matar el proceso.
            client.disconnect()
            client.loop_stop()

    print("Lotes enviados: {}   fallidos: {}".format(sent, failed))


if __name__ == "__main__":
    main()
