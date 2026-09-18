"""
Pruebas de la capa de riesgo. Una prueba por regla del IPS / reglas de la estrategia.
Correr: python -m pytest tests/ -q
"""
from datetime import date, timedelta
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from agente.risk import RiskEngine, OrderProposal, PortfolioState, Position
from agente.config import LIMITS

TODAY = date(2026, 9, 18)  # viernes
ENG = RiskEngine()


def state(equity=10_000.0, cash=None, positions=None, **kw) -> PortfolioState:
    s = PortfolioState(equity=equity, cash=equity if cash is None else cash, positions=positions or {},
                       high_water_mark=equity, day_start_equity=equity, week_start_equity=equity, **kw)
    return s


def buy(ticker="SPY", qty=1.0, px=760.0, ref=758.0, stop=745.0, **kw) -> OrderProposal:
    return OrderProposal(ticker=ticker, side="BUY", qty=qty, limit_price=px, ref_close=ref, stop_price=stop, reason="entry", **kw)


def sell(ticker="SPY", qty=1.0, px=756.0, ref=758.0, **kw) -> OrderProposal:
    return OrderProposal(ticker=ticker, side="SELL", qty=qty, limit_price=px, ref_close=ref, reason="stop_trail", **kw)


# ---------------------------------------------------------------- caso base
def test_compra_valida_aprobada():
    d = ENG.evaluate(buy(), state(), TODAY)
    assert d.approved, d.reasons
    assert "whitelisted" in d.checks and "cash_ok" in d.checks


def test_venta_valida_aprobada():
    s = state(positions={"SPY": Position(2.0, 758.0)})
    d = ENG.evaluate(sell(qty=2.0), s, TODAY)
    assert d.approved, d.reasons


# ---------------------------------------------------------------- kill switch
def test_kill_switch_bloquea_todo():
    s = state(positions={"SPY": Position(1.0, 758.0)})
    ENG.kill(s)
    assert not ENG.evaluate(buy(), s, TODAY)
    d = ENG.evaluate(sell(), s, TODAY)
    assert not d and d.reasons[0].startswith("KILL_SWITCH")


def test_unkill_requiere_nota():
    s = state(); ENG.kill(s)
    with pytest.raises(ValueError):
        ENG.unkill(s, "ok")
    ENG.unkill(s, "Revisado: falsa alarma de conexión, reanudamos.")
    assert not s.killed


# ---------------------------------------------------------------- lista blanca
@pytest.mark.parametrize("ticker", ["AAPL", "EURUSD", "TSLA", "BVL:CVERDEC1", "SPXL"])
def test_fuera_de_lista_blanca_rechazado(ticker):
    d = ENG.evaluate(buy(ticker=ticker, px=100, ref=100, stop=98), state(), TODAY)
    assert not d and any(r.startswith("NOT_WHITELISTED") for r in d.reasons)


def test_lista_blanca_no_distingue_mayusculas():
    assert ENG.evaluate(buy(ticker="spy"), state(), TODAY).approved


# ---------------------------------------------------------------- solo largo
def test_venta_sin_posicion_rechazada():
    d = ENG.evaluate(sell(), state(), TODAY)
    assert not d and any(r.startswith("NO_SHORTS") for r in d.reasons)


def test_venta_mayor_a_la_posicion_rechazada():
    s = state(positions={"SPY": Position(1.0, 758.0)})
    d = ENG.evaluate(sell(qty=1.5), s, TODAY)
    assert not d and any(r.startswith("NO_SHORTS") for r in d.reasons)


# ---------------------------------------------------------------- ida y vuelta mismo día
def test_no_comprar_y_vender_lo_mismo_el_mismo_dia():
    s = state(positions={"SPY": Position(1.0, 758.0)}, traded_today={"SPY": {"BUY"}})
    d = ENG.evaluate(sell(), s, TODAY)
    assert not d and any(r.startswith("SAME_DAY_ROUND_TRIP") for r in d.reasons)
    s2 = state(traded_today={"SPY": {"SELL"}})
    assert not ENG.evaluate(buy(), s2, TODAY)


def test_evaluate_registra_traded_today_al_aprobar_y_bloquea_la_vuelta():
    # Sin preset manual: la propia evaluate() debe dejar constancia de la compra aprobada, y esa
    # constancia (no una que el test arme a mano) es la que bloquea la venta del mismo ticker el
    # mismo día. Pendiente encontrado en la evaluación de avance (18-sep-2026).
    s = state()
    d1 = ENG.evaluate(buy(), s, TODAY)
    assert d1.approved
    assert s.traded_today == {"SPY": {"BUY"}}

    s.positions["SPY"] = Position(1.0, 758.0)
    d2 = ENG.evaluate(sell(), s, TODAY)
    assert not d2 and any(r.startswith("SAME_DAY_ROUND_TRIP") for r in d2.reasons)


