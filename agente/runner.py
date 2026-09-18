"""
Ciclo diario — une `strategy.py` (señales) + `risk.py` (aprobación) + `broker.py` (bróker) +
`audit.py` (bitácora), y agrega lo único que ninguno de los tres hace por su cuenta: el
arbitraje de cupos entre los 7 ETFs cuando hay más señales de entrada que cupos disponibles.

IMPORTANTE — dónde corre esto: igual que `broker.py` y `test_connection.py`, este módulo debe
ejecutarse en la Mac de Darwing (o, desde la Fase 4, en el VPS) — IB Gateway solo acepta
conexiones desde la misma máquina donde está corriendo, nunca desde la nube.

Modo de ejecución — dos modos, uno seguro por defecto:
- `execute=False` (por defecto, uso mientras "Read-Only API" siga activo en IB Gateway):
  calcula todo — frenos, señales, propuestas, decisiones de riesgo — y lo registra completo en
  la bitácora, pero NO envía ninguna orden al bróker (aunque lo intentara, Read-Only la
  rechazaría). Es el modo para correr el ciclo día tras día y revisar que las decisiones son las
  esperadas, antes de arriesgar dinero de papel.
- `execute=True` (solo después de desactivar "Read-Only API" y que Darwing lo confirme, punto
  9→10 del plan de construcción): las órdenes aprobadas se envían de verdad, primero en paper.

Memoria entre corridas: `risk.py` y `strategy.py` son clases SIN estado propio — cada llamada es
independiente. Lo que sí hay que recordar de un día al siguiente (el stop vigente y el ATR de
entrada de cada posición, el cooldown de re-entrada por ticker, el máximo histórico y las
referencias diaria/semanal de los frenos, y qué órdenes quedaron pendientes de confirmar) vive
en `AgentState`, persistido en `config.STATE_PATH` — deliberadamente FUERA de la carpeta de
OneDrive (ver el comentario en `config.py` y la nota operativa de `README.md`): este archivo
cambia todos los días, así que es el que más chocaría con la sincronización que ya reveló el
problema, y aquí un dato revertido sería grave (un stop desactualizado), no solo un código viejo.

Confirmación de llenado (por qué no se actualiza el estado al momento de aprobar una orden):
una orden aprobada por `risk.py` y enviada al bróker no necesariamente se llena ese mismo día
(es DAY, con banda ±0.5% — regla 6). Así que `runner.py` nunca asume que una orden se llenó por
haberla enviado: la marca como pendiente (`pending_entries` / `pending_exits`) y en la SIGUIENTE
corrida compara contra las posiciones reales que reporta el bróker (`_reconcile`) para confirmar
qué se llenó, qué expiró sin llenarse, y — con la misma lógica — detectar si el stop-límite
residente disparó solo entre una corrida y la siguiente (la red de protección intradía de la
regla 6, no el mecanismo principal de salida).

Arbitraje de cupos (réplica de `backtest/engine.py::run`, hallazgo documentado 18-sep-2026):
cuando hay más candidatos de compra que cupos libres, se toman los primeros según
`config.EQUITY_TICKERS_ORDER` (SPY, VTI, QQQ, EFA, VWO, XLK, XLF) — igual que en el backtest
aprobado. Los candidatos que no entran en el corte de cupos ni se evalúan; los que sí entran
todavía pueden ser rechazados por `risk.py` (efectivo, tope de exposición, etc.) — un rechazo NO
le pasa su cupo al siguiente de la lista, tal como corre el backtest.

Tres brechas cerradas tras la evaluación de avance (18-sep-2026, `evaluacion-avance-fase2.md`):
1. **Sleeve conservador activo (IPS §5/§6/§8, reglas §7):** el efectivo que no está en una
   posición de renta variable no se queda ocioso — al final de cada ciclo se barre a
   `config.SLEEVE_TICKER` (BIL), por encima de `config.CASH_BUFFER`; y si una entrada aprobada
   necesita más efectivo del que hay libre, se vende BIL primero (`_defund_sleeve`) antes de
   evaluar las entradas del día, replicando la misma idea de "ventas primero" que ya se usa entre
   salidas y entradas de renta variable. Si no se puede leer el precio de BIL ese día, el sleeve
   simplemente no se toca (no bloquea el resto del ciclo).
2. **Tamaño realista mientras la cuenta paper tenga US$1,000,000:** `config.SIZING_EQUITY_CAP`
   (US$10,000, el techo de capital de la Etapa 1) limita solo el CÁLCULO de cuántas unidades
   comprar (`strategy.entry_proposal`) — los frenos de `risk.py` siguen evaluando contra el
   equity real de la cuenta.
3. **Guard de cierre de mercado:** `run_cycle` no corre antes de las 4:15pm hora de Nueva York
   (`_assert_market_closed`) — antes de esa hora, la "barra de hoy" que reporta IBKR está a medio
   formar y tratarla como el cierre real generaría señales sobre un precio que todavía se mueve.
   `require_market_closed=False` (o `--allow-partial-bar` en la CLI) lo salta a propósito, para
   pruebas o si alguna vez hace falta mirar el estado sin esperar al cierre.

Cuatro pendientes menores cerrados tras la misma evaluación (18-sep-2026):
4. **`traded_today` ahora se llena de verdad** (`risk.RiskEngine.evaluate`): antes se leía pero
   nunca se escribía, así que la prohibición de ida-y-vuelta el mismo día (regla 4) nunca se
   verificaba por código.
5. **`flow` registra depósitos/retiros** (ver el parámetro del mismo nombre abajo) para que no se
   confundan con una ganancia o pérdida de mercado.
6. **Rebalanceo por exceso de 10pp** (IPS §8, `_rebalance_if_needed`): recorta posiciones
   proporcionalmente hasta el tope cuando la exposición lo supera en más de 10pp.
7. **Filtro de liquidez re-verificado en cada entrada** (reglas §7, `_liquidity_check`): antes de
   sumar un candidato de entrada, se re-calcula su ADV en dólares de los últimos
   `config.LIQUIDITY_ADV_WINDOW_DAYS` días — si cae por debajo de `config.LIQUIDITY_ADV_MIN_USD`,
   la entrada se omite (con aviso, nunca en silencio). Fail-open si no hay dato de volumen (no
   bloquea por un hueco de datos, igual que el resto del código ante datos faltantes) — y OJO:
   la unidad de `volume` que reporta IBKR para estas barras todavía no se verificó contra datos
   reales (ver el docstring de `broker.py::daily_bars`), así que la primera corrida real debe
   confirmarla antes de confiar en un rechazo de este filtro.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .audit import AuditLog
from .broker import Broker
from .config import (
    AUDIT_LOG_PATH, CASH_BUFFER, EQUITY_TICKERS_ORDER, LIQUIDITY_ADV_MIN_USD,
    LIQUIDITY_ADV_WINDOW_DAYS, REBALANCE_TRIGGER_EXCESS_PCT, SIZING_EQUITY_CAP, SLEEVE_TICKER,
    STATE_PATH,
)
from .risk import OrderProposal, PortfolioState, Position as RiskPosition, RiskEngine
from .strategy import OpenPosition, Strategy, TickerMemory, indicators

# Guard de cierre de mercado (brecha 3, evaluación de avance 18-sep-2026): NYSE/NASDAQ cierran a
# las 4:00pm hora de Nueva York; 15 minutos de margen para que IBKR ya haya publicado el cierre
# real como barra diaria (no la barra "en curso" del día).
NY_TZ = ZoneInfo("America/New_York")
MARKET_CLOSE_TIME = dtime(16, 15)


# ---------------------------------------------------------------------------- estado persistido
@dataclass
class AgentState:
    """Todo lo que el ciclo diario necesita recordar entre corridas. IBKR no guarda nada de
    esto por su cuenta — ni el stop vigente, ni el ATR de entrada, ni el cooldown, ni los frenos.
    Se serializa a JSON tal cual (todo aquí son tipos simples: float, bool, str, dict, o None)."""
    last_equity: float = 0.0                               # equity al CIERRE de la última corrida
    high_water_mark: float = 0.0
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    halted: bool = False
    paused_until: str | None = None                       # fecha ISO o None
    killed: bool = False
    last_run_date: str | None = None                       # fecha ISO de la última corrida completada

    positions: dict[str, dict] = field(default_factory=dict)        # ticker -> OpenPosition (dict)
    memory: dict[str, dict] = field(default_factory=dict)           # ticker -> TickerMemory (dict)
    pending_entries: dict[str, dict] = field(default_factory=dict)  # ticker -> {"stop","atr_at_entry","submitted_date"}
    pending_exits: dict[str, dict] = field(default_factory=dict)    # ticker -> {"reason","submitted_date"}
    resident_stops: dict[str, dict] = field(default_factory=dict)   # ticker -> {"order_id","stop"}

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "AgentState":
        base = cls()
        base_dict = asdict(base)
        base_dict.update({k: v for k, v in d.items() if k in base_dict})
        return cls(**base_dict)


def load_state(path: Path = STATE_PATH) -> AgentState:
    if not path.exists():
        return AgentState()
    return AgentState.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_state(state: AgentState, path: Path = STATE_PATH) -> None:
    """Escritura atómica (escribe a un archivo temporal y renombra) — nunca deja el archivo de
    estado a medio escribir si el proceso se interrumpe a mitad de la escritura."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# -------- conversión OpenPosition/TickerMemory <-> dict (para guardarlos en AgentState) --------
