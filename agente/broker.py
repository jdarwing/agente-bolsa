"""
Adaptador de bróker — conecta con IB Gateway vía `ib_async` y traduce su estado a lo que
`risk.PortfolioState` necesita. Esta capa solo LEE el estado de la cuenta y ENVÍA lo que
`strategy.py` ya propuso y `risk.py` ya aprobó — nunca decide nada por su cuenta.

IMPORTANTE — dónde corre esto: IB Gateway solo acepta conexiones desde la MISMA máquina donde
está corriendo (127.0.0.1 por defecto, sin abrir el puerto a la red). Este módulo tiene que
ejecutarse en la Mac de Darwing (decisión de Fase 2), nunca desde la nube — por eso no se pudo
probar en esta sesión contra un IB Gateway real. `test_connection.py`, en esta misma carpeta,
es la prueba de humo que Darwing corre en su propia Terminal; lo que reporte esa corrida es lo
que confirma (o corrige) este archivo.

`live=True` solo se usa en la Fase 5, y solo si `config.BROKER.live_enabled` ya está en `True`
en un archivo de entorno separado — el valor por defecto del código es `False` a propósito.
"""
from __future__ import annotations

import pandas as pd
from ib_async import IB, LimitOrder, StartupFetch, Stock, StopLimitOrder, Trade, util

from .config import BROKER
from .risk import OrderProposal, Position, PortfolioState


