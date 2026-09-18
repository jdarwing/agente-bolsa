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
| `agente/runner.py` | Ciclo diario: leer precios → señales → riesgo → órdenes → bitácora → arbitraje de cupos → sleeve conservador (BIL) → estado persistido → stop-límite residente | ✅ Entregado y corrido contra IB Gateway real · 15 pruebas |
| Respaldo de datos (Nasdaq.com CSV) | Se decidió NO hacer un `data.py` separado: `broker.daily_bars()` ya cubre la fuente principal (IBKR); el respaldo manual reutiliza `load_nasdaq_csv()` de `backtest/engine.py`, que ya existe | ✅ simplificado |
| Control de versiones | Repositorio Git local creado en esta misma carpeta (18-sep-2026, primer commit `4ba17f7`) — falta conectarlo a GitHub, ver sección más abajo | 🔄 en curso |

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
arriesgar dinero de papel. Sus 15 pruebas (`tests/test_runner.py`) usan un bróker de mentira
(`FakeBroker`) — nunca IB Gateway real, que solo se puede probar desde esta Mac.

**Tres brechas cerradas (evaluación de avance, 18-sep-2026):**

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

Hasta el 18-sep-2026 este código vivía solo en la carpeta de OneDrive, sin historial — cualquier
reversión de sincronización (ver la nota operativa más abajo) podía perder un cambio sin dejar
rastro de qué era la versión anterior. Se creó un repositorio Git **en esta misma carpeta**
(`agente/.git/`), con un primer commit (`4ba17f7`) que incluye todo el código y las pruebas
hasta esa fecha. Falta conectarlo a un repositorio remoto en GitHub — un solo paso, desde la
Terminal de la Mac (esto sí requiere tu sesión de GitHub, no se puede hacer desde la nube):

1. En [github.com/new](https://github.com/new): nombre (ej. `agente-bolsa`), **Private**, sin
   marcar "Add a README" ni "Add .gitignore" (el repositorio local ya existe con su propio
   historial).
2. En la Terminal:

```bash
cd "/Users/darwingcasana/Library/CloudStorage/OneDrive-AGRICOLADONRICARDO/My Information/Agentes IA/Agente de Bolsa/agente"
git log --oneline   # debería mostrar "4ba17f7 Fase 2: ..." — confirma que ves el mismo repositorio
git remote add origin https://github.com/<tu-usuario>/agente-bolsa.git
git push -u origin main
```

Con eso, cada vez que se entregue código nuevo a esta carpeta, un `git add -A && git commit -m "..." && git push`
desde la Terminal (o pedírmelo a mí, yo puedo hacer `git add`/`commit` locales, pero el `push`
final a GitHub necesita tu sesión) deja el cambio en GitHub con fecha e historial — y si algún
día OneDrive revierte un archivo sin avisar, `git diff`/`git log` lo detecta de inmediato.

**Nota de riesgo:** el repositorio Git queda DENTRO de la carpeta sincronizada por OneDrive (a
propósito, para no duplicar la carpeta) — y ya se vio, al crearlo, el mismo tipo de bloqueo de
archivos ("Operation not permitted" en archivos temporales de Git) que afecta a los demás
archivos de esta carpeta. No impidió el primer commit, pero si `git status`/`git log` alguna vez
se ven raros, es la primera sospecha — igual que con cualquier otro archivo de esta carpeta.

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

## Pendiente conocido: `traded_today` nunca se llena (encontrado 18-sep-2026)

`risk.PortfolioState.traded_today` existe y `RiskEngine.evaluate()` lo consulta (regla 10: no
comprar y vender el mismo instrumento el mismo día), pero **ningún código lo llena todavía** —
ni `runner.py` lo actualiza al aprobar/enviar una orden. En la práctica esto no puede pasar hoy
(el ciclo corre una vez al día y una posición no puede tener a la vez una entrada y una salida
pendientes), así que no es un riesgo activo, pero la regla no está realmente verificada por
código, solo por diseño del flujo. Anotado para cerrarlo junto con el resto de reglas escritas
y aún no implementadas (filtro de liquidez por entrada, rebalanceo por exceso de 10pp,
`record_flow` para depósitos/retiros) — ver `evaluacion-avance-fase2.md` en el proyecto.