def _pos_to_dict(p: OpenPosition) -> dict:
    return {"entry": p.entry, "stop": p.stop, "hi_close": p.hi_close,
            "atr_at_entry": p.atr_at_entry, "entry_date": p.entry_date.isoformat()}


def _pos_from_dict(d: dict) -> OpenPosition:
    return OpenPosition(entry=d["entry"], stop=d["stop"], hi_close=d["hi_close"],
                         atr_at_entry=d["atr_at_entry"], entry_date=date.fromisoformat(d["entry_date"]))


def _mem_to_dict(m: TickerMemory) -> dict:
    return {"need_new_cross": m.need_new_cross,
            "last_stop_exit": m.last_stop_exit.isoformat() if m.last_stop_exit else None}


def _mem_from_dict(d: dict) -> TickerMemory:
    last = d.get("last_stop_exit")
    return TickerMemory(need_new_cross=bool(d.get("need_new_cross", False)),
                         last_stop_exit=date.fromisoformat(last) if last else None)


def _assert_market_closed(now: datetime) -> None:
    """Corta temprano si todavía no pasó el cierre de NYSE/NASDAQ + margen (brecha 3). Sin esto,
    correr el ciclo a media tarde tomaría la barra diaria "en curso" que reporta IBKR como si
    fuera el cierre real, y `strategy.indicators()` generaría señales sobre un precio que todavía
    puede moverse. `now` puede venir sin zona (se asume hora de Nueva York) o con zona (se
    convierte)."""
    now_ny = now.astimezone(NY_TZ) if now.tzinfo else now.replace(tzinfo=NY_TZ)
    if now_ny.time() < MARKET_CLOSE_TIME:
        raise RuntimeError(
            f"Todavía no pasó el cierre de NYSE/NASDAQ + margen ({MARKET_CLOSE_TIME.strftime('%H:%M')} "
            f"hora de Nueva York) — ahora son las {now_ny.strftime('%H:%M')} allá. Correr más "
            f"tarde, o con require_market_closed=False (--allow-partial-bar en la CLI) si de "
            f"verdad se quiere usar una barra parcial."
        )


