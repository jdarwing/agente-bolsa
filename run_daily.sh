#!/bin/zsh -l
# Corre el ciclo diario del agente de bolsa (modo lectura, SIN --execute) y deja un registro
# con fecha/hora en el log. Pensado para ejecutarse solo, vía launchd — ver
# com.agentedebolsa.runnerdiario.plist y las instrucciones de instalación en README.md.
#
# "-l" en el shebang: carga el perfil de login (.zprofile / .zshrc), para que "python3" resuelva
# al mismo Python 3.13 que usas en la Terminal — launchd por sí solo no lee tu perfil de shell,
# así que sin esto correría con el Python viejo de macOS (3.9) y fallaría por versión.
set -e
cd "/Users/darwingcasana/Library/CloudStorage/OneDrive-AGRICOLADONRICARDO/My Information/Agentes IA/Agente de Bolsa/agente"
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
python3 -m agente.runner
