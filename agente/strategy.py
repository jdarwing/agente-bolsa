"""
Capa de estrategia — señales de entrada/salida (Fase 1, `reglas-entrada-salida-estrategia.md`).

Traduce las reglas de la Fase 1 en *propuestas* de orden (`risk.OrderProposal`). Esta capa no
decide si una propuesta se ejecuta — eso es trabajo exclusivo de `risk.RiskEngine.evaluate()`.
Aquí solo viven las reglas TÉCNICAS de la señal (régimen, cruce EMA, ATR, stops); los límites
de portafolio (tope por posición, tope de renta variable, número de posiciones, efectivo) NO
se duplican — se delegan a `risk.py`, que es la única fuente de verdad para esos números. El
único número de riesgo que se usa aquí (`LIMITS.risk_pct_max`, `LIMITS.max_notional_pct`) se
importa de `config.py`, nunca se repite a mano.

Los indicadores están portados de `backtest/engine.py::indicators()` — mismas fórmulas, mismo
orden de cálculo. Se verificó manualmente que producen los mismos valores sobre los datos reales
de Fase 1 (SPY, 2016-2026) antes de escribir este módulo.

Nota de calibración — cooldown de re-entrada (decisión de Darwing, 18-sep-2026): el motor de
backtest que Darwing aprobó el 15-sep-2026 (`backtest/engine.py`) cuenta el cooldown de 10 días
en **calendario**, mientras que `reglas-entrada-salida-estrategia.md` dice "10 días hábiles".
Confirmado: este módulo usa **días hábiles**, tal como dice el documento — no la cifra literal
del backtest. Consecuencia conocida y aceptada: la re-entrada en producción puede disparar 2-4
días calendario más tarde que en la simulación de Fase 1 (10 hábiles ≈ 14 calendario); el
backtest no se volvió a correr con esta variante exacta. Esto queda como una diferencia menor
a favor de la regla escrita, a resolver — si hiciera falta — con la validación fuera de muestra
de la Fase 4 (6 semanas de paper trading), no antes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from .config import LIMITS, RiskLimits
from .risk import OrderProposal

ExitReason = Literal["regime", "stop_init", "stop_trail"]


@dataclass(frozen=True)
class StrategyParams:
    """Parámetros técnicos aprobados en Fase 1 (15-sep-2026, ver reglas §2-5). Cambiar un número
    aquí es cambiar una regla de `reglas-entrada-salida-estrategia.md` — solo con revisión
    documentada allí también."""
    sma_regime: int = 200
    ema_fast: int = 20
    ema_slow: int = 50
    confirm_closes: int = 2          # cierres consecutivos de EMA20>EMA50 para confirmar la entrada (§3)
    atr_len: int = 14
    k_init: float = 1.5              # stop inicial: entrada − k_init·ATR (§4.2)
    k_trail: float = 5.0             # trailing: máximo de cierre − k_trail·ATR, calibrado 15-sep (§4.3)
    trail_arm_atr: float = 1.0       # el trailing se activa cuando la ganancia supera 1·ATR (§4.3)
    reentry_cooldown_days: int = 10  # días hábiles tras un stop antes de poder re-entrar (§3)


PARAMS = StrategyParams()


def indicators(df: pd.DataFrame, p: StrategyParams = PARAMS) -> pd.DataFrame:
    """SMA200, EMA20/50, ATR14, régimen alcista (EMA20>EMA50) y su racha de cierres consecutivos.
    `df` debe tener columnas open/high/low/close, indexado por fecha, orden ascendente."""
    d = df.copy()
    d["sma"] = d["close"].rolling(p.sma_regime).mean()
    d["ema_f"] = d["close"].ewm(span=p.ema_fast, adjust=False).mean()
    d["ema_s"] = d["close"].ewm(span=p.ema_slow, adjust=False).mean()
    hl = d["high"] - d["low"]
    hc = (d["high"] - d["close"].shift()).abs()
    lc = (d["low"] - d["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / p.atr_len, adjust=False).mean()
    d["bull"] = d["ema_f"] > d["ema_s"]
    grp = (d["bull"] != d["bull"].shift()).cumsum()
    d["bull_run"] = d.groupby(grp).cumcount() + 1
    d.loc[~d["bull"], "bull_run"] = 0
    return d


@dataclass
class OpenPosition:
    """Lo que la ESTRATEGIA necesita recordar de una posición abierta — más de lo que guarda
    `risk.Position` (qty, last_price), que solo sirve para los topes de la capa de riesgo.
    La mantiene el ciclo diario (`runner.py`, aún no construido), no esta clase."""
    entry: float
    stop: float
    hi_close: float
    atr_at_entry: float
    entry_date: date


@dataclass
class TickerMemory:
    """Estado de re-entrada por ticker (§3): tras un stop hay que esperar el cooldown; una
    salida de régimen no fuerza cooldown (el propio cruce EMA ya lo exige en la práctica —
    así se comporta también el motor de backtest aprobado)."""
    need_new_cross: bool = False
    last_stop_exit: date | None = None


@dataclass(frozen=True)
class ExitSignal:
    ticker: str
    reason: ExitReason
    ref_close: float
    new_stop: float | None = None    # nivel vigente del stop, para refrescar el STP LMT residente


@dataclass(frozen=True)
class EntrySignal:
    ticker: str
    ref_close: float
    atr: float
    stop: float


class Strategy:
    def __init__(self, params: StrategyParams = PARAMS, limits: RiskLimits = LIMITS):
        self.p = params
        self.limits = limits

    # ---------------------------------------------------------------- salidas (§4)
    def check_exit(self, ticker: str, row: pd.Series, pos: OpenPosition) -> ExitSignal | None:
        """Evalúa las 3 salidas de §4, en orden: régimen, stop inicial, trailing. `row` es la
        fila de hoy ya con indicadores (salida de `indicators()`). Actualiza `pos` en el sitio:
        el stop SOLO sube, nunca baja."""
        close, atr, sma = float(row["close"]), float(row["atr"]), float(row["sma"])
        pos.hi_close = max(pos.hi_close, close)
        if close - pos.entry > self.p.trail_arm_atr * pos.atr_at_entry:
            pos.stop = max(pos.stop, pos.hi_close - self.p.k_trail * atr)
        if close < sma:
            return ExitSignal(ticker, "regime", close, pos.stop)
        if close <= pos.stop:
            initial_stop = pos.entry - self.p.k_init * pos.atr_at_entry
            is_trail = pos.stop > initial_stop + 1e-9
            return ExitSignal(ticker, "stop_trail" if is_trail else "stop_init", close, pos.stop)
        return None

    # ---------------------------------------------------------------- entradas (§2-3)
    def check_entry(self, ticker: str, row: pd.Series, mem: TickerMemory, today: date) -> EntrySignal | None:
        """Régimen + cruce EMA confirmado. No sabe nada de cupos, exposición ni efectivo — eso
        lo decide `risk.py` cuando se le presenta la propuesta."""
        if bool(np.isnan(row["sma"])):
            return None
        if not bool(row["bull"]):
            mem.need_new_cross = False   # cayó bajo: el próximo cruce alcista vuelve a ser "nuevo"
            return None
        if mem.need_new_cross:
            if mem.last_stop_exit is not None and _business_days_between(mem.last_stop_exit, today) >= self.p.reentry_cooldown_days:
                mem.need_new_cross = False
            else:
                return None
        bull_run = int(row["bull_run"])
        ok_conf = (bull_run == self.p.confirm_closes) or (mem.last_stop_exit is not None and bull_run >= self.p.confirm_closes)
        if not (float(row["close"]) > float(row["sma"]) and ok_conf):
            return None
        close, atr = float(row["close"]), float(row["atr"])
        return EntrySignal(ticker, close, atr, close - self.p.k_init * atr)

    def on_exit_filled(self, mem: TickerMemory, reason: ExitReason, exit_date: date) -> None:
        """Avisar a la memoria del ticker después de que una salida se ejecutó (§3). Solo los
        stops activan el cooldown; una salida de régimen no lo hace (igual que en el backtest
        aprobado: el próximo cruce EMA ya es, por definición, un cruce nuevo)."""
        if reason in ("stop_init", "stop_trail"):
            mem.need_new_cross = True
            mem.last_stop_exit = exit_date

    # ---------------------------------------------------------------- propuestas de orden
    def entry_proposal(self, sig: EntrySignal, equity_now: float, sizing_cap: float | None = None) -> OrderProposal:
        """Tamaño = menor entre riesgo (techo `risk_pct_max`, §5) y tope nominal (§5) — el tope
        nominal manda casi siempre con este universo (confirmado en el backtest). Los demás
        topes de portafolio (exposición 50%, máx. posiciones, efectivo) los aplica `risk.py`.

        `sizing_cap` (evaluación de avance 18-sep-2026, brecha 2): mientras la cuenta paper tenga
        un saldo (US$1,000,000) muy por encima del capital real de la Etapa 1 (IPS §2, <US$10,000),
        el tamaño se calcula sobre el MENOR entre `equity_now` y este tope — para que las órdenes
        de prueba tengan un tamaño realista en dólares. Los frenos de `risk.py` siguen evaluando
        contra el equity real (`equity_now`), sin este tope; solo afecta cuánto comprar."""
        # Se dimensiona sobre el precio LÍMITE (cierre + banda), no sobre el cierre crudo: si se
        # usara el cierre, la banda de ±0.5% podía empujar el nocional real un pelo por encima
        # del tope al momento de ejecutar — la capa de riesgo lo detectaría y rechazaría la
        # propuesta por algo que no es un error de la señal, solo de aritmética de tamaño.
        L = self.limits
        equity_for_size = min(equity_now, sizing_cap) if sizing_cap else equity_now
        limit_price = round(sig.ref_close * (1 + L.limit_band), 4)
        risk_qty = (L.risk_pct_max * equity_for_size) / max(limit_price - sig.stop, 1e-9)
        notional_qty = (L.max_notional_pct * equity_for_size) / limit_price
        qty = min(risk_qty, notional_qty)
        return OrderProposal(ticker=sig.ticker, side="BUY", qty=qty, limit_price=limit_price,
                              ref_close=sig.ref_close, kind="LMT", stop_price=round(sig.stop, 4),
                              reason="entry")

    def exit_proposal(self, sig: ExitSignal, qty: float) -> OrderProposal:
        """Salida a mercado límite ejecutable (§6): −0.5% bajo el último cierre, DAY."""
        limit_price = round(sig.ref_close * (1 - self.limits.limit_band), 4)
        return OrderProposal(ticker=sig.ticker, side="SELL", qty=qty, limit_price=limit_price,
                              ref_close=sig.ref_close, kind="LMT", reason=sig.reason)

    def resident_stop_proposal(self, ticker: str, qty: float, stop: float, last_close: float) -> OrderProposal:
        """Orden stop-límite residente (§6): límite `stop_limit_gap` (1%) bajo el stop. Se
        refresca cada cierre cuando el trailing sube — el bróker reemplaza la orden vigente,
        nunca se acumulan dos. `last_close` es solo el precio de referencia para la bitácora."""
        limit_price = round(stop * (1 - self.limits.stop_limit_gap), 4)
        return OrderProposal(ticker=ticker, side="SELL", qty=qty, limit_price=limit_price,
                              ref_close=last_close, kind="STP LMT", stop_price=round(stop, 4),
                              reason="stop_resident")


def _business_days_between(start: date, end: date) -> int:
    """Días hábiles (lun-vie, sin festivos) entre dos fechas — aproximación suficiente para el
    cooldown de §3; no cuenta el propio `start`. Ver la nota de paridad al inicio del archivo."""
    if end <= start:
        return 0
    return int(np.busday_count(start, end))
