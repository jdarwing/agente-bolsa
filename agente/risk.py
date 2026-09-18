"""
Capa de riesgo — la frontera dura del agente.

Reglas de diseño (plan de construcción, "Arquitectura" y "Compuertas de seguridad"):
- Determinística: sin LLM, sin red, sin estado oculto. Misma entrada → misma decisión.
- Toda orden pasa por `RiskEngine.evaluate()` antes de tocar el bróker. La capa de razonamiento
  *propone*; esta capa *autoriza o rechaza*, y explica por qué.
- Los frenos (circuit breakers) viven aquí: -10% desde máximo histórico (alto total, reactivación
  manual), -3% diario / -5% semanal (pausa de compras), y el interruptor de emergencia (kill switch).
- Cada evaluación se registra en la bitácora de auditoría, aprobada o no.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .config import LIMITS, WHITELIST, EQUITY_TICKERS, CONSERVATIVE_TICKERS, RiskLimits

Side = Literal["BUY", "SELL"]
OrderKind = Literal["LMT", "STP LMT"]


@dataclass(frozen=True)
class OrderProposal:
    """Lo que la capa de razonamiento pide. Nunca se envía directo al bróker."""
    ticker: str
    side: Side
    qty: float
    limit_price: float
    ref_close: float                 # cierre de referencia con el que se calculó la señal
    kind: OrderKind = "LMT"
    stop_price: float | None = None  # obligatorio en compras (stop inicial); nivel del stop en STP LMT
    reason: str = ""                 # "entry", "regime_exit", "stop_init", "stop_trail", "rebalance", "sleeve"


@dataclass
class Position:
    qty: float
    last_price: float

    @property
    def notional(self) -> float:
        return self.qty * self.last_price


@dataclass
class PortfolioState:
    """Foto del portafolio que la capa de riesgo necesita. La construye la capa de ejecución
    a partir del bróker; la capa de riesgo no consulta nada por su cuenta."""
    equity: float                                   # valor total de la cuenta (NAV)
    cash: float                                     # efectivo disponible para comprar
    positions: dict[str, Position] = field(default_factory=dict)
    traded_today: dict[str, set[str]] = field(default_factory=dict)  # ticker -> {"BUY","SELL"}
    # Frenos
    high_water_mark: float = 0.0
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    halted: bool = False            # circuit breaker de portafolio (-10%) — solo se libera a mano
    paused_until: date | None = None  # breaker diario/semanal — pausa de compras
    killed: bool = False            # interruptor de emergencia — todo bloqueado

    def equity_exposure(self) -> float:
        return sum(p.notional for t, p in self.positions.items() if t in EQUITY_TICKERS)

    def equity_position_count(self) -> int:
        return sum(1 for t, p in self.positions.items() if t in EQUITY_TICKERS and p.qty > 0)


@dataclass(frozen=True)
class Decision:
    approved: bool
    reasons: tuple[str, ...]        # motivos de rechazo (vacío si aprobada)
    checks: tuple[str, ...]         # verificaciones que pasó (para auditoría)

    def __bool__(self) -> bool:
        return self.approved


@dataclass(frozen=True)
class BreakerEvent:
    kind: str                       # "PORTFOLIO_HALT" | "DAILY_PAUSE" | "WEEKLY_PAUSE" | "HWM_RESET" | "REACTIVATED"
    detail: str
    equity: float


class RiskEngine:
    def __init__(self, limits: RiskLimits = LIMITS, whitelist: frozenset[str] = WHITELIST):
        self.limits = limits
        self.whitelist = whitelist

    # ------------------------------------------------------------------ órdenes
    def evaluate(self, p: OrderProposal, s: PortfolioState, today: date) -> Decision:
        L = self.limits
        reasons: list[str] = []
        checks: list[str] = []

        def fail(msg: str) -> None:
            reasons.append(msg)

        def ok(msg: str) -> None:
            checks.append(msg)

        # 0. Interruptor de emergencia: nada pasa.
        if s.killed:
            return Decision(False, ("KILL_SWITCH: agente detenido por Darwing; ninguna orden se envía",), ())
        ok("kill_switch_off")

        # 1. Sanidad numérica.
        for name, v in (("qty", p.qty), ("limit_price", p.limit_price), ("ref_close", p.ref_close)):
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                fail(f"INVALID_{name.upper()}: {v!r}")
        if reasons:
            return Decision(False, tuple(reasons), tuple(checks))
        ok("numbers_sane")

        # 2. Lista blanca — la única defensa contra el permiso de Forex que IBKR incluye por defecto.
        t = p.ticker.upper()
        if t not in self.whitelist:
            fail(f"NOT_WHITELISTED: {t} no está en la lista de instrumentos permitidos")
            return Decision(False, tuple(reasons), tuple(checks))
        ok("whitelisted")
        is_equity = t in EQUITY_TICKERS

        # 3. Solo largo: una venta nunca supera lo que se tiene.
        held = s.positions.get(t, Position(0.0, 0.0)).qty
        if p.side == "SELL":
            if held <= 0:
                fail(f"NO_SHORTS: no hay posición en {t} para vender")
            elif p.qty > held * (1 + 1e-9):
                fail(f"NO_SHORTS: venta de {p.qty} supera la posición de {held} en {t}")
            else:
                ok("long_only")
        elif p.side != "BUY":
            fail(f"INVALID_SIDE: {p.side}")
        else:
            ok("long_only")

        # 4. Sin ida y vuelta en la misma sesión (regla 10).
        opposite = "SELL" if p.side == "BUY" else "BUY"
        if opposite in s.traded_today.get(t, set()):
            fail(f"SAME_DAY_ROUND_TRIP: ya hubo {opposite} de {t} hoy")
        else:
            ok("no_same_day_round_trip")

        # 5. Tipo de orden y límite ejecutable (regla 6).
        if p.kind == "LMT":
            if p.side == "BUY" and p.limit_price > p.ref_close * (1 + L.limit_band) + 1e-9:
                fail(f"LIMIT_TOO_HIGH: {p.limit_price:.4f} > cierre {p.ref_close:.4f} +{L.limit_band:.1%}")
            elif p.side == "SELL" and p.limit_price < p.ref_close * (1 - L.limit_band) - 1e-9:
                fail(f"LIMIT_TOO_LOW: {p.limit_price:.4f} < cierre {p.ref_close:.4f} -{L.limit_band:.1%}")
            else:
                ok("limit_within_band")
        elif p.kind == "STP LMT":
            # Solo para salidas de protección residentes: SELL con stop y límite ≥ stop·(1-gap).
            if p.side != "SELL":
                fail("STP_LMT_ONLY_FOR_SELL")
            elif p.stop_price is None or p.stop_price <= 0:
                fail("STP_LMT_NEEDS_STOP")
            elif p.limit_price < p.stop_price * (1 - L.stop_limit_gap) - 1e-9:
                fail(f"STP_LMT_LIMIT_TOO_LOW: límite {p.limit_price:.4f} < stop {p.stop_price:.4f} -{L.stop_limit_gap:.0%}")
            else:
                ok("stop_limit_ok")
        else:
            fail(f"ORDER_KIND_NOT_ALLOWED: {p.kind} (solo LMT y STP LMT; nunca MKT)")

        # 6. Frenos activos: bloquean compras, nunca salidas de protección.
        if p.side == "BUY":
            if s.halted:
                fail("PORTFOLIO_HALTED: drawdown ≤ -10% desde máximo; requiere reactivación manual")
            elif s.paused_until is not None and today < s.paused_until:
                fail(f"PAUSED_UNTIL_{s.paused_until.isoformat()}: breaker diario/semanal activo")
            else:
                ok("breakers_clear")

        # 7. Reglas de compra: stop, riesgo, tope nominal, tope de exposición, número de posiciones, efectivo.
        if p.side == "BUY":
            notional = p.qty * p.limit_price
            if notional < L.min_order_notional:
                fail(f"ORDER_TOO_SMALL: {notional:.2f} < {L.min_order_notional:.2f}")

            if is_equity:
                if p.stop_price is None:
                    fail("STOP_REQUIRED: toda compra de renta variable lleva stop definido antes de entrar")
                elif not (0 < p.stop_price < p.limit_price):
                    fail(f"STOP_INVALID: stop {p.stop_price} debe estar por debajo del precio {p.limit_price}")
                else:
                    risk_amt = (p.limit_price - p.stop_price) * p.qty
                    if risk_amt > L.risk_pct_max * s.equity + 1e-9:
                        fail(f"RISK_TOO_HIGH: {risk_amt:.2f} > {L.risk_pct_max:.0%} de {s.equity:.2f}")
                    else:
                        ok(f"risk_ok:{risk_amt / s.equity:.3%}")

                total_in_ticker = notional + s.positions.get(t, Position(0, 0)).notional
                if total_in_ticker > L.max_notional_pct * s.equity + 1e-9:
                    fail(f"POSITION_CAP: {total_in_ticker:.2f} en {t} > {L.max_notional_pct:.0%} del portafolio")
                else:
                    ok("position_cap_ok")

                if s.equity_exposure() + notional > L.equity_cap_pct * s.equity + 1e-9:
                    fail(f"EQUITY_CAP: exposición {s.equity_exposure() + notional:.2f} > {L.equity_cap_pct:.0%} del portafolio")
                else:
                    ok("equity_cap_ok")

                new_position = held <= 0
                if new_position and s.equity_position_count() + 1 > L.max_positions:
                    fail(f"MAX_POSITIONS: ya hay {s.equity_position_count()} posiciones (máx {L.max_positions})")
                else:
                    ok("max_positions_ok")

            commission = max(L.comm_min, L.comm_per_share * p.qty)
            if notional + commission > s.cash + 1e-9:
                fail(f"INSUFFICIENT_CASH: necesita {notional + commission:.2f}, hay {s.cash:.2f}")
            else:
                ok("cash_ok")

        approved = not reasons
        if approved:
            # Regla 4 (arriba) depende de que esto quede registrado — sin esto, `traded_today`
            # nunca se llenaba y la prohibición de ida-y-vuelta el mismo día no se verificaba de
            # verdad por código (pendiente encontrado en la evaluación de avance, 18-sep-2026).
            # `roll_day()` lo limpia al empezar la sesión siguiente.
            s.traded_today.setdefault(t, set()).add(p.side)
        return Decision(approved, tuple(reasons), tuple(checks))

    # ------------------------------------------------------------------ frenos
    def update_marks(self, s: PortfolioState, equity_now: float, today: date) -> list[BreakerEvent]:
        """Llamar una vez por cierre con el NAV del día. Actualiza máximo histórico y dispara frenos.
        Los depósitos/retiros se registran ANTES con `record_flow` para no contaminar el drawdown."""
        L = self.limits
        events: list[BreakerEvent] = []
        if s.high_water_mark <= 0:
            s.high_water_mark = equity_now
        if s.day_start_equity <= 0:
            s.day_start_equity = equity_now
        if s.week_start_equity <= 0:
            s.week_start_equity = equity_now

        s.equity = equity_now
        if not s.halted:
            s.high_water_mark = max(s.high_water_mark, equity_now)

        dd = equity_now / s.high_water_mark - 1 if s.high_water_mark > 0 else 0.0
        if dd <= -L.cb_portfolio_dd and not s.halted:
            s.halted = True
            events.append(BreakerEvent("PORTFOLIO_HALT", f"drawdown {dd:.2%} desde máximo {s.high_water_mark:.2f}", equity_now))

        daily = equity_now / s.day_start_equity - 1 if s.day_start_equity > 0 else 0.0
        weekly = equity_now / s.week_start_equity - 1 if s.week_start_equity > 0 else 0.0
        if daily <= -L.cb_daily and (s.paused_until is None or today >= s.paused_until):
            s.paused_until = _next_monday(today)
            events.append(BreakerEvent("DAILY_PAUSE", f"caída diaria {daily:.2%}; compras pausadas hasta {s.paused_until}", equity_now))
        elif weekly <= -L.cb_weekly and (s.paused_until is None or today >= s.paused_until):
            s.paused_until = _next_monday(today)
            events.append(BreakerEvent("WEEKLY_PAUSE", f"caída semanal {weekly:.2%}; compras pausadas hasta {s.paused_until}", equity_now))
        return events

    def roll_day(self, s: PortfolioState, today: date) -> None:
        """Al inicio de cada sesión: fija las referencias diaria/semanal y limpia el registro del día."""
        s.day_start_equity = s.equity
        if today.weekday() == 0 or s.week_start_equity <= 0:
            s.week_start_equity = s.equity
        s.traded_today = {}

    def record_flow(self, s: PortfolioState, amount: float) -> None:
        """Depósito (+) o retiro (−). Ajusta las referencias para que un flujo no parezca ganancia/pérdida."""
        s.equity += amount
        s.cash += amount
        s.high_water_mark += amount
        s.day_start_equity += amount
        s.week_start_equity += amount

    def reactivate(self, s: PortfolioState, note: str) -> BreakerEvent:
        """Reactivación MANUAL tras un alto de portafolio. Reinicia el máximo histórico al valor actual
        (IPS 7.1). Exige una nota escrita de qué falló."""
        if not note or len(note.strip()) < 10:
            raise ValueError("La reactivación requiere una nota de revisión (≥10 caracteres).")
        s.halted = False
        s.high_water_mark = s.equity
        return BreakerEvent("REACTIVATED", note.strip(), s.equity)

    def kill(self, s: PortfolioState) -> BreakerEvent:
        s.killed = True
        return BreakerEvent("KILL_SWITCH", "interruptor de emergencia activado", s.equity)

    def unkill(self, s: PortfolioState, note: str) -> BreakerEvent:
        if not note or len(note.strip()) < 10:
            raise ValueError("Quitar el kill switch requiere una nota (≥10 caracteres).")
        s.killed = False
        return BreakerEvent("KILL_SWITCH_OFF", note.strip(), s.equity)


def _next_monday(d: date) -> date:
    from datetime import timedelta
    days = (7 - d.weekday()) % 7 or 7
    return d + timedelta(days=days)
