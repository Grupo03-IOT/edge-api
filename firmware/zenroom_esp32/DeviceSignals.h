#pragma once
#include <stdint.h>

enum class ComfortState { NoData, Optimal, Moderate, Red };

inline ComfortState comfortState(bool climateValid, float temp, float rh,
                                 bool occupied, float noise) {
  if (!climateValid) return ComfortState::NoData;
  if ((occupied && noise > 60) || temp < 18 || temp > 28 || rh < 25 || rh > 70)
    return ComfortState::Red;
  if ((occupied && noise > 55) || temp < 20 || temp > 26 || rh < 30 || rh > 60)
    return ComfortState::Moderate;
  return ComfortState::Optimal;
}

class RedAlertLatch {
 public:
  bool update(ComfortState state, bool muted) {
    const bool red = state == ComfortState::Red;
    const bool entered = red && !wasRed_;
    wasRed_ = red;
    return entered && !muted;
  }
 private:
  bool wasRed_ = false;
};

// Solo la tarea del buzzer modifica esta ventana. Una orden repetida no
// prolonga el sonido y una nueva rafaga requiere una pausa audible.
class BuzzerWindow {
 public:
  static constexpr uint32_t MaxMs = 4000;
  static constexpr uint32_t PauseMs = 250;

  bool start(uint32_t now, uint32_t requestedMs) {
    if (active_ || requestedMs == 0 || (hasStopped_ && now - stoppedAt_ < PauseMs))
      return false;
    startedAt_ = now;
    duration_ = requestedMs > MaxMs ? MaxMs : requestedMs;
    active_ = true;
    return true;
  }
  bool active() const { return active_; }
  bool expired(uint32_t now) const {
    return active_ && now - startedAt_ >= duration_;
  }
  uint32_t remaining(uint32_t now) const {
    if (!active_ || expired(now)) return 0;
    return duration_ - (now - startedAt_);
  }
  void stop(uint32_t now) {
    if (active_) { stoppedAt_ = now; hasStopped_ = true; }
    active_ = false;
  }
 private:
  bool active_ = false, hasStopped_ = false;
  uint32_t startedAt_ = 0, duration_ = 0, stoppedAt_ = 0;
};

enum class BuzzerAction { Tone, StopAll, StopAutomatic };
struct BuzzerCommand {
  BuzzerAction action;
  uint32_t durationMs;
  bool automatic;
};
