#include <cassert>
#include <cstdio>
#include "../zenroom_esp32/DeviceSignals.h"

int main() {
  // Los mismos umbrales deciden el LED y el aviso: ruido solo con presencia.
  assert(comfortState(true, 23, 50, true, 61) == ComfortState::Red);
  assert(comfortState(true, 23, 50, false, 99) == ComfortState::Optimal);
  assert(comfortState(true, 23, 50, true, 60) == ComfortState::Moderate);
  assert(comfortState(true, 23, 50, true, 55) == ComfortState::Optimal);
  assert(comfortState(true, 28, 50, false, 30) == ComfortState::Moderate);
  assert(comfortState(true, 28.1f, 50, false, 30) == ComfortState::Red);
  assert(comfortState(true, 17.9f, 50, false, 30) == ComfortState::Red);
  assert(comfortState(true, 23, 24.9f, false, 30) == ComfortState::Red);
  assert(comfortState(true, 23, 70.1f, false, 30) == ComfortState::Red);
  assert(comfortState(false, 99, 99, true, 99) == ComfortState::NoData);

  // Mantener el estado rojo (incluido su parpadeo) no repite la alarma.
  RedAlertLatch red;
  assert(red.update(ComfortState::Red, false));
  for (int i = 0; i < 100; ++i) assert(!red.update(ComfortState::Red, false));
  assert(!red.update(ComfortState::Optimal, false));
  assert(red.update(ComfortState::Red, false));
  assert(!red.update(ComfortState::Moderate, false));
  assert(!red.update(ComfortState::Red, true));
  assert(!red.update(ComfortState::Red, false));  // expirar el silencio no reinicia el aviso
  assert(!red.update(ComfortState::NoData, false));
  assert(red.update(ComfortState::Red, false));

  // Ninguna orden puede prolongar un tono activo mas alla de cuatro segundos.
  BuzzerWindow tone;
  assert(!tone.start(0, 0));
  assert(tone.start(1000, 60000));
  assert(tone.remaining(1000) == 4000);
  assert(!tone.start(4900, 4000));
  assert(!tone.expired(4999));
  assert(tone.expired(5000));
  tone.stop(5000);
  assert(!tone.active());
  assert(!tone.start(5000, 250));
  assert(!tone.start(5249, 250));
  assert(tone.start(5250, 250));
  assert(!tone.expired(5499));
  assert(tone.expired(5500));
  tone.stop(5500);

  // El limite sigue funcionando cuando el contador millis vuelve a cero.
  BuzzerWindow rollover;
  const uint32_t start = 0xfffffff0u;
  assert(rollover.start(start, 4000));
  assert(!rollover.expired(start + 3999u));
  assert(rollover.expired(start + 4000u));
  rollover.stop(start + 4000u);
  assert(!rollover.start(start + 4100u, 250));
  assert(rollover.start(start + 4250u, 250));
  std::puts("OK: umbrales LED, aviso por entrada a rojo, silencio, limite de 4 s y rollover");
}
