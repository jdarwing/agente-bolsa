"""
Prueba de humo de la conexión a IB Gateway — CORRER EN LA TERMINAL DE LA MAC, no desde la nube
(IB Gateway solo acepta conexiones desde la misma máquina donde corre). Con IB Gateway abierto
y logueado en la cuenta paper DUT141709:

    cd "/Users/darwingcasana/Library/CloudStorage/OneDrive-AGRICOLADONRICARDO/My Information/Agentes IA/Agente de Bolsa/agente"
    pip3 install ib_async pandas
    python3 -m agente.test_connection

Si algo falla, copia el error completo — se ajusta el código con eso, no hay que adivinar.
"""
from __future__ import annotations

from .broker import Broker


def main() -> None:
    b = Broker(live=False)
    print(f"Conectando a 127.0.0.1:{b.port} (cuenta {b.account})...")
    b.connect()
    print("Conectado:", b.ib.isConnected())

    st = b.portfolio_state()
    print(f"Equity: {st.equity:.2f}  Cash: {st.cash:.2f}  Posiciones: {list(st.positions) or '(ninguna)'}")

    print("Pidiendo 1 mes de barras diarias de SPY...")
    bars = b.daily_bars("SPY", duration="1 M")
    print(bars.tail())

    b.disconnect()
    print("Desconectado. Prueba OK.")


if __name__ == "__main__":
    main()