def _is_trailing_stop(pos: OpenPosition, strat: Strategy) -> bool:
    """Mismo criterio que `strategy.check_exit`: distingue si el stop vigente ya es el trailing
    (subió) o sigue siendo el inicial — para clasificar correctamente una salida que se infiere
    (el stop-límite residente disparó solo) en vez de haberla decidido `check_exit` hoy."""
    initial_stop = pos.entry - strat.p.k_init * pos.atr_at_entry
    return pos.stop > initial_stop + 1e-9


def _liquidity_check(df: pd.DataFrame, min_adv_usd: float = LIQUIDITY_ADV_MIN_USD,
                      window: int = LIQUIDITY_ADV_WINDOW_DAYS) -> tuple[bool, str | None]:
    """Filtro de liquidez re-verificado en cada entrada (reglas §7, pendiente cerrado en la
    evaluación de avance 18-sep-2026): ADV en dólares de los últimos `window` días de barras. Si
    cae por debajo de `min_adv_usd`, la entrada no debe pasar.

    Fail-open a propósito, igual que el resto del código ante un dato faltante (p.ej.
    `_sleeve_close` devolviendo `None` sin bloquear el ciclo): si `df` no tiene columna `volume`
    (bróker o barra sintética sin ese dato), o los últimos `window` días no traen ningún volumen
    numérico utilizable, esta función deja pasar la entrada — nunca rechaza por no poder calcular,
    solo por confirmar que el ADV de verdad está por debajo del mínimo. Ver `broker.py::daily_bars`
    para la advertencia pendiente sobre la unidad de `volume` que reporta IBKR (sin verificar
    todavía contra datos reales).

    Devuelve `(pasa, detalle)` — `detalle` es `None` si pasó (con o sin poder calcularlo); si no
    pasó, es el mensaje listo para el aviso/bitácora."""
    if "volume" not in df.columns:
        return True, None
    recent = df.tail(window)
    dollar_vol = (recent["volume"].astype(float) * recent["close"].astype(float)).dropna()
    if dollar_vol.empty:
        return True, None
    adv = float(dollar_vol.mean())
    if adv < min_adv_usd:
        return False, (
            f"ADV de los últimos {len(dollar_vol)} días: US${adv:,.0f}, por debajo del mínimo "
            f"(US${min_adv_usd:,.0f}) — entrada omitida (reglas §7)."
        )
    return True, None