class Broker:
    def __init__(self, live: bool = False):
        if live and not BROKER.live_enabled:
            raise RuntimeError(
                "live_enabled=False: el agente no puede conectar al puerto real todavía "
                "(ver config.py y el punto de control de la Fase 5)."
            )
        self.live = live
        self.port = BROKER.live_port if live else BROKER.paper_port
        self.account = BROKER.live_account if live else BROKER.paper_account
        self.ib = IB()

    def connect(self) -> None:
        """No pide la sincronización inicial de órdenes abiertas/completadas al conectar
        (evaluación de avance, 29-sep-2026 — corrige el diagnóstico del 28-sep, ver abajo).

        El 28-sep se subió `timeout` a 20s (antes ~4s, el default de `ib_async`) para los avisos
        "open orders request timed out" / "completed orders request timed out" que aparecían en
        cada corrida, asumiendo que 4s era poco tiempo. **La corrida real del 29-sep confirmó que
        esa no era la causa**: el aviso siguió apareciendo igual con 20s. Revisando el código
        fuente de `ib_async` (`ib.py`, `connectAsync()`): al conectar, la librería pide en
        paralelo posiciones, órdenes abiertas y órdenes completadas, y cada una tiene el mismo
        `timeout` — si "open orders"/"completed orders" nunca completan ni en 4s ni en 20s, el
        problema no es la duración, sino que esas dos solicitudes concretas no reciben el mensaje
        de cierre de IB Gateway (posible relación con "Read-Only API" activo — no se pudo
        confirmar la causa exacta desde la sesión en la nube, solo el punto donde ocurre).

        La solución real: no pedirlas al conectar, porque este código no las necesita ahí.
        `runner.py` nunca llama a `self.ib.trades()` — el único lugar que lo hace es
        `cancel_order()`, que ya pide su propia resincronización justo antes de usarla (ver su
        docstring), no depende de lo que se haya sincronizado al conectar. `fetchFields` deja
        fuera `ORDERS_OPEN`/`ORDERS_COMPLETE` a propósito; posiciones, cuenta y ejecuciones se
        siguen pidiendo igual, con `timeout=20` de margen para esas (se deja, aunque ya no aplique
        al aviso original, por si alguna tarda).

        **Sin verificar todavía contra IB Gateway real**: esto se confirmó leyendo el código de
        `ib_async`, no corriendo contra el Gateway de Darwing — hay que confirmarlo en la próxima
        corrida (el aviso debería desaparecer del todo; si no, la causa es otra)."""
        self.ib.connect(
            BROKER.host, self.port, clientId=BROKER.client_id, timeout=20,
            fetchFields=(
                StartupFetch.POSITIONS
                | StartupFetch.ACCOUNT_UPDATES
                | StartupFetch.SUB_ACCOUNT_UPDATES
                | StartupFetch.EXECUTIONS
            ),
        )

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def __enter__(self) -> "Broker":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------------ estado de la cuenta
    def portfolio_state(self) -> PortfolioState:
        """Construye la foto que `risk.RiskEngine.evaluate()` necesita: equity, cash y
        posiciones actuales. NO incluye los frenos (high_water_mark, paused_until, killed) —
        esos los mantiene `runner.py` entre sesiones (el bróker no los conoce)."""
        summary = {item.tag: item.value for item in self.ib.accountSummary(self.account)}
        equity = float(summary.get("NetLiquidation", 0.0))
        cash = float(summary.get("TotalCashValue", 0.0))
        positions: dict[str, Position] = {}
        for item in self.ib.portfolio(self.account):
            if item.position:
                positions[item.contract.symbol] = Position(qty=float(item.position),
                                                             last_price=float(item.marketPrice))
        return PortfolioState(equity=equity, cash=cash, positions=positions)

    def average_costs(self) -> dict[str, float]:
        """Precio promedio de compra por ticker, reportado por IBKR — lo necesita `runner.py`
        para reconstruir `strategy.OpenPosition.entry` cuando confirma que una compra se llenó
        (el bróker es la única fuente de verdad del precio real de entrada; el `limit_price` de
        la propuesta era solo una referencia antes de saber si se ejecutó)."""
        return {item.contract.symbol: float(item.averageCost)
                for item in self.ib.portfolio(self.account) if item.position}

    # ------------------------------------------------------------------ envío de órdenes
    def submit_order(self, p: OrderProposal) -> int:
        """Envía una orden al bróker y devuelve su `orderId` de IBKR. SOLO se llama con una
        propuesta que ya pasó por `RiskEngine.evaluate()` con `approved=True` — este método no
        vuelve a verificar nada, esa es la responsabilidad exclusiva de `risk.py`.

        Con "Read-Only API" activo en IB Gateway, IBKR rechaza la orden con un error propio
        (protección adicional mientras `runner.py` corre en modo lectura, ver `config.py`).

        Los DAY (entradas y salidas, regla 6 de las reglas de estrategia) se descartan solos si
        no se llenan en la sesión; el GTC (stop-límite residente, regla de protección intradía)
        queda puesto hasta que `runner.py` lo cancele o lo reemplace al subir el trailing."""
        contract = Stock(p.ticker, "SMART", "USD")
        if p.kind == "LMT":
            order = LimitOrder(p.side, p.qty, p.limit_price, tif="DAY")
        elif p.kind == "STP LMT":
            order = StopLimitOrder(p.side, p.qty, p.limit_price, p.stop_price, tif="GTC")
        else:
            raise ValueError(f"Tipo de orden no soportado: {p.kind}")
        trade: Trade = self.ib.placeOrder(contract, order)
        return trade.order.orderId

    def cancel_order(self, order_id: int) -> None:
        """Cancela una orden viva por `orderId` (p.ej. el stop residente anterior, antes de
        reemplazarlo por uno con el stop ya subido). No hace nada si ya no está viva — cancelar
        dos veces la misma orden, o una que ya se llenó, no es un error aquí.

        Pide una sincronización fresca de órdenes abiertas antes de buscar (evaluación de avance,
        28-sep-2026): `self.ib.trades()` se llena en gran parte al conectar (ver `connect()`), y
        si esa sincronización inicial no alcanzó a completarse a tiempo, esta orden podría no
        aparecer todavía — sin este refresco, la cancelación fallaría en silencio y `runner.py`
        terminaría con dos órdenes de stop vivas para el mismo ticker. Sin verificar todavía
        contra una corrida real con un stop residente vigente — confirmar la primera vez que se
        tenga que reemplazar uno de verdad."""
        self.ib.reqAllOpenOrders()
        self.ib.sleep(1)
        for trade in self.ib.trades():
            if trade.order.orderId == order_id and not trade.isDone():
                self.ib.cancelOrder(trade.order)
                return

    # ------------------------------------------------------------------ datos de precio
    def daily_bars(self, ticker: str, duration: str = "2 Y") -> pd.DataFrame:
        """Barras diarias open/high/low/close/volume, listas para `strategy.indicators()`.
        `duration` en formato IBKR ("2 Y", "6 M", "30 D", …) — 2 años sobran para la SMA200 (~1
        año de historia) con margen.

        `volume` (evaluación de avance 18-sep-2026, filtro de liquidez re-verificado en cada
        entrada, reglas §7): IBKR la trae en las barras de `whatToShow="TRADES"` sin pedirla
        aparte. **Sin verificar todavía contra datos reales** en qué unidad llega para acciones/
        ETFs de EE.UU. (podría venir en acciones individuales o en lotes de 100, según la versión
        de la API) — la primera corrida real con `runner.py` actualizado debe confirmar esto
        comparando el ADV en dólares que calcula contra un dato de referencia (p.ej. Yahoo/
        IBKR TWS) antes de confiar en un rechazo del filtro de liquidez."""
        contract = Stock(ticker, "SMART", "USD")
        bars = self.ib.reqHistoricalData(contract, endDateTime="", durationStr=duration,
                                          barSizeSetting="1 day", whatToShow="TRADES", useRTH=True)
        df = util.df(bars).set_index("date")[["open", "high", "low", "close", "volume"]]
        df.index = pd.to_datetime(df.index)
        return df.astype(float)
