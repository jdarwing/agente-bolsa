# Agente de Bolsa — código (Fase 2)

Paquete Python del agente. Cada carpeta/archivo corresponde a una capa del plan
(`plan-construccion-agente-bolsa.md`).

## Estado

| Módulo | Capa | Estado |
|---|---|---|
| `agente/config.py` | Universo (lista blanca) y límites del IPS en código | ✅ Entregado |
| `agente/risk.py` | **Capa de riesgo**: valida cada orden propuesta, frenos (breakers), kill switch | ✅ Entregado · 38 pruebas |
| `agente/audit.py` | Bitácora de auditoría (JSON Lines, solo-append) | ✅ Entregado |
| `agente/strategy.py` | Señales (SMA200, EMA20/50, ATR, stops, cooldown de re-entrada) → propuestas de orden | ✅ Entregado · 23 pruebas |
| `agente/broker.py` | Conexión a IB Gateway (paper 4002 / real bloqueado) + barras diarias (con volumen) vía IBKR | ✅ Entregado y probado contra IB Gateway real (18-sep-2026) |
| `agente/test_connection.py` | Prueba de humo: conecta, lee cuenta y barras de SPY | ✅ Corrida con éxito en la Mac de Darwing |
| `agente/runner.py` | Ciclo diario: leer precios → señales → riesgo → órdenes → bitácora → arbitraje de cupos → sleeve conservador (BIL) → rebalanceo → estado persistido → stop-límite residente | ✅ Entregado y corrido contra IB Gateway real · 22 pruebas |
| Respaldo de datos (Nasdaq.com CSV) | Se decidió NO hacer un `data.py` separado: `broker.daily_bars()` ya cubre la fuente principal (IBKR); el respaldo manual reutiliza `load_nasdaq_csv()` de `backtest/engine.py`, que ya existe | ✅ simplificado |
| Control de versiones | Git + GitHub (`github.com/jdarwing/agente-bolsa`, privado) — ver sección más abajo | ✅ Conectado (commit `81f58d5`) |

## Cómo correr las pruebas

```bash
cd agente
pip install pytest
python -m pytest tests/ -q
```

## `runner.py` — el ciclo diario

Une `strategy.py` + `risk.py` + `broker.py` + `audit.py`, y agrega tres cosas que ninguno de
esos módulos hace por su cuenta: el arbitraje de cupos entre los 7 tickers (orden de lista: SPY,
VTI, QQQ, EFA, VWO, XLK, XLF — gana el primero cuando hay más señales que cupos, igual que el
backtest aprobado), el sleeve conservador activo (ver más abajo), y el guard de cierre de
mercado. Guarda entre corridas lo que ni IBKR ni `risk.py`/`strategy.py` recuerdan por su cuenta
(stop vigente, ATR de entrada, cooldown de re-entrada, máximo histórico, referencias
diaria/semanal) en `~/.agente_bolsa/state.json` — **fuera de esta carpeta de OneDrive a
propósito** (ver la nota operativa más abajo: ese archivo cambia todos los días y aquí un dato
revertido sería grave, no solo un código viejo). La bitácora de auditoría del ciclo vive junto a
él, en `~/.agente_bolsa/audit.jsonl`.

Dos modos:

```bash
python3 -m agente.runner                     # modo lectura (por defecto): calcula y registra, no envía nada
python3 -m agente.runner --execute            # envía las órdenes aprobadas — solo con Read-Only ya desactivado
python3 -m agente.runner --allow-partial-bar  # salta el guard de cierre de mercado (solo para mirar estado antes de las 4pm ET, NUNCA para decidir una orden real)
```

Mientras "Read-Only API" siga activo en IB Gateway, correr en modo lectura día tras día y
revisar la bitácora es la forma de validar que las decisiones son las esperadas antes de
arriesgar dinero de papel. Sus 22 pruebas (`tests/test_runner.py`) usan un bróker de mentira
(`FakeBroker`) — nunca IB Gateway real, que solo se puede probar desde esta Mac.

**Siete brechas cerradas (evaluación de avance, 18-sep-2026):**

1. **Sleeve conservador activo (IPS §5/§6/§8, reglas §7):** el efectivo que sobra al final de
   cada ciclo (por encima de `config.CASH_BUFFER`, US$100) se compra en `config.SLEEVE_TICKER`
   (BIL) — antes se quedaba en efectivo, que IBKR no remunera. Si una entrada aprobada necesita
   más efectivo del que hay libre, `runner.py` vende BIL primero para cubrir el faltante, antes
   de evaluar las entradas del día.
2. **Tamaño de orden realista (`config.SIZING_EQUITY_CAP`, US$10,000):** mientras la cuenta
   paper tenga US$1,000,000 (muy por encima del capital real de la Etapa 1), el cálculo de
   cuántas unidades comprar usa el menor entre el equity real y este tope — los frenos de
   `risk.py` siguen evaluando contra el equity real, sin este tope.