# ---------------------------------------------------------------------------- ciclo diario
def run_cycle(*, today: date | None = None, now: datetime | None = None, execute: bool = False,
              require_market_closed: bool = True, flow: float = 0.0, broker: Broker | None = None,
              state_path: Path = STATE_PATH, audit_path: Path = AUDIT_LOG_PATH) -> dict:
    """Corre un ciclo diario completo y devuelve un resumen (dict) para imprimir/inspeccionar.
    `execute=False` por defecto: ver el modo de ejecución en el docstring del módulo.
    `require_market_closed=True` por defecto (brecha 3): exige que ya haya pasado el cierre de
    NYSE/NASDAQ + margen antes de leer barras y calcular señales.
    `flow` (pendiente cerrado en la evaluación de avance, 18-sep-2026): depósito (+) o retiro (-)
    que YA se hizo en IBKR antes de correr este ciclo el mismo día. Sin esto, un depósito se vería
    como una ganancia enorme (inflando el máximo histórico sin motivo) y un retiro como una
    pérdida enorme (disparando el freno diario/semanal o el alto de portafolio sin motivo) — ver
    `risk.record_flow()` y el IPS sección 7.1. Se aplica ANTES de `roll_day()` a propósito: así
    la referencia del día ya incluye el flujo, y el retorno que calculan los frenos refleja solo
    lo que hizo el mercado hoy, no el depósito/retiro."""
    today = today or date.today()
    if require_market_closed:
        _assert_market_closed(now or datetime.now(NY_TZ))
    audit = AuditLog(audit_path)
    state = load_state(state_path)

    if state.last_run_date == today.isoformat():
        raise RuntimeError(
            f"Ya se corrió el ciclo de hoy ({today.isoformat()}); no se repite en el mismo día "
            f"(evita reiniciar mal las referencias diaria/semanal de los frenos)."
        )

    risk = RiskEngine()
    strat = Strategy()
    own_broker = broker is None
    broker = broker or Broker(live=False)
    if own_broker:
        broker.connect()

    report: dict[str, Any] = {
        "date": today.isoformat(), "execute": execute, "flow": 0.0,
        "breaker_events": [], "warnings": [], "exits": [], "entries": [], "rejected": [],
        "resident_stops": [], "sleeve": [], "rebalance": [],
    }

    try:
        # ---------------- 1. foto del portafolio: real (bróker) + frenos (persistidos) ----------------
        broker_state = broker.portfolio_state()
        # OJO con el orden: `pf.equity` arranca en el cierre de la corrida ANTERIOR (persistido),
        # no en el valor de hoy — `roll_day` necesita eso para fijar `day_start_equity` como el
        # cierre de ayer (ver `test_risk.py::test_roll_day_limpia_operaciones_del_dia`). Recién
        # `update_marks`, con el equity de HOY que reporta el bróker, mueve `pf.equity` hacia
        # adelante y calcula el retorno diario/semanal correctamente. Si se pasara ya el equity
        # de hoy antes de `roll_day`, el retorno diario saldría comparando hoy contra hoy (0%
        # siempre) y los frenos -3%/-5% nunca dispararían.
        pf = PortfolioState(
            equity=state.last_equity if state.last_equity > 0 else broker_state.equity,
            cash=broker_state.cash, positions=broker_state.positions,
            high_water_mark=state.high_water_mark, day_start_equity=state.day_start_equity,
            week_start_equity=state.week_start_equity, halted=state.halted,
            paused_until=date.fromisoformat(state.paused_until) if state.paused_until else None,
            killed=state.killed,
        )
        if flow:
            risk.record_flow(pf, flow)
            audit.write("runner", "flow_registrado", monto=flow, equity_tras_flujo=pf.equity)
            report["flow"] = flow
        risk.roll_day(pf, today)
        for ev in risk.update_marks(pf, broker_state.equity, today):
            audit.write("risk", ev.kind, detail=ev.detail, equity=ev.equity)
            report["breaker_events"].append({"kind": ev.kind, "detail": ev.detail})

        # ---------------- 2. reconstruir memoria de estrategia desde el estado persistido ----------------
        open_positions: dict[str, OpenPosition] = {t: _pos_from_dict(d) for t, d in state.positions.items()}
        memory: dict[str, TickerMemory] = {t: _mem_from_dict(d) for t, d in state.memory.items()}
        for t in EQUITY_TICKERS_ORDER:
            memory.setdefault(t, TickerMemory())

        # ---------------- 3. reconciliar contra lo que el bróker reporta de verdad ----------------
        _reconcile(broker, pf, state, open_positions, memory, strat, today, audit, report)

        # ---------------- 4. barras + indicadores por ticker (una sola vez por ciclo) ----------------
        rows: dict[str, pd.Series] = {}
        dfs: dict[str, pd.DataFrame] = {}   # historial completo (no solo la última fila) — lo
                                             # necesita `_liquidity_check` para el ADV de la ventana
        for t in EQUITY_TICKERS_ORDER:
            df = indicators(broker.daily_bars(t))
            if df.empty:
                report["warnings"].append(f"{t}: sin barras diarias — se omite hoy")
                continue
            last = df.iloc[-1]
            if last.name.date() != today:
                report["warnings"].append(
                    f"{t}: la última barra disponible es del {last.name.date().isoformat()}, "
                    f"no de hoy ({today.isoformat()}) — se usa igual, puede ser normal si el "
                    f"cierre oficial todavía no se publicó."
                )
            rows[t] = last
            dfs[t] = df

        # ---------------- 4b. precio del sleeve conservador (BIL) para barrido/desfondeo -------
        sleeve_close = _sleeve_close(broker)
        if sleeve_close is None:
            report["warnings"].append(
                f"{SLEEVE_TICKER}: sin barra diaria hoy — el sleeve conservador no se toca en este ciclo."
            )

        # ---------------- 5. salidas + refresco del stop-límite residente ----------------
        _process_exits(broker, risk, strat, pf, open_positions, rows, state, today, execute, audit, report)

        # ---------------- 6. entradas — con arbitraje de cupos por orden de lista ----------------
        _process_entries(broker, risk, strat, pf, open_positions, memory, state, rows, today, execute,
                          audit, report, sleeve_close=sleeve_close, dfs=dfs)

        # ---------------- 6b. rebalanceo si la exposición quedó muy por encima del tope (IPS §8) ----
        _rebalance_if_needed(broker, risk, pf, rows, today, execute, audit, report)

        # ---------------- 6c. barrer el efectivo excedente al sleeve conservador ----------------
        _sweep_to_sleeve(broker, risk, pf, sleeve_close, today, execute, audit, report)

        # ---------------- 7. persistir estado ----------------
        state.last_equity = pf.equity   # el cierre de HOY, cierre de referencia para roll_day() mañana
        state.high_water_mark = pf.high_water_mark
        state.day_start_equity = pf.day_start_equity
        state.week_start_equity = pf.week_start_equity
        state.halted = pf.halted
        state.paused_until = pf.paused_until.isoformat() if pf.paused_until else None
        state.killed = pf.killed
        state.positions = {t: _pos_to_dict(p) for t, p in open_positions.items()}
        state.memory = {t: _mem_to_dict(m) for t, m in memory.items()}
        state.last_run_date = today.isoformat()
        save_state(state, state_path)

        report["equity"] = pf.equity
        report["cash"] = pf.cash
        report["halted"] = pf.halted
        report["paused_until"] = state.paused_until
        return report
    finally:
        if own_broker:
            broker.disconnect()


