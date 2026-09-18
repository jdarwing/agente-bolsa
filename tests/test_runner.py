"""
Pruebas del ciclo diario (`agente/runner.py`) — la pieza que arbitra cupos entre los 7 tickers,
reconcilia lo que el bróker confirma contra lo que el agente recuerda, y persiste el estado
entre corridas. Usa un `FakeBroker` (nunca IB Gateway real — eso solo se puede probar desde la
Mac de Darwing, ver `broker.py`) para poder correr en la nube sin red ni conexión local.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
import pytest

from agente.config import EQUITY_TICKERS_ORDER
from agente.risk import Position
from agente.runner import (
    AgentState, _liquidity_check, _process_entries, _process_exits, _reconcile, _rebalance_if_needed,
    run_cycle, save_state,
)
from agente.strategy import OpenPosition, Strategy, TickerMemory

TODAY = date(2024, 12, 17)   # coincide con el último día de las series sintéticas de abajo


# ---------------------------------------------------------------------------- series sintéticas
def _flat_bars(periods: int = 251, price: float = 100.0, volume: float | None = None) -> pd.DataFrame:
    """251 días hábiles planos: SMA200 válida, sin cruce (bull=False) -> nunca da señal de
    entrada. Sirve de barra "sin novedad" para los tickers que no son el foco de una prueba.
    `volume` es opcional (columna omitida por defecto) — la mayoría de las pruebas no necesitan
    liquidez, y `_liquidity_check` deja pasar cuando no hay columna `volume` (fail-open)."""
    idx = pd.date_range("2024-01-02", periods=periods, freq="B")
    close = np.full(periods, price)
    data = {"open": close, "high": close + 0.5, "low": close - 0.5, "close": close}
    if volume is not None:
        data["volume"] = np.full(periods, volume)
    return pd.DataFrame(data, index=idx)


def _entry_signal_bars(volume: float | None = None) -> pd.DataFrame:
    """Termina con bull_run==2 y cierre por encima de la SMA200 -> `check_entry` da señal en el
    último día (2024-12-17), que es TODAY. Verificado a mano contra `strategy.indicators()`.
    `volume` opcional, para las pruebas del filtro de liquidez (reglas §7)."""
    idx = pd.date_range("2024-01-02", periods=251, freq="B")
    close = np.concatenate([np.full(248, 100.0), [100.0, 106.0, 108.0]])
    data = {"open": close, "high": close + 0.5, "low": close - 0.5, "close": close}
    if volume is not None:
        data["volume"] = np.full(251, volume)
    return pd.DataFrame(data, index=idx)


def _regime_exit_bars() -> pd.DataFrame:
    """Termina con un cierre muy por debajo de la SMA200 -> `check_exit` da salida de régimen
    en el último día (2024-12-17)."""
    idx = pd.date_range("2024-01-02", periods=251, freq="B")
    close = np.concatenate([np.full(250, 100.0), [80.0]])
    return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close}, index=idx)


# ---------------------------------------------------------------------------- bróker de prueba
@dataclass
class FakeBroker:
    equity: float = 100_000.0
    cash: float = 100_000.0
    positions: dict[str, Position] = field(default_factory=dict)
    avg_costs: dict[str, float] = field(default_factory=dict)
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    submitted: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    _next_order_id: int = 1000

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def portfolio_state(self):
        from agente.risk import PortfolioState
        return PortfolioState(equity=self.equity, cash=self.cash, positions=dict(self.positions))

    def average_costs(self) -> dict[str, float]:
        return dict(self.avg_costs)

    def daily_bars(self, ticker: str, duration: str = "2 Y") -> pd.DataFrame:
        return self.bars.get(ticker, _flat_bars())

    def submit_order(self, proposal) -> int:
        self._next_order_id += 1
        self.submitted.append(proposal)
        return self._next_order_id

    def cancel_order(self, order_id: int) -> None:
        self.cancelled.append(order_id)


def _paths(tmp_path):
    return tmp_path / "state.json", tmp_path / "audit.jsonl"


# ---------------------------------------------------------------------------- run_cycle: orquestación
def test_no_corre_dos_veces_el_mismo_dia(tmp_path):
    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker()
    run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
              state_path=state_path, audit_path=audit_path)
    with pytest.raises(RuntimeError):
        run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
                  state_path=state_path, audit_path=audit_path)


def test_ciclo_sin_senales_persiste_estado_y_equity(tmp_path):
    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker(equity=12_345.0, cash=12_345.0)
    report = run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
                        state_path=state_path, audit_path=audit_path)
    assert report["entries"] == [] and report["exits"] == []
    assert report["equity"] == pytest.approx(12_345.0)

    saved = AgentState.from_json(__import__("json").loads(state_path.read_text()))
    assert saved.last_run_date == TODAY.isoformat()
    assert saved.last_equity == pytest.approx(12_345.0)
    assert saved.high_water_mark == pytest.approx(12_345.0)


def test_modo_lectura_nunca_envia_ordenes_al_broker(tmp_path):
    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker(bars={"SPY": _entry_signal_bars()})
    report = run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
                        state_path=state_path, audit_path=audit_path)
    assert any(e["ticker"] == "SPY" and e["approved"] for e in report["entries"])
    assert broker.submitted == []   # la señal se aprueba y se registra, pero NO se envía


def test_freno_diario_dispara_con_perdida_desde_el_cierre_anterior(tmp_path):
    # El cierre de ayer (persistido) era 10,000; el bróker reporta hoy 9,600: -4%, por debajo
    # del -3% diario (regla del IPS) -> debe pausar compras hasta el próximo lunes.
    state_path, audit_path = _paths(tmp_path)
    prev = AgentState(last_equity=10_000.0, high_water_mark=10_000.0,
                       day_start_equity=10_000.0, week_start_equity=10_000.0)
    save_state(prev, state_path)
    broker = FakeBroker(equity=9_600.0, cash=9_600.0)
    report = run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
                        state_path=state_path, audit_path=audit_path)
    kinds = [ev["kind"] for ev in report["breaker_events"]]
    assert "DAILY_PAUSE" in kinds
    assert report["paused_until"] is not None


# ---------------------------------------------------------------------------- guard de cierre de mercado
def test_run_cycle_rechaza_correr_antes_del_cierre_de_nyse(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker()
    antes_del_cierre = datetime(2024, 12, 17, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    with pytest.raises(RuntimeError, match="cierre de NYSE"):
        run_cycle(today=TODAY, now=antes_del_cierre, execute=False, broker=broker,
                  state_path=state_path, audit_path=audit_path)
    assert not state_path.exists()   # no se tocó nada: falló antes de leer/escribir estado


def test_run_cycle_corre_despues_del_cierre_de_nyse(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker()
    despues_del_cierre = datetime(2024, 12, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    report = run_cycle(today=TODAY, now=despues_del_cierre, execute=False, broker=broker,
                        state_path=state_path, audit_path=audit_path)
    assert report["date"] == TODAY.isoformat()


# ---------------------------------------------------------------------------- flujos (depósitos/retiros)
def test_flow_evita_que_un_retiro_dispare_los_frenos_sin_motivo(tmp_path):
    # Ayer cerró en 10,000. Hoy Darwing retira 3,000 (sin --flow esto se vería como -30%: dispara
    # el freno diario Y el alto de portafolio). Con --flow -3000, el bróker reporta exactamente
    # el retiro (7,000, sin ganancia/pérdida real de mercado) -> ningún freno debe dispararse.
    state_path, audit_path = _paths(tmp_path)
    prev = AgentState(last_equity=10_000.0, high_water_mark=10_000.0,
                       day_start_equity=10_000.0, week_start_equity=10_000.0)
    save_state(prev, state_path)
    broker = FakeBroker(equity=7_000.0, cash=7_000.0)
    report = run_cycle(today=TODAY, execute=False, require_market_closed=False, flow=-3_000.0,
                        broker=broker, state_path=state_path, audit_path=audit_path)
    assert report["breaker_events"] == []
    assert report["flow"] == -3_000.0
    assert report["equity"] == pytest.approx(7_000.0)


# ---------------------------------------------------------------------------- rebalanceo (IPS §8)
def test_rebalance_recorta_proporcionalmente_hasta_el_tope_cuando_supera_60pct():
    from agente.audit import AuditLog
    from agente.risk import PortfolioState, RiskEngine
    from agente.strategy import indicators as _ind

    # Exposición actual: 7,000 de 10,000 (70%) — supera el tope (50%) + 10pp (60%) por 10pp reales.
    row = _ind(_flat_bars(price=700.0)).iloc[-1]
    pf = PortfolioState(equity=10_000.0, cash=1_000.0, positions={"SPY": Position(qty=10.0, last_price=700.0)})
    risk = RiskEngine()
    report = {"rebalance": []}
    audit = AuditLog(_tmp_audit_path())

    _rebalance_if_needed(None, risk, pf, {"SPY": row}, TODAY, False, audit, report)

    assert report["rebalance"] and report["rebalance"][0]["approved"]
    nueva_exposicion = pf.positions["SPY"].qty * pf.positions["SPY"].last_price
    assert nueva_exposicion <= 5_000.0 + 1.0   # de vuelta cerca del tope (50% de 10,000), no por debajo


def test_rebalance_no_hace_nada_si_la_exposicion_no_supera_el_tope_mas_10pp():
    from agente.audit import AuditLog
    from agente.risk import PortfolioState, RiskEngine
    from agente.strategy import indicators as _ind

    # 55% de exposición: supera el tope (50%) pero NO el margen de 10pp (60%) -> no recorta.
    row = _ind(_flat_bars(price=550.0)).iloc[-1]
    pf = PortfolioState(equity=10_000.0, cash=4_500.0, positions={"SPY": Position(qty=10.0, last_price=550.0)})
    risk = RiskEngine()
    report = {"rebalance": []}
    audit = AuditLog(_tmp_audit_path())

    _rebalance_if_needed(None, risk, pf, {"SPY": row}, TODAY, False, audit, report)

    assert report["rebalance"] == []
    assert pf.positions["SPY"].qty == pytest.approx(10.0)


# ---------------------------------------------------------------------------- sleeve conservador (BIL)
def test_sweep_barre_el_efectivo_excedente_a_bil(tmp_path):
    # Sin señales de entrada/salida hoy y mucho efectivo libre -> al final del ciclo se compra
    # BIL con el excedente por encima de CASH_BUFFER.
    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker(equity=10_000.0, cash=10_000.0, bars={"BIL": _flat_bars(price=91.0)})
    report = run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
                        state_path=state_path, audit_path=audit_path)
    sweeps = [s for s in report["sleeve"] if s["action"] == "sweep"]
    assert sweeps and sweeps[0]["approved"]
    assert sweeps[0]["qty"] > 0


def test_defund_vende_bil_para_financiar_una_entrada_sin_efectivo_libre(tmp_path):
    # Casi todo el efectivo ya está en BIL; hoy hay una señal de entrada en SPY -> antes de
    # evaluar la entrada, se debe vender BIL para cubrir el faltante.
    from agente.risk import Position

    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker(
        equity=10_000.0, cash=150.0,
        positions={"BIL": Position(qty=100.0, last_price=91.0)},
        bars={"SPY": _entry_signal_bars(), "BIL": _flat_bars(price=91.0)},
    )
    report = run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
                        state_path=state_path, audit_path=audit_path)
    defunds = [s for s in report["sleeve"] if s["action"] == "defund"]
    assert defunds and defunds[0]["approved"] and defunds[0]["qty"] > 0
    assert any(e["ticker"] == "SPY" and e["approved"] for e in report["entries"])


# ---------------------------------------------------------------------------- liquidez (reglas §7)
def test_liquidity_check_pasa_si_no_hay_columna_volume():
    # Fail-open a propósito (docstring de `_liquidity_check`): sin dato de volumen, nunca bloquea.
    df = _flat_bars()
    assert _liquidity_check(df) == (True, None)


def test_liquidity_check_pasa_con_adv_alto_y_bloquea_con_adv_bajo():
    alto = _flat_bars(price=100.0, volume=500_000.0)   # ADV ≈ US$50M > mínimo (US$10M)
    bajo = _flat_bars(price=100.0, volume=1_000.0)      # ADV ≈ US$100K < mínimo

    pasa, detalle = _liquidity_check(alto)
    assert pasa and detalle is None

    pasa, detalle = _liquidity_check(bajo)
    assert not pasa and "ADV" in detalle and "US$" in detalle


def test_run_cycle_permite_la_entrada_con_liquidez_alta(tmp_path):
    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker(bars={"SPY": _entry_signal_bars(volume=500_000.0)})
    report = run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
                        state_path=state_path, audit_path=audit_path)
    assert any(e["ticker"] == "SPY" and e["approved"] for e in report["entries"])


def test_run_cycle_omite_la_entrada_con_liquidez_baja_y_avisa(tmp_path):
    state_path, audit_path = _paths(tmp_path)
    broker = FakeBroker(bars={"SPY": _entry_signal_bars(volume=1_000.0)})
    report = run_cycle(today=TODAY, execute=False, require_market_closed=False, broker=broker,
                        state_path=state_path, audit_path=audit_path)
    assert not any(e["ticker"] == "SPY" for e in report["entries"])
    assert any("SPY" in w and "ADV" in w for w in report["warnings"])


# ---------------------------------------------------------------------------- tope de dimensionamiento
def test_entry_proposal_usa_el_tope_de_dimensionamiento_si_es_menor_que_el_equity_real():
    from agente.strategy import EntrySignal

    strat = Strategy()
    sig = EntrySignal(ticker="SPY", ref_close=100.0, atr=2.0, stop=97.0)
    sin_tope = strat.entry_proposal(sig, equity_now=1_000_000.0)
    con_tope = strat.entry_proposal(sig, equity_now=1_000_000.0, sizing_cap=10_000.0)
    assert con_tope.qty < sin_tope.qty
    assert con_tope.qty == pytest.approx(strat.entry_proposal(sig, equity_now=10_000.0).qty)


# ---------------------------------------------------------------------------- arbitraje de cupos
def test_arbitraje_de_cupos_prefiere_el_orden_de_la_lista_y_no_reasigna_el_cupo_rechazado():
    from agente.audit import AuditLog
    from agente.risk import PortfolioState, RiskEngine

    row = _entry_signal_bars().pipe(lambda df: __import__("agente.strategy", fromlist=["indicators"]).indicators(df)).iloc[-1]
    rows = {"SPY": row, "VTI": row, "QQQ": row}   # las tres tickers, mismo patrón de señal
    memory = {t: TickerMemory() for t in EQUITY_TICKERS_ORDER}

    risk = RiskEngine()
    strat = Strategy()
    pf = PortfolioState(equity=1_000_000.0, cash=1_000_000.0,
                         high_water_mark=1_000_000.0, day_start_equity=1_000_000.0,
                         week_start_equity=1_000_000.0)
    # 6 cupos ya "ocupados" por propuestas pendientes de otros tickers (no importa cuáles) para
    # dejar exactamente 2 cupos libres, con 3 candidatos disponibles (SPY, VTI, QQQ).
    state = AgentState(pending_entries={f"FAKE{i}": {"stop": 1.0, "atr_at_entry": 1.0,
                                                       "submitted_date": TODAY.isoformat()}
                                         for i in range(6)})
    audit = AuditLog(_tmp_audit_path())
    report = {"entries": [], "rejected": [], "exits": [], "warnings": [], "resident_stops": []}

    _process_entries(None, risk, strat, pf, {}, memory, state, rows, TODAY, False, audit, report)

    tickers_entered = [e["ticker"] for e in report["entries"]]
    assert tickers_entered == ["SPY", "VTI"]      # QQQ nunca se evalúa: se quedó sin cupo
    assert all(e["approved"] for e in report["entries"])
    assert "QQQ" not in state.pending_entries


def _tmp_audit_path():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp()) / "audit.jsonl"


# ---------------------------------------------------------------------------- reconciliación
def test_reconcile_confirma_una_entrada_llenada_con_el_precio_promedio_del_broker():
    from agente.audit import AuditLog

    state = AgentState(pending_entries={"SPY": {"stop": 95.0, "atr_at_entry": 2.0,
                                                 "submitted_date": "2024-12-16"}})
    broker = FakeBroker(positions={"SPY": Position(qty=10.0, last_price=101.0)},
                         avg_costs={"SPY": 100.5})
    from agente.risk import PortfolioState
    pf = PortfolioState(equity=100_000.0, cash=90_000.0, positions=dict(broker.positions))
    open_positions: dict = {}
    memory = {t: TickerMemory() for t in EQUITY_TICKERS_ORDER}
    strat = Strategy()
    report = {"warnings": []}
    audit = AuditLog(_tmp_audit_path())

    _reconcile(broker, pf, state, open_positions, memory, strat, TODAY, audit, report)

    assert "SPY" not in state.pending_entries
    assert open_positions["SPY"].entry == pytest.approx(100.5)
    assert open_positions["SPY"].stop == pytest.approx(95.0)


def test_reconcile_detecta_salida_llenada_y_activa_cooldown_si_fue_por_stop():
    from agente.audit import AuditLog
    from agente.risk import PortfolioState

    state = AgentState(pending_exits={"SPY": {"reason": "stop_trail", "submitted_date": "2024-12-16"}})
    pos = OpenPosition(entry=100.0, stop=105.0, hi_close=110.0, atr_at_entry=2.0, entry_date=date(2024, 12, 1))
    open_positions = {"SPY": pos}
    memory = {t: TickerMemory() for t in EQUITY_TICKERS_ORDER}
    broker = FakeBroker(positions={})   # el bróker ya no tiene SPY: la venta se llenó
    pf = PortfolioState(equity=100_000.0, cash=100_000.0, positions={})
    strat = Strategy()
    report = {"warnings": []}
    audit = AuditLog(_tmp_audit_path())

    _reconcile(broker, pf, state, open_positions, memory, strat, TODAY, audit, report)

    assert "SPY" not in open_positions
    assert "SPY" not in state.pending_exits
    assert memory["SPY"].need_new_cross is True
    assert memory["SPY"].last_stop_exit == TODAY


def test_reconcile_avisa_de_una_posicion_no_reconocida_sin_gestionarla():
    from agente.audit import AuditLog
    from agente.risk import PortfolioState

    state = AgentState()
    open_positions: dict = {}
    memory = {t: TickerMemory() for t in EQUITY_TICKERS_ORDER}
    broker = FakeBroker(positions={"QQQ": Position(qty=5.0, last_price=400.0)})
    pf = PortfolioState(equity=100_000.0, cash=100_000.0, positions=dict(broker.positions))
    strat = Strategy()
    report = {"warnings": []}
    audit = AuditLog(_tmp_audit_path())

    _reconcile(broker, pf, state, open_positions, memory, strat, TODAY, audit, report)

    assert "QQQ" not in open_positions   # no se gestiona sin poder reconstruir su stop
    assert any("QQQ" in w for w in report["warnings"])


def test_reconcile_infiere_salida_por_stop_residente_cuando_no_habia_salida_pendiente():
    from agente.audit import AuditLog
    from agente.risk import PortfolioState

    # stop actual (105) por encima del inicial (100 - 1.5*2 = 97) -> se infiere trailing
    pos = OpenPosition(entry=100.0, stop=105.0, hi_close=110.0, atr_at_entry=2.0, entry_date=date(2024, 12, 1))
    open_positions = {"SPY": pos}
    state = AgentState()   # sin pending_exits registrado: nadie envió la venta desde el ciclo
    memory = {t: TickerMemory() for t in EQUITY_TICKERS_ORDER}
    broker = FakeBroker(positions={})   # pero el bróker ya no tiene la posición
    pf = PortfolioState(equity=100_000.0, cash=100_000.0, positions={})
    strat = Strategy()
    report = {"warnings": []}
    audit = AuditLog(_tmp_audit_path())

    _reconcile(broker, pf, state, open_positions, memory, strat, TODAY, audit, report)

    assert "SPY" not in open_positions
    assert memory["SPY"].need_new_cross is True   # se trató como stop, no como salida silenciosa
    assert any("SPY" in w for w in report["warnings"])   # pero se avisa: es una inferencia


# ---------------------------------------------------------------------------- salidas
def test_process_exits_envia_salida_de_regimen_y_no_toca_el_stop_residente():
    from agente.audit import AuditLog
    from agente.risk import PortfolioState
    from agente.strategy import indicators as _ind

    row = _ind(_regime_exit_bars()).iloc[-1]
    pos = OpenPosition(entry=90.0, stop=85.0, hi_close=100.0, atr_at_entry=2.0, entry_date=date(2024, 12, 1))
    open_positions = {"SPY": pos}
    rows = {"SPY": row}
    pf = PortfolioState(equity=100_000.0, cash=10_000.0, positions={"SPY": Position(qty=10.0, last_price=80.0)},
                         high_water_mark=100_000.0, day_start_equity=100_000.0, week_start_equity=100_000.0)
    from agente.risk import RiskEngine
    risk = RiskEngine()
    strat = Strategy()
    state = AgentState()
    report = {"exits": [], "rejected": [], "resident_stops": [], "warnings": []}
    audit = AuditLog(_tmp_audit_path())

    _process_exits(None, risk, strat, pf, open_positions, rows, state, TODAY, False, audit, report)

    assert report["exits"][0]["reason"] == "regime" and report["exits"][0]["approved"]
    assert "SPY" in state.pending_exits
    assert "SPY" not in pf.positions   # ya no cuenta como exposición para entradas del mismo ciclo