def test_evaluate_no_registra_traded_today_si_la_orden_se_rechaza():
    s = state(cash=10.0)  # efectivo insuficiente -> rechazada
    d = ENG.evaluate(buy(), s, TODAY)
    assert not d.approved
    assert s.traded_today == {}


# ---------------------------------------------------------------- órdenes límite
def test_limite_de_compra_fuera_de_banda():
    d = ENG.evaluate(buy(px=758 * 1.006, ref=758), state(), TODAY)
    assert not d and any(r.startswith("LIMIT_TOO_HIGH") for r in d.reasons)


def test_limite_de_venta_fuera_de_banda():
    s = state(positions={"SPY": Position(1.0, 758.0)})
    d = ENG.evaluate(sell(px=758 * 0.994, ref=758), s, TODAY)
    assert not d and any(r.startswith("LIMIT_TOO_LOW") for r in d.reasons)


def test_orden_de_mercado_prohibida():
    p = OrderProposal("SPY", "BUY", 1, 760, 758, kind="MKT", stop_price=745)  # type: ignore[arg-type]
    d = ENG.evaluate(p, state(), TODAY)
    assert not d and any("ORDER_KIND_NOT_ALLOWED" in r for r in d.reasons)


def test_stop_limit_residente_valido():
    s = state(positions={"SPY": Position(1.0, 758.0)})
    p = OrderProposal("SPY", "SELL", 1, limit_price=745 * 0.99, ref_close=758, kind="STP LMT", stop_price=745, reason="protective")
    assert ENG.evaluate(p, s, TODAY).approved


def test_stop_limit_con_limite_demasiado_bajo():
    s = state(positions={"SPY": Position(1.0, 758.0)})
    p = OrderProposal("SPY", "SELL", 1, limit_price=745 * 0.98, ref_close=758, kind="STP LMT", stop_price=745)
    assert not ENG.evaluate(p, s, TODAY)


# ---------------------------------------------------------------- reglas de compra
def test_compra_sin_stop_rechazada():
    d = ENG.evaluate(buy(stop=None), state(), TODAY)
    assert not d and any(r.startswith("STOP_REQUIRED") for r in d.reasons)


def test_stop_por_encima_del_precio_rechazado():
    d = ENG.evaluate(buy(stop=770), state(), TODAY)
    assert not d and any(r.startswith("STOP_INVALID") for r in d.reasons)


def test_riesgo_por_operacion_supera_2pct():
    # 1 acción a 760 con stop en 500 → riesgo 260 = 2.6% de 10,000
    d = ENG.evaluate(buy(qty=1, px=760, ref=758, stop=500), state(), TODAY)
    assert not d and any(r.startswith("RISK_TOO_HIGH") for r in d.reasons)


def test_tope_nominal_10pct_por_posicion():
    # 1.4 acciones × 760 = 1,064 > 1,000
    d = ENG.evaluate(buy(qty=1.4, px=760, ref=758, stop=755), state(), TODAY)
    assert not d and any(r.startswith("POSITION_CAP") for r in d.reasons)


def test_tope_nominal_cuenta_la_posicion_existente():
    s = state(positions={"SPY": Position(1.0, 758.0)})  # ya hay 758
    d = ENG.evaluate(buy(qty=0.4, px=760, ref=758, stop=755), s, TODAY)  # +304 → 1,062
    assert not d and any(r.startswith("POSITION_CAP") for r in d.reasons)


def test_tope_de_renta_variable_50pct():
    pos = {t: Position(1.0, 900.0) for t in ["SPY", "VTI", "QQQ", "EFA", "VWO"]}  # 4,500 = 45%
    s = state(cash=5_500, positions=pos)
    d = ENG.evaluate(buy(ticker="XLK", qty=1.0, px=900, ref=898, stop=890), s, TODAY)  # → 5,400 = 54%
    assert not d and any(r.startswith("EQUITY_CAP") for r in d.reasons)


def test_sleeve_conservador_exento_de_topes_de_renta_variable():
    # BIL puede ser el 90% del portafolio sin stop y sin contar como renta variable.
    d = ENG.evaluate(OrderProposal("BIL", "BUY", 98, 91.5, 91.5, reason="sleeve"), state(), TODAY)
    assert d.approved, d.reasons


def test_maximo_de_posiciones():
    pos = {t: Position(0.1, 100.0) for t in ["SPY", "VTI", "QQQ", "EFA", "VWO", "XLK", "XLF"]}  # 7 posiciones
    s = state(positions=pos)
    eng = RiskEngine(limits=LIMITS.__class__(max_positions=7))
    d = eng.evaluate(buy(ticker="EFA", qty=1.0, px=104.3, ref=104, stop=100), s, TODAY)  # EFA ya existe → ok
    assert d.approved, d.reasons
    # una octava distinta no cabría con max_positions=7: quitamos EFA y probamos XLF nuevo
    pos2 = {t: Position(0.1, 100.0) for t in ["SPY", "VTI", "QQQ", "EFA", "VWO", "XLK", "BIL"]}  # 6 equity + BIL
    s2 = state(positions=pos2)
    eng6 = RiskEngine(limits=LIMITS.__class__(max_positions=6))
    d2 = eng6.evaluate(buy(ticker="XLF", qty=0.1, px=57, ref=57, stop=55), s2, TODAY)
    assert not d2 and any(r.startswith("MAX_POSITIONS") for r in d2.reasons)