# ---------------------------------------------------------------------------- reconciliación
def _reconcile(broker: Broker, pf: PortfolioState, state: AgentState,
               open_positions: dict[str, OpenPosition], memory: dict[str, TickerMemory],
               strat: Strategy, today: date, audit: AuditLog, report: dict) -> None:
    """Compara lo que se recuerda (`open_positions`, `state.pending_*`) contra lo que el bróker
    reporta de verdad, y corrige la memoria ANTES de calcular ninguna señal nueva. Nunca inventa
    un dato que no se pueda confirmar — cuando no se puede saber con certeza qué pasó, deja una
    advertencia en `report["warnings"]` en vez de adivinar."""
    broker_qty = {t: p.qty for t, p in pf.positions.items() if p.qty > 0}

    # a) salidas que se habían enviado: ¿se llenaron?
    for t, info in list(state.pending_exits.items()):
        if broker_qty.get(t, 0) <= 0:
            reason = info["reason"]
            strat.on_exit_filled(memory.setdefault(t, TickerMemory()), reason, today)
            open_positions.pop(t, None)
            state.resident_stops.pop(t, None)
            audit.write("runner", "exit_filled", ticker=t, reason=reason)
        else:
            audit.write("runner", "exit_not_filled_expired", ticker=t, reason=info["reason"])
        del state.pending_exits[t]

    # b) entradas que se habían enviado: ¿se llenaron?
    if state.pending_entries:
        avg_cost = broker.average_costs()
    for t, info in list(state.pending_entries.items()):
        if broker_qty.get(t, 0) > 0 and t not in open_positions:
            entry_price = avg_cost.get(t, info["stop"])  # normalmente viene averageCost; fallback defensivo
            open_positions[t] = OpenPosition(entry=entry_price, stop=info["stop"],
                                              hi_close=entry_price, atr_at_entry=info["atr_at_entry"],
                                              entry_date=today)
            audit.write("runner", "entry_filled", ticker=t, entry=entry_price, stop=info["stop"])
        elif broker_qty.get(t, 0) <= 0:
            audit.write("runner", "entry_not_filled_expired", ticker=t)
        del state.pending_entries[t]

    # c) posiciones que se recordaban abiertas y el bróker ya no tiene, sin que hubiera una
    #    salida pendiente registrada — probablemente el stop-límite residente disparó solo, o
    #    hubo una venta manual. Se infiere la razón más probable (residente = trailing o inicial,
    #    según el nivel del stop) y se avisa con claridad: es una inferencia, no una certeza.
    for t in list(open_positions):
        if broker_qty.get(t, 0) <= 0 and t not in state.pending_exits:
            pos = open_positions.pop(t)
            reason = "stop_trail" if _is_trailing_stop(pos, strat) else "stop_init"
            strat.on_exit_filled(memory.setdefault(t, TickerMemory()), reason, today)
            state.resident_stops.pop(t, None)
            msg = (f"{t}: la posición ya no está en el bróker sin que hubiera una salida enviada "
                   f"por el ciclo — se asume que disparó el stop-límite residente (inferido: "
                   f"{reason}) o hubo una venta manual. Revisar el historial de órdenes en IBKR.")
            report["warnings"].append(msg)
            audit.write("runner", "exit_inferred", ticker=t, reason=reason, note=msg)

    # d) posiciones que el bróker tiene y la memoria no — no se puede reconstruir su stop/ATR de
    #    entrada sin adivinar, así que NO se gestionan hasta que Darwing lo revise a mano.
    #    Excepción: el ETF del sleeve conservador (SLEEVE_TICKER) SÍ es esperado aquí — no lo
    #    gestiona `open_positions`/`pending_entries` (eso es solo para renta variable), lo
    #    gestiona el barrido/desfondeo del sleeve (`_sweep_to_sleeve`/`_defund_sleeve`) con el
    #    precio del día, no con memoria entre corridas.
    for t, qty in broker_qty.items():
        if t == SLEEVE_TICKER:
            continue
        if t not in open_positions and t not in state.pending_entries:
            msg = (f"{t}: el bróker reporta {qty} en cartera pero no está en la memoria del "
                   f"agente (¿compra manual, o memoria perdida?) — no se le calcula stop ni se "
                   f"gestiona hasta que se revise a mano.")
            report["warnings"].append(msg)
            audit.write("runner", "unrecognized_position", ticker=t, qty=qty, note=msg)


# ---------------------------------------------------------------------------- salidas
def _process_exits(broker: Broker, risk: RiskEngine, strat: Strategy, pf: PortfolioState,
                    open_positions: dict[str, OpenPosition], rows: dict[str, pd.Series],
                    state: AgentState, today: date, execute: bool, audit: AuditLog, report: dict) -> None:
    for t, pos in list(open_positions.items()):
        row = rows.get(t)
        if row is None:
            continue
        sig = strat.check_exit(t, row, pos)  # puede subir pos.stop (trailing) aunque no salga hoy
        qty = pf.positions.get(t)
        qty = qty.qty if qty else 0.0

        if sig is not None and qty > 0:
            proposal = strat.exit_proposal(sig, qty)
            decision = risk.evaluate(proposal, pf, today)
            audit.write("strategy", "exit_signal", ticker=t, reason=sig.reason, ref_close=sig.ref_close)
            audit.write("risk", "exit_decision", ticker=t, approved=decision.approved,
                        reasons=decision.reasons, checks=decision.checks, proposal=asdict(proposal))
            entry = {"ticker": t, "reason": sig.reason, "qty": qty, "limit_price": proposal.limit_price,
                     "approved": decision.approved, "reasons": list(decision.reasons)}
            report["exits"].append(entry)
            if not decision.approved:
                report["rejected"].append(entry)
                continue  # no se toca el stop residente: la salida no se envió, sigue protegiendo

            if execute:
                broker.submit_order(proposal)
            state.pending_exits[t] = {"reason": sig.reason, "submitted_date": today.isoformat()}
            # Deliberado: el stop-límite residente NO se cancela aquí. Si esta salida (DAY) no
            # se llena, el residente sigue protegiendo hasta la próxima corrida; si sí se llena,
            # `_reconcile` lo detecta y limpia `state.resident_stops` entonces. El riesgo — que
            # ambas órdenes intenten llenarse — lo resuelve el propio IBKR rechazando la segunda
            # venta por no tener ya de qué vender; nunca vende de más.
            pf.positions.pop(t, None)  # ya no cuenta como exposición para las evaluaciones de hoy
            # Efectivo liberado ESTIMADO (precio límite, no el de llenado real) se suma aquí para
            # que una entrada evaluada en el mismo ciclo pueda usarlo — igual que el backtest
            # aprobado, que liquida ventas antes de evaluar compras dentro del mismo día
            # (`engine.py::run`, "ventas primero"). Si esta venta no se llena, la entrada que la
            # haya usado tampoco se llenará por falta real de fondos — IBKR la deja pendiente sin
            # ejecutar, nunca presta de más (cuenta "No Borrow Margin").
            commission = max(risk.limits.comm_min, risk.limits.comm_per_share * qty)
            pf.cash += proposal.qty * proposal.limit_price - commission
            continue

        if qty <= 0:
            continue  # sin tamaño real que proteger (lo detectará `_reconcile` en el próximo ciclo)

        # sin salida hoy: refrescar el stop-límite residente si el trailing lo subió (o si nunca
        # se puso). Es la red de protección intradía de la regla 6 — no el mecanismo principal.
        existing = state.resident_stops.get(t)
        if existing is not None and abs(existing["stop"] - pos.stop) < 1e-9:
            continue  # el stop no cambió desde el último refresco; no reemplazar sin necesidad
        stop_proposal = strat.resident_stop_proposal(t, qty, pos.stop, float(row["close"]))
        decision = risk.evaluate(stop_proposal, pf, today)
        report["resident_stops"].append({"ticker": t, "stop": pos.stop, "approved": decision.approved,
                                          "reasons": list(decision.reasons)})
        audit.write("risk", "resident_stop_decision", ticker=t, approved=decision.approved,
                    reasons=decision.reasons, proposal=asdict(stop_proposal))
        if not decision.approved:
            report["warnings"].append(f"{t}: no se pudo formar el stop-límite residente ({decision.reasons})")
            continue
        if execute:
            if existing is not None:
                broker.cancel_order(existing["order_id"])
            order_id = broker.submit_order(stop_proposal)
            state.resident_stops[t] = {"order_id": order_id, "stop": pos.stop}
        # en modo lectura (execute=False) no hay order_id real que guardar — el refresco queda
        # solo registrado en la bitácora, para revisión, hasta que se active el envío de órdenes.