3. **Guard de cierre de mercado:** `runner.py` ya no corre antes de las 4:15pm hora de Nueva
   York — antes de esa hora, la barra "de hoy" que reporta IBKR está a medio formar, y tratarla
   como el cierre real podría generar una señal sobre un precio que todavía se mueve.
4. **`traded_today` se llena de verdad (`risk.py`):** `RiskEngine.evaluate()` ahora registra la
   operación al aprobarla — la prohibición de ida-y-vuelta el mismo día (regla 10) ya se
   verifica por código, no solo por diseño del flujo.
5. **`--flow` para depósitos/retiros:** `python3 -m agente.runner --flow 5000` (depósito) o
   `--flow -2000` (retiro) — ya hecho en IBKR el mismo día, antes de correr el ciclo — evita que
   un movimiento de capital se vea como una ganancia o pérdida de mercado y dispare los frenos
   sin motivo (IPS §7.1).
6. **Rebalanceo por exceso de 10pp (IPS §8):** si la exposición a renta variable supera el tope
   (50%) en más de `config.REBALANCE_TRIGGER_EXCESS_PCT` (10pp) por revalorización, se recorta
   cada posición proporcionalmente hasta volver exactamente al tope.
7. **Filtro de liquidez re-verificado en cada entrada (reglas §7):** antes de sumar un candidato
   de entrada, se re-calcula su ADV en dólares de los últimos `config.LIQUIDITY_ADV_WINDOW_DAYS`
   días (20); por debajo de `config.LIQUIDITY_ADV_MIN_USD` (US$10M), la entrada se omite con
   aviso. **Pendiente de verificar contra datos reales:** la unidad en la que IBKR reporta
   `volume` para estas barras (acciones individuales vs. lotes de 100) — confirmar en la primera
   corrida real comparando el ADV calculado contra una referencia externa (Yahoo/IBKR TWS) antes
   de confiar en un rechazo de este filtro.

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

## Control de versiones (Git + GitHub)

El código vive en un repositorio Git **en esta misma carpeta** (`agente/.git/`) y en un
repositorio remoto privado en GitHub, `github.com/jdarwing/agente-bolsa` — conectado el
18-sep-2026, vía HTTPS con tu sesión de GitHub en la Terminal de la Mac. Historial hasta ahora:

- `4ba17f7` — Fase 2: capa de riesgo, estrategia, broker, ciclo diario y pruebas (69→74).
- `a73c468` — Documentar sleeve conservador, tope de dimensionamiento, guard de cierre de
  mercado y control de versiones.
- `81f58d5` — Cierra los 4 pendientes menores de la evaluación de avance: `traded_today`,
  `--flow`, rebalanceo por exceso de 10pp, filtro de liquidez (69→83 pruebas en total).

Cada vez que se entregue código nuevo a esta carpeta, hace falta un `git add -A && git commit -m "..."`
(yo puedo hacerlo localmente desde la nube) seguido de un `git push` **desde tu propia Terminal**
— el `push` necesita las credenciales de GitHub guardadas en tu Mac, que no están disponibles
desde la sesión en la nube. Si alguna vez un commit mío queda sin subir, basta con:

```bash
cd "/Users/darwingcasana/Library/CloudStorage/OneDrive-AGRICOLADONRICARDO/My Information/Agentes IA/Agente de Bolsa/agente"
git push origin main
```

Y si algún día OneDrive revierte un archivo sin avisar (ver la nota operativa más abajo),
`git status`/`git diff`/`git log` lo detecta de inmediato — comparando contra el último commit,
no contra la memoria de nadie.

**Nota de riesgo:** el repositorio Git queda DENTRO de la carpeta sincronizada por OneDrive (a
propósito, para no duplicar la carpeta) — y ya se vio, al crearlo, el mismo tipo de bloqueo de
archivos ("Operation not permitted" en archivos temporales de Git, y una vez un `.git/HEAD.lock`
trabado) que afecta a los demás archivos de esta carpeta. No ha impedido ningún commit hasta
ahora, pero si `git status`/`git log` alguna vez se ven raros, es la primera sospecha — igual
que con cualquier otro archivo de esta carpeta.

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

## Pendiente conocido: unidad de `volume` de IBKR sin verificar (18-sep-2026)

Los cuatro pendientes menores de la evaluación de avance (`traded_today`, `--flow`, rebalanceo
por exceso de 10pp, filtro de liquidez por entrada) ya están cerrados — ver la sección
"Siete brechas cerradas" más arriba. Queda un único pendiente de esa misma ronda: `broker.py`
pide la columna `volume` en las barras diarias para el filtro de liquidez, pero **todavía no se
confirmó contra datos reales** en qué unidad la reporta IBKR para acciones/ETFs de EE.UU.
(podría venir en acciones individuales o en lotes de 100). La primera corrida real con
`runner.py` actualizado debe confirmar esto comparando el ADV en dólares que calcula contra una
referencia externa (p.ej. Yahoo Finance o el propio IB TWS) antes de confiar en un rechazo del
filtro de liquidez — ver `evaluacion-avance-fase2.md` en el proyecto.
