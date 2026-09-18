# Agente de Bolsa — código (Fase 2)

Paquete Python del agente. Cada carpeta/archivo corresponde a una capa del plan
(`plan-construccion-agente-bolsa.md`).

## Estado

| Módulo | Capa | Estado |
|---|---|---|
| `agente/config.py` | Universo (lista blanca) y límites del IPS en código | ✅ Entregado |
| `agente/risk.py` | **Capa de riesgo**: valida cada orden propuesta, frenos (breakers), kill switch | ✅ Entregado · 36 pruebas |
| `agente/audit.py` | Bitácora de auditoría (JSON Lines, solo-append) | ✅ Entregado |
| `agente/strategy.py` | Señales (SMA200, EMA20/50, ATR, stops, cooldown de re-entrada) → propuestas de orden | ✅ Entregado · 23 pruebas |
| `agente/broker.py` | Conexión a IB Gateway (paper 4002 / real bloqueado) + barras diarias vía IBKR | ✅ Entregado y probado contra IB Gateway real (18-sep-2026) |
| `agente/test_connection.py` | Prueba de humo: conecta, lee cuenta y barras de SPY | ✅ Corrida con éxito en la Mac de Darwing |
| `agente/runner.py` | Ciclo diario: leer precios → señales → riesgo → órdenes → bitácora → arbitraje de cupos entre tickers → estado persistido → stop-límite residente | ✅ Entregado · 10 pruebas · **pendiente correr contra IB Gateway real** |
| Respaldo de datos (Nasdaq.com CSV) | Se decidió NO hacer un `data.py` separado: `broker.daily_bars()` ya cubre la fuente principal (IBKR); el respaldo manual reutiliza `load_nasdaq_csv()` de `backtest/engine.py`, que ya existe | ✅ simplificado |

## Cómo correr las pruebas

```bash
cd agente
pip install pytest
python -m pytest tests/ -q
```

## `runner.py` — el ciclo diario

Une `strategy.py` + `risk.py` + `broker.py` + `audit.py`, y agrega el arbitraje de cupos entre
los 7 tickers (orden de lista: SPY, VTI, QQQ, EFA, VWO, XLK, XLF — gana el primero cuando hay
más señales que cupos, igual que el backtest aprobado). Guarda entre corridas lo que ni IBKR ni
`risk.py`/`strategy.py` recuerdan por su cuenta (stop vigente, ATR de entrada, cooldown de
re-entrada, máximo histórico, referencias diaria/semanal) en `~/.agente_bolsa/state.json` —
**fuera de esta carpeta de OneDrive a propósito** (ver la nota operativa más abajo: ese archivo
cambia todos los días y aquí un dato revertido sería grave, no solo un código viejo). La
bitácora de auditoría del ciclo vive junto a él, en `~/.agente_bolsa/audit.jsonl`.

Dos modos:

```bash
python3 -m agente.runner              # modo lectura (por defecto): calcula y registra, no envía nada
python3 -m agente.runner --execute    # envía las órdenes aprobadas — solo con Read-Only ya desactivado
```

Mientras "Read-Only API" siga activo en IB Gateway, correr en modo lectura día tras día y
revisar la bitácora es la forma de validar que las decisiones son las esperadas antes de
arriesgar dinero de papel. Sus 10 pruebas (`tests/test_runner.py`) usan un bróker de mentira
(`FakeBroker`) — nunca IB Gateway real, que solo se puede probar desde esta Mac.

## Automatizar la corrida diaria (LaunchAgent de macOS)

Para no depender de acordarse de abrir la Terminal cada día, `run_daily.sh` +
`com.agentedebolsa.runnerdiario.plist` (en esta misma carpeta) programan el ciclo con
`launchd` — lunes a viernes, 6:00 pm hora de Perú (con margen sobre el cierre de NYSE en
cualquier época del año). Sigue en modo lectura (sin `--execute`); eso no cambia solo por
automatizarlo.

**Requisito que esto NO resuelve:** IB Gateway tiene que estar abierto y logueado en la cuenta
paper a esa hora — si no, la corrida falla con un error de conexión en el log, sin romper nada,
pero tampoco corre. Revisa en IB Gateway, en Configuración → "Lock and Exit", si está activado
"Auto restart" (deja la sesión viva entre el reinicio nocturno obligatorio de IBKR sin pedir
login de nuevo) — si no está, hay que abrir/loguear IB Gateway a mano antes de esa hora cada día
hasta que se resuelva de fondo (Fase 4, al pasar a un VPS con IBC).