# ---------------------------------------------------------------------------- entradas
def _process_entries(broker: Broker, risk: RiskEngine, strat: Strategy, pf: PortfolioState,
                      open_positions: dict[str, OpenPosition], memory: dict[str, TickerMemory],
                      state: AgentState, rows: dict[str, pd.Series], today: date, execute: bool,
                      audit: AuditLog, report: dict, sleeve_close: float | None = None,
                      dfs: dict[str, pd.DataFrame] | None = None) -> None:
    if pf.halted or (pf.paused_until is not None and today < pf.paused_until):
        return  # los frenos ya lo bloquearían en risk.evaluate(), pero evita calcular en vano

    held_by_broker = {t for t, p in pf.positions.items() if p.qty > 0}
    busy = set(open_positions) | set(state.pending_entries) | set(state.pending_exits) | held_by_broker
    slots = max(0, risk.limits.max_positions - len(open_positions) - len(state.pending_entries))
    if slots <= 0:
        return

    # arbitraje de cupos: candidatos en el ORDEN de la lista, réplica del backtest aprobado —
    # un rechazo de risk.py no le pasa su cupo al siguiente (ver docstring del módulo). El filtro
    # de liquidez (reglas §7, brecha 4) se aplica ANTES de sumar el candidato: un ticker ilíquido
    # no debe quitarle un cupo a otro que sí pasaría (misma lógica que un rechazo de risk.py).
    candidates: list = []
    for t in EQUITY_TICKERS_ORDER:
        if t in busy or t not in rows:
            continue
        sig = strat.check_entry(t, rows[t], memory[t], today)
        if sig is None:
            continue
        if dfs is not None and t in dfs:
            liquid, detail = _liquidity_check(dfs[t])
            if not liquid:
                report["warnings"].append(f"{t}: {detail}")
                audit.write("strategy", "liquidity_rejected", ticker=t, detail=detail)
                continue
        candidates.append(sig)
    candidates = candidates[:slots]

    # Sleeve conservador — "ventas primero" (brecha 1): si lo que se necesita hoy para las
    # entradas candidatas supera el efectivo libre (por encima de CASH_BUFFER), se vende BIL
    # primero. Estimación con los mismos proposals que se van a evaluar abajo — barato y
    # determinístico; volver a calcularlos ahí no cambia el resultado.
    if candidates and sleeve_close is not None:
        provisional = [strat.entry_proposal(sig, pf.equity, sizing_cap=SIZING_EQUITY_CAP) for sig in candidates]
        needed = sum(p.qty * p.limit_price + max(risk.limits.comm_min, risk.limits.comm_per_share * p.qty)
                     for p in provisional)
        shortfall = needed - (pf.cash - CASH_BUFFER)
        if shortfall > 0:
            _defund_sleeve(shortfall, broker, risk, pf, sleeve_close, today, execute, audit, report)

    for sig in candidates:
        proposal = strat.entry_proposal(sig, pf.equity, sizing_cap=SIZING_EQUITY_CAP)
        decision = risk.evaluate(proposal, pf, today)
        audit.write("strategy", "entry_signal", ticker=sig.ticker, ref_close=sig.ref_close,
                    atr=sig.atr, stop=sig.stop)
        audit.write("risk", "entry_decision", ticker=sig.ticker, approved=decision.approved,
                    reasons=decision.reasons, checks=decision.checks, proposal=asdict(proposal))
        entry = {"ticker": sig.ticker, "qty": proposal.qty, "limit_price": proposal.limit_price,
                 "stop": proposal.stop_price, "approved": decision.approved, "reasons": list(decision.reasons)}
        report["entries"].append(entry)
        if not decision.approved:
            report["rejected"].append(entry)
            continue

        if execute:
            broker.submit_order(proposal)
        state.pending_entries[sig.ticker] = {"stop": proposal.stop_price, "atr_at_entry": sig.atr,
                                              "submitted_date": today.isoformat()}
        # reflejar el compromiso en la foto local: el próximo candidato de este mismo ciclo no
        # debe ver efectivo/exposición/cupos como si esta compra no existiera (aunque todavía no
        # se haya confirmado el llenado) — así se replica cómo el backtest procesa compras en
        # secuencia dentro del mismo día (engine.py::run, sección "entradas").
        commission = max(risk.limits.comm_min, risk.limits.comm_per_share * proposal.qty)
        pf.cash -= proposal.qty * proposal.limit_price + commission
        pf.positions[sig.ticker] = RiskPosition(qty=proposal.qty, last_price=proposal.limit_price)


