"""
Metricas acusticas segun ISO 1996.

La regla que hay que tener presente en todo este modulo: los decibelios son
logaritmicos y NO se promedian aritmeticamente. Promediar dB directamente
subestima siempre el resultado cuando hay variacion, y en una sala de reuniones
la hay toda.
"""
import math

HIST_MIN_DB = 30.0
HIST_BIN_DB = 1.0
HIST_BINS = 70          # 30 dB .. 100 dB


def leq(levels):
    """
    Nivel continuo equivalente: promedio ENERGETICO de una lista de niveles en dB.

        Leq = 10 * log10( (1/N) * sum(10^(Li/10)) )

    Devuelve None si la lista viene vacia.
    """
    if not levels:
        return None
    total = sum(10.0 ** (level / 10.0) for level in levels)
    return 10.0 * math.log10(total / len(levels))


def leq_from_hist(hist, min_db=HIST_MIN_DB, bin_db=HIST_BIN_DB):
    """Leq reconstruido desde un histograma de ocurrencias por bin."""
    total = sum(hist)
    if total == 0:
        return None
    energy = 0.0
    for i, count in enumerate(hist):
        if count:
            center = min_db + (i + 0.5) * bin_db
            energy += count * 10.0 ** (center / 10.0)
    return 10.0 * math.log10(energy / total)


def percentile_level(hist, n, min_db=HIST_MIN_DB, bin_db=HIST_BIN_DB):
    """
    Ln = nivel superado durante el n% del tiempo (ISO 1996).

    OJO con la definicion, que es donde se equivoca todo el mundo:
    L90 es el nivel superado el 90% del tiempo, es decir que casi siempre
    estamos POR ENCIMA de el => es un valor BAJO => es el percentil 10 del
    valor. Por eso el (1 - n/100).

        L90 -> ruido de fondo (el aire acondicionado, la calle)
        L50 -> mediana
        L10 -> picos intrusivos (voces, golpes)
    """
    total = sum(hist)
    if total == 0:
        return None
    target = total * (1.0 - n / 100.0)
    acc = 0
    for i, count in enumerate(hist):
        acc += count
        if acc > target:
            return round(min_db + (i + 0.5) * bin_db, 1)
    return round(min_db + (len(hist) - 0.5) * bin_db, 1)


def merge_hists(hists):
    """Suma histogramas bin a bin (los del minuto, para sacar percentiles)."""
    out = [0] * HIST_BINS
    for h in hists:
        for i, count in enumerate(h[:HIST_BINS]):
            out[i] += count
    return out


def hist_index(level, min_db=HIST_MIN_DB, bin_db=HIST_BIN_DB, bins=HIST_BINS):
    """Bin al que cae un nivel, saturando en los extremos."""
    idx = int((level - min_db) / bin_db)
    return max(0, min(bins - 1, idx))