**Instalación (una sola vez, en la Terminal de la Mac):**

```bash
cd "/Users/darwingcasana/Library/CloudStorage/OneDrive-AGRICOLADONRICARDO/My Information/Agentes IA/Agente de Bolsa/agente"
chmod +x run_daily.sh
cp com.agentedebolsa.runnerdiario.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.agentedebolsa.runnerdiario.plist
```

**Probarlo ahora** (sin esperar a las 6pm — corre el ciclo una vez, de inmediato):

```bash
launchctl start com.agentedebolsa.runnerdiario
sleep 5
cat ~/.agente_bolsa/runner_cron.log
cat ~/.agente_bolsa/runner_cron.err.log
```

**Ver el registro de cualquier día** (se va acumulando, no se borra solo):

```bash
cat ~/.agente_bolsa/runner_cron.log
```

**Desinstalarlo** (si algún día se quiere volver a correr solo a mano):

```bash
launchctl unload ~/Library/LaunchAgents/com.agentedebolsa.runnerdiario.plist
rm ~/Library/LaunchAgents/com.agentedebolsa.runnerdiario.plist
```

**Nota:** la Mac tiene que estar prendida (no dormida) a las 6pm para que `launchd` dispare la
tarea — si está dormida, `launchd` la corre en cuanto la Mac despierta, no la salta ni la
acumula. Si la Mac suele estar cerrada/dormida a esa hora, mejor ajustar el horario del `.plist`
a uno en que sí esté despierta, o revisar Preferencias del Sistema → Batería → "Programar" para
que despierte poco antes.

## Requisito de Python (Mac de Darwing)

`ib_async` exige **Python 3.10 o más nuevo**. El Python que trae macOS por defecto (el de las
herramientas de Xcode) suele ser 3.9 — hay que instalar uno más nuevo desde
[python.org/downloads/macos](https://www.python.org/downloads/macos/) (se usó 3.13.15). Después
de instalarlo, cerrar y volver a abrir la Terminal para que tome el Python nuevo.

## Nota operativa: sincronización de OneDrive

Al corregir el puerto del bróker (18-sep-2026) se encontró que un cambio ya escrito en el
`config.py` de la carpeta de OneDrive no había llegado al archivo real en la Mac — se quedó con
el valor anterior sin avisar de ningún error. Causa probable: una sincronización de OneDrive que
revirtió el archivo. Desde entonces, cada vez que se actualiza un archivo de código en esta
carpeta, se vuelve a leer justo después de escribirlo para confirmar que el cambio quedó, en vez
de asumirlo. Si alguna vez el comportamiento del código en la Mac no coincide con lo que se
esperaría del archivo más reciente, esto es lo primero a revisar.

Cada prueba corresponde a una regla del IPS o de `reglas-entrada-salida-estrategia.md`.
Si una regla cambia en los documentos, primero cambia (o agrega) la prueba, luego el código.

## Principio de diseño

La capa de riesgo es **determinística**: no usa IA, no consulta internet, no "opina".
Recibe una orden propuesta y el estado del portafolio y devuelve `approved=True/False`
con la lista de razones. Nada llega al bróker sin pasar por `RiskEngine.evaluate()`.

La capa de estrategia (`strategy.py`) tampoco decide nada por su cuenta: calcula la señal
técnica (régimen, cruce EMA, ATR, stops) para UN ticker a la vez y propone una orden con el
tamaño de la sección 5 de las reglas. Nunca aplica los topes de portafolio (exposición 50%,
máx. 8 posiciones, efectivo) — eso es trabajo exclusivo de `risk.py`.

**Nota importante encontrada al construir `strategy.py` (18-sep-2026):** el backtest de 10
años que Darwing aprobó simula los 7 ETFs *compitiendo* por 8 cupos y un tope de 50% de
exposición — cuando hay más señales que cupos, gana el ticker que aparece primero en la lista
(SPY, VTI, QQQ, EFA, VWO, XLK, XLF, en ese orden), no el "mejor". Esa arbitración es trabajo de
`runner.py` (el ciclo diario, todavía no construido) llamando a `risk.py`, no de `strategy.py`.
Por eso las pruebas de `strategy.py` verifican la señal ticker por ticker (contra los mismos
indicadores del motor de backtest, comprobados con diferencia 0.0), pero **no** reproducen
operación por operación el backtest completo — eso solo se puede verificar una vez exista
`runner.py`, y de todas formas la prueba real fuera de muestra es la Fase 4 (6 semanas de
paper trading), no una réplica exacta del backtest.