# ---------------------------------------------------------------------------- rebalanceo (IPS §8)
def _rebalance_if_needed(broker: Broker, risk: RiskEngine, pf: PortfolioState, rows: dict[str, pd.Series],
                          today: date, execute: bool, audit: AuditLog, report: dict) -> None:
    """IPS §8: "revisión trimestral, o antes si la exposición a renta variable supera el tope en
    más de 10 puntos porcentuales por revalorización — en ese caso se recortan posiciones
    proporcionalmente hasta volver al tope". Se corre después de las salidas/entradas normales de
    hoy (así ve la exposición ya actualizada) y antes del barrido al sleeve. Vende una porción de
    CADA posición de renta variable abierta, proporcional a su peso dentro de la exposición total
    — nunca recorta por debajo del tope, solo hasta él. No es una señal de `strategy.py`: no
    activa cooldown de re-entrada ni pasa por `state.pending_exits` (esto no es un stop ni un fin
    de régimen), y no necesita reconciliación especial — `OpenPosition` no guarda cantidad propia,
    así que la próxima corrida ve la cantidad ya reducida directo del bróker, como con cualquier
    venta parcial real."""
    L = risk.limits
    if pf.equity <= 0:
        return
    exposure = pf.equity_exposure()
    cap_amount = L.equity_cap_pct * pf.equity
    trigger_amount = cap_amount + REBALANCE_TRIGGER_EXCESS_PCT * pf.equity
    if exposure <= trigger_amount:
        return
    excess = exposure - cap_amount  # recortar EXACTAMENTE hasta el tope, no más abajo
    held = [(t, p) for t, p in list(pf.positions.items()) if t in EQUITY_TICKERS_ORDER and p.qty > 0]
    if not held:
        return
    for t, p in held:
        row = rows.get(t)
        if row is None:
            continue
        share = p.notional / exposure
        sell_notional = excess * share
        ref_close = float(row["close"])
        limit_price = round(ref_close * (1 - L.limit_band), 4)
        qty = min(p.qty, sell_notional / max(limit_price, 1e-9))
        qty = round(qty, 6)
        if qty <= 0 or qty * limit_price < L.min_order_notional:
            continue
        proposal = OrderProposal(ticker=t, side="SELL", qty=qty, limit_price=limit_price,
                                  ref_close=ref_close, kind="LMT", reason="rebalance")
        decision = risk.evaluate(proposal, pf, today)
        audit.write("risk", "rebalance_decision", ticker=t, approved=decision.approved,
                    reasons=decision.reasons, proposal=asdict(proposal))
        report["rebalance"].append({"ticker": t, "qty": qty, "limit_price": limit_price,
                                     "approved": decision.approved, "reasons": list(decision.reasons)})
        if not decision.approved:
            continue
        if execute:
            broker.submit_order(proposal)
        commission = max(L.comm_min, L.comm_per_share * qty)
        pf.cash += qty * limit_price - commission
        remaining = p.qty - qty
        if remaining > 1e-9:
            pf.positions[t] = RiskPosition(qty=remaining, last_price=p.last_price)
        else:
            pf.positions.pop(t, None)


# ---------------------------------------------------------------------------- sleeve conservador
def _sleeve_close(broker: Broker) -> float | None:
    """Último cierre de `config.SLEEVE_TICKER` (BIL) — precio de referencia para las órdenes LMT
    de barrido/desfondeo. `None` si no se pudo leer (no bloquea el resto del ciclo: el sleeve
    simplemente no se toca ese día; queda una advertencia en el reporte)."""
    try:
        df = broker.daily_bars(SLEEVE_TICKER, duration="5 D")
    except Exception:
        return None
    if df.empty:
        return None
    return float(df.iloc[-1]["close"])


def _defund_sleeve(needed: float, broker: Broker, risk: RiskEngine, pf: PortfolioState,
                    sleeve_close: float, today: date, execute: bool, audit: AuditLog, report: dict) -> None:
    """Vende parte (o todo) del sleeve conservador para financiar entradas de renta variable
    cuando el efectivo libre no alcanza (reglas §7: el sleeve es el destino POR DEFECTO del
    capital, no un colchón intocable). `needed` es el faltante estimado en dólares; nunca
    vende más de lo que se tiene. Pasa por `risk.evaluate()` igual que cualquier otra
    orden — si por algún motivo se rechazara, las entradas de hoy simplemente se quedarán sin
    ese efectivo y `risk.py` las rechazará por INSUFFICIENT_CASH, con la razón visible en la
    bitácora (nunca se asume que la venta salió bien)."""
    L = risk.limits
    held = pf.positions.get(SLEEVE_TICKER, RiskPosition(0.0, 0.0)).qty
    if needed <= 0 or held <= 0:
        return
    limit_price = round(sleeve_close * (1 - L.limit_band), 4)
    qty = min(held, (needed + L.comm_min) / max(limit_price, 1e-9))
    qty = round(qty, 6)
    if qty <= 0:
        return
    proposal = OrderProposal(ticker=SLEEVE_TICKER, side="SELL", qty=qty, limit_price=limit_price,
                              ref_close=sleeve_close, kind="LMT", reason="sleeve_defund")
    decision = risk.evaluate(proposal, pf, today)
    audit.write("risk", "sleeve_defund_decision", ticker=SLEEVE_TICKER, approved=decision.approved,
                reasons=decision.reasons, proposal=asdict(proposal))
    report["sleeve"].append({"action": "defund", "qty": qty, "limit_price": limit_price,
                              "approved": decision.approved, "reasons": list(decision.reasons)})
    if not decision.approved:
        return
    if execute:
        broker.submit_order(proposal)
    commission = max(L.comm_min, L.comm_per_share * qty)
    pf.cash += qty * limit_price - commission
    remaining = held - qty
    if remaining > 1e-9:
        pf.positions[SLEEVE_TICKER] = RiskPosition(qty=remaining, last_price=sleeve_close)
    else:
        pf.positions.pop(SLEEVE_TICKER, None)


