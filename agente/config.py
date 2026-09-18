"""
Configuración del agente — Etapa 1.
Todos los valores salen de `ips-personal-agente-bolsa.md` y `reglas-entrada-salida-estrategia.md`.
Cambiar un número aquí es cambiar una regla del IPS: hacerlo solo con la revisión documentada.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


# --- Universo (lista blanca) -------------------------------------------------
# Renta variable: 7 ETFs validados en el backtest de Fase 1. EQUITY_TICKERS_ORDER es la misma
# lista pero en el orden con el que corrió `backtest/engine.py` — importa para `runner.py`:
# cuando hay más señales de compra que cupos libres, gana la que aparece primero en este orden
# (hallazgo documentado 18-sep-2026 en el plan de construcción). EQUITY_TICKERS (frozenset) no
# garantiza orden — se usa donde solo importa pertenencia, nunca para arbitrar cupos.
EQUITY_TICKERS_ORDER: tuple[str, ...] = ("SPY", "VTI", "QQQ", "EFA", "VWO", "XLK", "XLF")
EQUITY_TICKERS: frozenset[str] = frozenset(EQUITY_TICKERS_ORDER)
# Sleeve conservador: ETF de T-bills. Exento del tope por posición y del tope de renta variable,
# pero igual debe estar en la lista blanca.
CONSERVATIVE_TICKERS: frozenset[str] = frozenset({"BIL", "SGOV", "SHV"})
WHITELIST: frozenset[str] = EQUITY_TICKERS | CONSERVATIVE_TICKERS

# ETF del sleeve que `runner.py` usa activamente (IPS §5/§6/§8, reglas §7): destino por defecto
# del efectivo que no está en una posición de renta variable — nunca efectivo ocioso, porque
# IBKR no paga interés sobre los primeros US$10,000 (hallazgo del checklist de apertura). BIL es
# el más líquido de los tres CONSERVATIVE_TICKERS; SGOV/SHV quedan como alternativa manual, no
# los usa el código todavía.
SLEEVE_TICKER: str = "BIL"

# --- Escala de la corrida (evaluación de avance 18-sep-2026, brecha 2) -------
# La cuenta PAPER quedó con US$1,000,000 (valor de fábrica de IBKR), muy por encima del capital
# real de la Etapa 1 (IPS §2: menos de US$10,000). Sin este tope, cada posición de prueba saldría
# de ~US$100,000 — un tamaño que nunca se parecería al real (mínimos de orden, peso de la comisión
# mínima, fracciones). `strategy.entry_proposal` usa el MENOR entre el equity real y este tope
# solo para calcular CUÁNTO comprar — los frenos de `risk.py` (drawdown, exposición, etc.) siguen
# evaluando contra el equity real de la cuenta, sin este tope. Poner en None lo desactiva (para
# cuando el capital real ya esté cerca de o por encima de este número).
SIZING_EQUITY_CAP: float | None = 10_000.0

# Reserva mínima de efectivo que `runner.py` nunca barre al sleeve — para comisiones y para no
# dejar la cuenta en cero exacto por un redondeo. No es una regla del IPS, es un margen operativo.
CASH_BUFFER: float = 100.0

# --- Estado y bitácora del ciclo diario (runner.py) --------------------------
# A propósito FUERA de la carpeta de OneDrive (`Agente de Bolsa/agente/`, ver README.md):
# ese archivo cambia todos los días (a veces varias veces al día) y es el que más chocaría con
# la sincronización de OneDrive que ya revirtió un archivo sin avisar (18-sep-2026) — ahí sería
# grave (un stop vigente desactualizado), no solo un cambio de código a re-verificar. Vive en el
# home de quien corra el agente (la Mac de Darwing en Fases 2-3; el VPS desde la Fase 4).
_HOME_AGENTE = Path.home() / ".agente_bolsa"
STATE_PATH: Path = _HOME_AGENTE / "state.json"
AUDIT_LOG_PATH: Path = _HOME_AGENTE / "audit.jsonl"

@dataclass(frozen=True)
class RiskLimits:
    """Límites duros de la capa de riesgo (IPS secciones 4-8; reglas secciones 5-7, 10)."""
    risk_pct_max: float = 0.02          # riesgo máximo por operación: 2% (techo del rango 1-2%)
    max_notional_pct: float = 0.10      # tope nominal por posición: ~10% del portafolio
    equity_cap_pct: float = 0.50        # tope de exposición total a renta variable
    max_positions: int = 8              # posiciones simultáneas en renta variable
    limit_band: float = 0.005           # ±0.5%: límite "ejecutable" respecto al cierre de referencia
    stop_limit_gap: float = 0.01        # stop-límite residente: límite 1% bajo el stop
    cb_portfolio_dd: float = 0.10       # -10% desde máximo histórico → alto total, reactivación manual
    cb_daily: float = 0.03              # -3% en el día → pausa de compras
    cb_weekly: float = 0.05             # -5% en la semana → pausa de compras
    min_order_notional: float = 25.0    # órdenes menores no tienen sentido (comisión mínima US$0.35)
    comm_per_share: float = 0.0035      # IBKR Pro Tiered
    comm_min: float = 0.35


@dataclass(frozen=True)
class BrokerConfig:
    """Conexión al bróker. El puerto REAL nunca es el valor por defecto.
    Puertos de IB GATEWAY (decisión de Fase 2: Gateway + IBC, no TWS) — si algún día se corre
    TWS en vez de Gateway, los puertos son otros: paper 7497, real 7496."""
    host: str = "127.0.0.1"
    paper_port: int = 4002              # IB Gateway paper. TWS paper sería 7497.
    live_port: int = 4001               # IB Gateway real. TWS real sería 7496.
    client_id: int = 17
    paper_account: str = "DUT141709"
    live_account: str = "U29034226"
    # Interruptor explícito. Debe ponerse en True a mano, en un archivo de entorno separado,
    # y además pasar la verificación de `assert_live_allowed()` en el arranque (Fase 5).
    live_enabled: bool = False


LIMITS = RiskLimits()
BROKER = BrokerConfig()