def test_efectivo_insuficiente():
    s = state(cash=500.0)
    d = ENG.evaluate(buy(qty=1.0, px=760, ref=758, stop=745), s, TODAY)
    assert not d and any(r.startswith("INSUFFICIENT_CASH") for r in d.reasons)


def test_orden_demasiado_pequena():
    d = ENG.evaluate(buy(qty=0.01, px=760, ref=758, stop=745), state(), TODAY)
    assert not d and any(r.startswith("ORDER_TOO_SMALL") for r in d.reasons)


def test_numeros_invalidos():
    for bad in (0, -1, float("nan"), float("inf")):
        d = ENG.evaluate(buy(qty=bad), state(), TODAY)
        assert not d


# ---------------------------------------------------------------- frenos
def test_breaker_de_portafolio_10pct_detiene_compras_pero_no_ventas():
    s = state(positions={"SPY": Position(1.0, 758.0)})
    ev = ENG.update_marks(s, 8_999.0, TODAY)  # -10.01%
    assert s.halted and ev and ev[0].kind == "PORTFOLIO_HALT"
    assert not ENG.evaluate(buy(), s, TODAY)
    assert ENG.evaluate(sell(), s, TODAY).approved  # la salida de protección sí pasa


def test_high_water_mark_sube_con_la_cuenta():
    s = state()
    ENG.update_marks(s, 11_000.0, TODAY)
    assert s.high_water_mark == 11_000.0
    ev = ENG.update_marks(s, 10_000.0, TODAY)  # -9.1% desde 11,000: no dispara
    assert not s.halted and not [e for e in ev if e.kind == "PORTFOLIO_HALT"]
    ev = ENG.update_marks(s, 9_899.0, TODAY)   # -10.01%
    assert s.halted


def test_deposito_no_infla_el_maximo_ni_retiro_dispara_freno():
    s = state()
    ENG.record_flow(s, 5_000.0)              # depósito
    assert s.high_water_mark == 15_000.0 and s.equity == 15_000.0
    ENG.record_flow(s, -5_000.0)             # retiro
    ev = ENG.update_marks(s, 10_000.0, TODAY)
    assert not s.halted and not ev


def test_reactivacion_manual_reinicia_el_maximo_y_exige_nota():
    s = state()
    ENG.update_marks(s, 8_900.0, TODAY)
    assert s.halted
    with pytest.raises(ValueError):
        ENG.reactivate(s, "ok")
    ev = ENG.reactivate(s, "Caída de mercado general, estrategia se comportó como en backtest.")
    assert not s.halted and s.high_water_mark == 8_900.0 and ev.kind == "REACTIVATED"
    # La reactivación levanta el ALTO TOTAL, pero no borra la pausa diaria/semanal:
    # una caída de -11% en un día también disparó el freno diario, que dura hasta el lunes.
    d = ENG.evaluate(buy(qty=0.5, px=760, ref=758, stop=745), s, TODAY)
    assert not d and any(r.startswith("PAUSED_UNTIL") for r in d.reasons)
    # Caso limpio: alto total sin caída intradía (la caída venía de días anteriores).
    s2 = state()
    s2.day_start_equity, s2.week_start_equity = 8_950.0, 9_000.0
    ENG.update_marks(s2, 8_900.0, TODAY)
    assert s2.halted and s2.paused_until is None
    ENG.reactivate(s2, "Caída acumulada de varias semanas; se revisó y se reactiva.")
    assert ENG.evaluate(buy(qty=0.5, px=760, ref=758, stop=745), s2, TODAY).approved


def test_breaker_diario_pausa_compras_hasta_el_lunes():
    s = state()
    ev = ENG.update_marks(s, 9_690.0, TODAY)  # -3.1% en el día
    assert ev and ev[0].kind == "DAILY_PAUSE"
    assert s.paused_until == date(2026, 9, 21)  # lunes
    assert not ENG.evaluate(buy(), s, TODAY)
    assert ENG.evaluate(buy(), s, date(2026, 9, 21)).approved  # el lunes vuelve a comprar


def test_breaker_semanal():
    s = state()
    s.day_start_equity = 9_600.0            # hoy casi plano...
    ev = ENG.update_marks(s, 9_490.0, TODAY)  # ...pero -5.1% en la semana
    assert ev and ev[0].kind == "WEEKLY_PAUSE"


def test_roll_day_limpia_operaciones_del_dia():
    s = state(traded_today={"SPY": {"BUY"}})
    ENG.roll_day(s, date(2026, 9, 21))  # lunes
    assert s.traded_today == {} and s.week_start_equity == s.equity
