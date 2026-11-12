"""Lee el Serial Monitor original y entrega cada bloque completo al Edge local."""
import argparse
from collections import deque
import datetime
import json
import math
from pathlib import Path
import re
import sys
import time
import uuid

import requests


class SensorParser:
    """Un bloque solo es valido si contiene exactamente todas las lecturas."""
    def __init__(self):
        self.values = {}

    def feed(self, line):
        line = line.strip()
        if line.startswith("---"):
            self.values = {}
            return None
        if ":" not in line:
            return None
        label, value = (item.strip() for item in line.split(":", 1))
        try:
            if label in ("Temperatura", "Humedad", "Distancia"):
                match = re.fullmatch(r"(-?\d+(?:[.,]\d+)?)\s*(?:C|%|cm)?", value)
                if match is None and value not in ("Error", "Sin lectura"):
                    self.values = {}
                    return None
                number = float(match[1].replace(",", ".")) if match else None
                if number is not None and not math.isfinite(number):
                    number = None
                self.values[label] = number if label != "Distancia" or number is None or number >= 0 else None
            elif label == "Movimiento":
                if value not in ("SI", "NO"):
                    self.values = {}
                    return None
                self.values[label] = value == "SI"
            elif label == "Potenciometro":
                self.values[label] = int(value)
            elif label == "Botones":
                if not re.fullmatch(r"[01]\s*\|\s*[01]\s*\|\s*[01]", value):
                    self.values = {}
                    return None
                self.values[label] = [part.strip() == "1" for part in value.split("|")]
                if set(self.values) != {"Temperatura", "Humedad", "Movimiento", "Distancia", "Potenciometro", "Botones"}:
                    self.values = {}
                    return None
                v, self.values = self.values, {}
                if not 0 <= v["Potenciometro"] <= 4095:
                    return None
                return {"climate": {"temp_c": v["Temperatura"], "rh_pct": v["Humedad"]},
                        "sensors": {"movement": v["Movimiento"], "distance_cm": v["Distancia"],
                                    "potentiometer": v["Potenciometro"],
                                    **{f"button{i + 1}": pressed for i, pressed in enumerate(v["Botones"])}}}
        except ValueError:
            self.values = {}
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Puerto USB, por ejemplo COM5")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--edge-url", default="http://127.0.0.1:5000/api/v1/telemetry")
    parser.add_argument("--room", default="sala-01")
    parser.add_argument("--device", default="esp32-sala-01")
    parser.add_argument("--window-s", type=int, choices=range(1, 61), default=2, metavar="1..60")
    parser.add_argument("--outbox", type=Path, default=Path("serial-outbox.json"),
                        help="Cola local persistente para reintentar si Edge se desconecta")
    args = parser.parse_args()
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        parser.exit(1, "Falta pyserial. Ejecuta: python -m pip install -r requirements.txt\n")
    ports = list(list_ports.comports())
    if args.list_ports:
        for port in ports:
            print(f"{port.device}: {port.description}")
        if not ports:
            print("No hay puertos seriales. Conecta el ESP32 por USB.")
        return
    port = args.port or (ports[0].device if len(ports) == 1 else None)
    if not port:
        parser.error("indica --port COMx; usa --list-ports para consultar los puertos")

    # Cada lectura conserva identidad y fecha al reintentarse, incluso tras reiniciar.
    pending = deque()
    if args.outbox.exists():
        pending.extend(json.loads(args.outbox.read_text(encoding="utf-8")))

    def save():
        temporary = args.outbox.with_suffix(args.outbox.suffix + ".tmp")
        temporary.write_text(json.dumps(list(pending)), encoding="utf-8")
        temporary.replace(args.outbox)

    boot_id, seq, next_retry, last_warning = str(uuid.uuid4()), 0, 0.0, 0.0
    sensor_parser = SensorParser()
    print(f"USB {port} a 115200 -> {args.edge_url} | sala {args.room}", flush=True)
    print("Cierra Serial Monitor/Serial Plotter: el puerto solo admite un lector.", flush=True)
    try:
        # DTR/RTS se conectan al circuito de arranque del ESP32. Dejarlos
        # desactivados evita mantener ciertas placas en reset al abrir USB.
        connection = serial.Serial(baudrate=115200, timeout=0.25)
        connection.dtr = False
        connection.rts = False
        connection.port = port
        connection.open()
        with connection, requests.Session() as http:
            # Limita el tiempo HTTP para seguir vaciando el buffer serial cada 2 s.
            while True:
                data = sensor_parser.feed(connection.readline().decode("utf-8", errors="replace"))
                if data:
                    seq += 1
                    pending.append({"device_id": args.device, "room_id": args.room,
                                    "boot_id": boot_id, "seq": seq,
                                    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "window_s": args.window_s, **data})
                    save()
                    print(f"USB #{seq}: {data['climate']} | {data['sensors']}", flush=True)
                if pending and time.monotonic() >= next_retry:
                    try:
                        reply = http.post(args.edge_url, json=pending[0], timeout=(0.4, 0.8))
                        if reply.status_code not in (200, 201):
                            raise RuntimeError(f"Edge HTTP {reply.status_code}: {reply.text[:300]}")
                        pending.popleft()
                        save()
                        next_retry = time.monotonic()
                    except (requests.RequestException, RuntimeError) as exc:
                        next_retry = time.monotonic() + 3
                        if time.monotonic() - last_warning > 10:
                            print(f"Pendientes {len(pending)} en {args.outbox}: {exc}", file=sys.stderr, flush=True)
                            last_warning = time.monotonic()
    except serial.SerialException as exc:
        parser.exit(1, f"No se pudo leer {port}: {exc}. Revisa USB y cierra Serial Monitor.\n")
    except KeyboardInterrupt:
        print(f"Puente detenido. Lecturas pendientes guardadas: {len(pending)}")


if __name__ == "__main__":
    main()