def _sweep_to_sleeve(broker: Broker, risk: RiskEngine, pf: PortfolioState, sleeve_close: float | None,
                      today: date, execute: bool, audit: AuditLog, report: dict) -> None:
    """Barre el efectivo por encima de `config.CASH_BUFFER` a `config.SLEEVE_TICKER` (BIL) — nunca
    efectivo ocioso (IBKR no paga interés sobre los primeros US$10,000, checklist de apertura).
    Se corre al final del ciclo, después de que las entradas de hoy ya comprometieron lo suyo. Si
    los frenos bloquean compras (halted/paused), `risk.evaluate()` rechaza esta compra igual que
    cualquier otra — el efectivo queda sin invertir ese día, sin ningún riesgo adicional."""
    if sleeve_close is None:
        return
    L = risk.limits
    available = pf.cash - CASH_BUFFER
    if available < L.min_order_notional:
        return
    limit_price = round(sleeve_close * (1 + L.limit_band), 4)
    qty = round((available - L.comm_min) / max(limit_price, 1e-9), 6)
    if qty <= 0:
        return
    proposal = OrderProposal(ticker=SLEEVE_TICKER, side="BUY", qty=qty, limit_price=limit_price,
                              ref_close=sleeve_close, kind="LMT", reason="sleeve_sweep")
    decision = risk.evaluate(proposal, pf, today)
    audit.write("risk", "sleeve_sweep_decision", ticker=SLEEVE_TICKER, approved=decision.approved,
                reasons=decision.reasons, proposal=asdict(proposal))
    report["sleeve"].append({"action": "sweep", "qty": qty, "limit_price": limit_price,
                              "approved": decision.approved, "reasons": list(decision.reasons)})
    if not decision.approved:
        return
    if execute:
        broker.submit_order(proposal)
    commission = max(L.comm_min, L.comm_per_share * qty)
    pf.cash -= qty * limit_price + commission
    held = pf.positions.get(SLEEVE_TICKER, RiskPosition(0.0, 0.0)).qty
    pf.positions[SLEEVE_TICKER] = RiskPosition(qty=held + qty, last_price=sleeve_close)


# ---------------------------------------------------------------------------- CLI
def _print_report(report: dict) -> None:
    print(f"=== Ciclo {report['date']} ({'EJECUTANDO' if report['execute'] else 'solo lectura'}) ===")
    print(f"Equity: {report.get('equity', 0):.2f}  Cash: {report.get('cash', 0):.2f}  "
          f"Halted: {report.get('halted')}  Paused: {report.get('paused_until')}")
    if report.get("flow"):
        tipo = "DEPÓSITO" if report["flow"] > 0 else "RETIRO"
        print(f"  [FLUJO] {tipo} registrado: {report['flow']:+.2f}")
    for r in report.get("rebalance", []):
        print(f"  REBALANCEO {r['ticker']:<5} qty={r['qty']:.4f} limite={r['limit_price']:.2f} "
              f"{'ok' if r['approved'] else 'RECHAZADO: ' + ', '.join(r['reasons'])}")
    for ev in report["breaker_events"]:
        print(f"  [FRENO] {ev['kind']}: {ev['detail']}")
    for w in report["warnings"]:
        print(f"  [AVISO] {w}")
    for e in report["exits"]:
        print(f"  SALIDA  {e['ticker']:<5} {e['reason']:<12} qty={e['qty']:.4f} "
              f"{'aprobada' if e['approved'] else 'RECHAZADA: ' + ', '.join(e['reasons'])}")
    for e in report["entries"]:
        print(f"  ENTRADA {e['ticker']:<5} qty={e['qty']:.4f} limite={e['limit_price']:.2f} "
              f"stop={e['stop']:.2f} {'aprobada' if e['approved'] else 'RECHAZADA: ' + ', '.join(e['reasons'])}")
    for r in report["resident_stops"]:
        print(f"  STOP RESIDENTE {r['ticker']:<5} nivel={r['stop']:.2f} "
              f"{'ok' if r['approved'] else 'RECHAZADO: ' + ', '.join(r['reasons'])}")
    for s in report["sleeve"]:
        etiqueta = "VENTA (para financiar entradas)" if s["action"] == "defund" else "COMPRA (barrido de efectivo)"
        print(f"  SLEEVE {SLEEVE_TICKER} {etiqueta} qty={s['qty']:.4f} limite={s['limit_price']:.2f} "
              f"{'ok' if s['approved'] else 'RECHAZADO: ' + ', '.join(s['reasons'])}")
    if not (report["exits"] or report["entries"]):
        print("  (sin señales hoy)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ciclo diario del agente de bolsa.")
    parser.add_argument("--execute", action="store_true",
                         help="Enviar de verdad las órdenes aprobadas (requiere Read-Only "
                              "desactivado en IB Gateway). Por defecto: solo calcula y registra.")
    parser.add_argument("--allow-partial-bar", action="store_true",
                         help="Saltar el guard de cierre de mercado y correr aunque la barra de "
                              "hoy todavía esté a medio formar (NUNCA usar para decidir una orden "
                              "real — solo para revisar estado antes del cierre).")
    parser.add_argument("--flow", type=float, default=0.0,
                         help="Depósito (positivo) o retiro (negativo) que YA se hizo en IBKR "
                              "hoy, antes de correr este ciclo — para que no se vea como una "
                              "ganancia o pérdida de mercado. Ejemplo: --flow 5000 (depósito de "
                              "US$5,000), --flow -2000 (retiro de US$2,000). Usar solo el día del "
                              "movimiento, nunca en corridas posteriores del mismo flujo.")
    args = parser.parse_args()

    with Broker(live=False) as broker:
        report = run_cycle(execute=args.execute, require_market_closed=not args.allow_partial_bar,
                            flow=args.flow, broker=broker)
    _print_report(report)


if __name__ == "__main__":
    main()
