"""
Confort termico PMV / PPD  --  ISO 7730 (modelo de Fanger).

Reemplaza al Heat Index, que NO sirve en interiores: la regresion de Rothfusz
(NOAA) solo es valida desde ~26.7 C y >=40% HR. En una oficina a 22 C devuelve
la temperatura seca y no aporta informacion.

Entradas MEDIDAS por el SHT31:  ta, rh
Entradas ASUMIDAS y documentadas (ver ASSUMPTIONS): tr, vel, met, clo
"""
import math

# Asunciones de ingenieria. Van en el informe, no se esconden.
ASSUMPTIONS = {
    "tr": "= ta (interior sin fuentes radiantes fuertes)",
    "vel": 0.1,   # m/s   aire interior en reposo
    "met": 1.2,   # met   trabajo de oficina sentado (tabla ISO 7730)
    "clo": 0.6,   # clo   ropa ligera, clima de Lima
}


def pmv_ppd(ta, rh, tr=None, vel=0.1, met=1.2, clo=0.6, wme=0.0):
    """
    ta   temperatura del aire [C]
    rh   humedad relativa [%]
    tr   temperatura radiante media [C]  (por defecto = ta)
    vel  velocidad del aire [m/s]
    met  tasa metabolica [met]      1 met = 58.15 W/m2
    clo  aislamiento de la ropa [clo]
    wme  trabajo externo [met]

    Devuelve (pmv, ppd).
      PMV va de -3 (frio) a +3 (calor).
      ASHRAE 55 acepta -0.5 < PMV < +0.5, equivalente a PPD < 10%.
      El PPD nunca baja de 5%: siempre hay un 5% de insatisfechos.
    """
    if tr is None:
        tr = ta

    # presion parcial de vapor de agua [Pa]
    pa = rh * 10.0 * math.exp(16.6536 - 4030.183 / (ta + 235.0))

    icl = 0.155 * clo               # aislamiento [m2 K/W]
    m = met * 58.15                 # metabolismo [W/m2]
    w = wme * 58.15
    mw = m - w

    # factor de area de la ropa
    fcl = 1.0 + 1.290 * icl if icl <= 0.078 else 1.05 + 0.645 * icl

    hcf = 12.1 * math.sqrt(vel)     # conveccion forzada
    taa = ta + 273.0
    tra = tr + 273.0

    # --- solver iterativo de la temperatura de superficie de la ropa (tcl) ---
    # No hay forma cerrada: tcl aparece a ambos lados y elevada a la 4a por el
    # termino de radiacion. Se resuelve por punto fijo.
    tcla = taa + (35.5 - ta) / (3.5 * (6.45 * icl + 0.1))
    p1 = icl * fcl
    p2 = p1 * 3.96
    p3 = p1 * 100.0
    p4 = p1 * taa
    p5 = 308.7 - 0.028 * mw + p2 * (tra / 100.0) ** 4

    xn = tcla / 100.0
    xf = xn
    hc = hcf
    for _ in range(150):
        xf = (xf + xn) / 2.0
        hcn = 2.38 * abs(100.0 * xf - taa) ** 0.25   # conveccion natural
        hc = max(hcf, hcn)
        xn = (p5 + p4 * hc - p2 * xf ** 4) / (100.0 + p3 * hc)
        if abs(xn - xf) <= 0.00015:
            break
    tcl = 100.0 * xn - 273.0

    # --- terminos de perdida de calor ---
    hl1 = 3.05e-3 * (5733.0 - 6.99 * mw - pa)          # difusion por la piel
    hl2 = 0.42 * (mw - 58.15) if mw > 58.15 else 0.0   # sudoracion
    hl3 = 1.7e-5 * m * (5867.0 - pa)                   # respiracion latente
    hl4 = 0.0014 * m * (34.0 - ta)                     # respiracion seca
    hl5 = 3.96 * fcl * (xn ** 4 - (tra / 100.0) ** 4)  # radiacion
    hl6 = fcl * hc * (tcl - ta)                        # conveccion

    ts = 0.303 * math.exp(-0.036 * m) + 0.028
    pmv = ts * (mw - hl1 - hl2 - hl3 - hl4 - hl5 - hl6)
    ppd = 100.0 - 95.0 * math.exp(-0.03353 * pmv ** 4 - 0.2179 * pmv ** 2)

    return round(pmv, 2), round(ppd, 1)


def verdict(pmv):
    """Etiqueta legible para la UI. La escala de sensacion termica de ISO 7730."""
    if pmv < -2.5:  return "cold"
    if pmv < -1.5:  return "cool"
    if pmv < -0.5:  return "slightly_cool"
    if pmv <= 0.5:  return "neutral"
    if pmv <= 1.5:  return "slightly_warm"
    if pmv <= 2.5:  return "warm"
    return "hot"
