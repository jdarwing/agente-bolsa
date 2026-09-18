"""
Pruebas del módulo de estrategia (`agente/strategy.py`) — una por regla de
`reglas-entrada-salida-estrategia.md` (secciones 2-6).

Estas pruebas verifican la LÓGICA DE LA SEÑAL para un ticker aislado. No reproducen el
backtest de 10 años operación por operación a propósito: ese backtest simula 7 tickers
compitiendo por 8 cupos y un tope de 50% de exposición (ver `backtest/engine.py::run`,
sección de entradas) — esa arbitración de cupos es trabajo de `risk.py` + el ciclo diario
(`runner.py`, todavía no construido), no de `strategy.py`. Sí se verificó por separado,
manualmente, que `strategy.indicators()` reproduce EXACTO (diferencia 0.0) los valores de
`backtest/engine.py::indicators()` sobre los datos reales de SPY — eso es lo que garantiza
que la base de cálculo es la misma; lo que sigue prueba que las reglas de decisión sobre esa
base son correctas.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from agente.risk import LIMITS, RiskEngine
from agente.strategy import (
    EntrySignal,
    OpenPosition,
    Strategy,
    StrategyParams,
    TickerMemory,
    _business_days_between,
    indicators,
)

S = Strategy()


def row(sma=100.0, ema_f=105.0, ema_s=100.0, atr=2.0, close=110.0, bull=True, bull_run=2) -> pd.Series:
    return pd.Series({"sma": sma, "ema_f": ema_f, "ema_s": ema_s, "atr": atr, "close": close,
                       "bull": bull, "bull_run": bull_run})


# ---------------------------------------------------------------- indicadores
def test_indicadores_producen_las_columnas_esperadas_y_sin_look_ahead():
    idx = pd.date_range("2020-01-01", periods=260, freq="B")
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, len(idx)))
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close}, index=idx)
    ind = indicators(df)
    assert {"sma", "ema_f", "ema_s", "atr", "bull", "bull_run"}.issubset(ind.columns)
    assert ind["sma"].iloc[:199].isna().all()          # SMA200 no existe antes del día 200
    assert ind["sma"].iloc[199:].notna().all()
    assert (ind["atr"].dropna() > 0).all()
    assert (ind.loc[~ind["bull"], "bull_run"] == 0).all()   # racha en 0 mientras no hay régimen alcista


def test_racha_de_cierres_se_reinicia_al_perder_el_cruce():
    idx = pd.date_range("2020-01-01", periods=5, freq="B")
    df = pd.DataFrame({"ema_f": [1, 2, 3, 2, 3], "ema_s": [2, 1, 1, 3, 1]}, index=idx)  # bull: F,T,T,F,T
    bull = df["ema_f"] > df["ema_s"]
    grp = (bull != bull.shift()).cumsum()
    run = bull.groupby(grp).cumcount() + 1
    run[~bull] = 0
    assert list(run) == [0, 1, 2, 0, 1]


# ---------------------------------------------------------------- entradas (§2-3)
def test_regimen_bloquea_la_entrada_aunque_haya_cruce_confirmado():
    r = row(sma=120.0, close=110.0, bull=True, bull_run=2)   # cierre bajo SMA200
    assert S.check_entry("SPY", r, TickerMemory(), date(2026, 1, 5)) is None


def test_entrada_exige_exactamente_2_cierres_de_confirmacion():
    mem = TickerMemory()
    assert S.check_entry("SPY", row(bull_run=1), mem, date(2026, 1, 5)) is None   # 1 solo cierre: no
    sig = S.check_entry("SPY", row(bull_run=2), mem, date(2026, 1, 5))            # 2 cierres: sí
    assert sig is not None and sig.ticker == "SPY"


def test_entrada_no_dispara_dos_veces_seguidas_en_una_tendencia_larga():
    mem = TickerMemory()
    assert S.check_entry("SPY", row(bull_run=2), mem, date(2026, 1, 5)) is not None
    # al tercer y cuarto cierre alcista (ya con posición abierta el runner no volvería a llamar
    # a check_entry, pero si se llamara, la regla de "==2" ya no vuelve a cumplirse):
    assert S.check_entry("SPY", row(bull_run=3), mem, date(2026, 1, 6)) is None
    assert S.check_entry("SPY", row(bull_run=4), mem, date(2026, 1, 7)) is None


def test_reentrada_tras_stop_exige_10_dias_habiles_de_cooldown():
    mem = TickerMemory(need_new_cross=True, last_stop_exit=date(2026, 9, 18))  # viernes
    # 9 días hábiles después: sigue bloqueado
    assert S.check_entry("SPY", row(bull_run=2), mem, date(2026, 10, 1)) is None
    assert mem.need_new_cross is True
    # 10 días hábiles después: se libera y, como ya tuvo un stop antes, no exige bull_run==2 exacto
    sig = S.check_entry("SPY", row(bull_run=7), mem, date(2026, 10, 2))
    assert sig is not None
    assert mem.need_new_cross is False


def test_reentrada_tras_stop_sin_cooldown_cumplido_no_exige_bull_run_exacto_pero_sigue_bloqueada():
    # aunque bull_run ya sea "válido" (>=2), si el cooldown no se cumplió, no hay señal.
    mem = TickerMemory(need_new_cross=True, last_stop_exit=date(2026, 9, 18))
    assert S.check_entry("SPY", row(bull_run=5), mem, date(2026, 9, 21)) is None


def test_caer_bajo_el_cruce_libera_la_espera_de_cooldown():
    mem = TickerMemory(need_new_cross=True, last_stop_exit=date(2026, 9, 18))
    assert S.check_entry("SPY", row(bull=False, bull_run=0), mem, date(2026, 9, 21)) is None
    assert mem.need_new_cross is False   # el próximo cruce alcista ya cuenta como "nuevo"


def test_salida_de_regimen_no_activa_cooldown_de_reentrada():
    # Comportamiento verificado también en el motor de backtest aprobado (engine.py): una salida
    # por fin de régimen no agrega el ticker a la lista de "espera cruce nuevo" — en la práctica
    # el propio cruce EMA ya lo exige, porque para caer bajo SMA200 casi siempre EMA20<EMA50 antes.
    mem = TickerMemory()
    S.on_exit_filled(mem, "regime", date(2026, 3, 1))
    assert mem.need_new_cross is False and mem.last_stop_exit is None


def test_salida_por_stop_si_activa_cooldown_de_reentrada():
    mem = TickerMemory()
    S.on_exit_filled(mem, "stop_trail", date(2026, 3, 1))
    assert mem.need_new_cross is True and mem.last_stop_exit == date(2026, 3, 1)


# ---------------------------------------------------------------- salidas (§4)
def test_salida_por_regimen_tiene_prioridad_sobre_el_stop():
    pos = OpenPosition(entry=100.0, stop=95.0, hi_close=100.0, atr_at_entry=2.0, entry_date=date(2026, 1, 1))
    r = row(sma=105.0, close=94.0, atr=2.0)   # cierre bajo SMA200 Y bajo el stop
    sig = S.check_exit("SPY", r, pos)
    assert sig is not None and sig.reason == "regime"


def test_stop_inicial_se_distingue_del_trailing():
    pos = OpenPosition(entry=100.0, stop=100 - 1.5 * 2.0, hi_close=100.0, atr_at_entry=2.0,
                        entry_date=date(2026, 1, 1))
    r = row(sma=50.0, close=96.0, atr=2.0)    # cae directo al stop inicial, nunca ganó 1 ATR
    sig = S.check_exit("SPY", r, pos)
    assert sig is not None and sig.reason == "stop_init"


def test_trailing_se_arma_solo_tras_superar_1_atr_de_ganancia():
    pos = OpenPosition(entry=100.0, stop=100 - 1.5 * 2.0, hi_close=100.0, atr_at_entry=2.0,
                        entry_date=date(2026, 1, 1))
    # ganancia de 10 (>1 ATR=2 de entrada): el trailing se activa. hi_close-k_trail*atr = 110-10=100,
    # que ya supera el stop inicial (97) -> el stop sube a 100, no al inicial.
    r = row(sma=90.0, close=110.0, atr=2.0)
    S.check_exit("SPY", r, pos)
    assert pos.stop == pytest.approx(100.0)


def test_trailing_armado_no_baja_el_stop_si_el_nivel_calculado_es_menor():
    pos = OpenPosition(entry=100.0, stop=97.0, hi_close=100.0, atr_at_entry=2.0, entry_date=date(2026, 1, 1))
    # se arma (ganancia 3 > 2), pero hi_close-k_trail*atr = 103-10=93 < stop actual 97: no baja.
    r = row(sma=90.0, close=103.0, atr=2.0)
    S.check_exit("SPY", r, pos)
    assert pos.stop == 97.0


def test_el_stop_solo_sube_nunca_baja():
    pos = OpenPosition(entry=100.0, stop=97.0, hi_close=104.0, atr_at_entry=2.0, entry_date=date(2026, 1, 1))
    # un día de ATR grande que produciría un trailing MÁS BAJO que el stop actual: no debe bajar
    r = row(sma=90.0, close=101.0, atr=10.0)   # hi_close-k_trail*atr = 104-50 = -46 << 97
    S.check_exit("SPY", r, pos)
    assert pos.stop == 97.0


def test_hi_close_solo_sube_con_el_maximo_de_cierre_desde_la_entrada():
    pos = OpenPosition(entry=100.0, stop=97.0, hi_close=110.0, atr_at_entry=2.0, entry_date=date(2026, 1, 1))
    r = row(sma=90.0, close=105.0, atr=2.0)   # cierre de hoy por debajo del máximo ya alcanzado
    S.check_exit("SPY", r, pos)
    assert pos.hi_close == 110.0


def test_sin_senal_de_salida_devuelve_none():
    pos = OpenPosition(entry=100.0, stop=90.0, hi_close=100.0, atr_at_entry=2.0, entry_date=date(2026, 1, 1))
    r = row(sma=90.0, close=101.0, atr=2.0)
    assert S.check_exit("SPY", r, pos) is None


# ---------------------------------------------------------------- dimensionamiento (§5)
def test_tamano_de_entrada_usa_el_menor_entre_riesgo_y_tope_nominal():
    # stop muy cerca del precio -> el riesgo permitiría comprar mucho más que el 10% nominal:
    # el tope nominal debe mandar (igual que en el backtest, sección 5 de las reglas).
    sig = EntrySignal(ticker="SPY", ref_close=100.0, atr=1.0, stop=99.5)  # riesgo = 0.5
    p = S.entry_proposal(sig, equity_now=10_000.0)
    limit_price = 100.0 * (1 + LIMITS.limit_band)
    risk_qty = (LIMITS.risk_pct_max * 10_000.0) / (limit_price - 99.5)
    notional_qty = (LIMITS.max_notional_pct * 10_000.0) / limit_price
    assert p.qty == pytest.approx(min(risk_qty, notional_qty))
    assert p.qty == pytest.approx(notional_qty)


def test_tamano_de_entrada_usa_riesgo_cuando_el_stop_esta_lejos():
    sig = EntrySignal(ticker="SPY", ref_close=100.0, atr=20.0, stop=70.0)  # riesgo amplio: 30
    p = S.entry_proposal(sig, equity_now=10_000.0)
    limit_price = 100.0 * (1 + LIMITS.limit_band)
    risk_qty = (LIMITS.risk_pct_max * 10_000.0) / (limit_price - 70.0)
    notional_qty = (LIMITS.max_notional_pct * 10_000.0) / limit_price
    assert risk_qty < notional_qty   # confirma que este caso sí ejercita la rama de riesgo
    assert p.qty == pytest.approx(min(risk_qty, notional_qty))
    assert p.qty == pytest.approx(risk_qty)


# ---------------------------------------------------------------- las propuestas pasan por risk.py
def test_propuesta_de_entrada_bien_formada_es_aprobada_por_la_capa_de_riesgo():
    from agente.risk import PortfolioState
    sig = EntrySignal(ticker="SPY", ref_close=100.0, atr=2.0, stop=97.0)
    p = S.entry_proposal(sig, equity_now=10_000.0)
    eng = RiskEngine()
    st = PortfolioState(equity=10_000.0, cash=10_000.0, high_water_mark=10_000.0,
                         day_start_equity=10_000.0, week_start_equity=10_000.0)
    d = eng.evaluate(p, st, date(2026, 9, 18))
    assert d.approved, d.reasons


def test_propuesta_de_salida_se_aprueba_incluso_con_frenos_de_compra_activos():
    from agente.risk import PortfolioState, Position
    from agente.strategy import ExitSignal
    sig = ExitSignal(ticker="SPY", reason="stop_trail", ref_close=95.0, new_stop=95.0)
    p = S.exit_proposal(sig, qty=10.0)
    eng = RiskEngine()
    st = PortfolioState(equity=8_900.0, cash=100.0, positions={"SPY": Position(10.0, 95.0)},
                         high_water_mark=10_000.0, day_start_equity=10_000.0, week_start_equity=10_000.0,
                         halted=True)   # alto total activo — solo debe bloquear COMPRAS
    d = eng.evaluate(p, st, date(2026, 9, 18))
    assert d.approved, d.reasons


def test_orden_stop_limite_residente_tiene_forma_valida_para_la_capa_de_riesgo():
    from agente.risk import PortfolioState, Position
    p = S.resident_stop_proposal("SPY", qty=10.0, stop=95.0, last_close=100.0)
    assert p.kind == "STP LMT" and p.side == "SELL"
    eng = RiskEngine()
    st = PortfolioState(equity=10_000.0, cash=1_000.0, positions={"SPY": Position(10.0, 100.0)},
                         high_water_mark=10_000.0, day_start_equity=10_000.0, week_start_equity=10_000.0)
    d = eng.evaluate(p, st, date(2026, 9, 18))
    assert d.approved, d.reasons


# ---------------------------------------------------------------- helper de fechas
def test_dias_habiles_entre_fechas_viernes_a_viernes_dos_semanas():
    assert _business_days_between(date(2026, 9, 18), date(2026, 10, 2)) == 10
    assert _business_days_between(date(2026, 9, 18), date(2026, 10, 1)) == 9
    assert _business_days_between(date(2026, 9, 18), date(2026, 9, 18)) == 0
