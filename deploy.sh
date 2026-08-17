#!/usr/bin/env bash
#
# Instala / actualiza el Dashboard de Radioenlaces en un Ubuntu.
#
# Uso, desde la carpeta del repositorio ya clonado:
#     sudo bash deploy.sh
#
# Es idempotente: se puede volver a lanzar para actualizar el codigo sin perder
# ni la configuracion (radios.json) ni el historico (radioenlaces.db).

set -euo pipefail

DESTINO="/opt/dashboard-radioenlaces"
SERVICIO="dashboard-radioenlaces"
USUARIO="dashboard-radio"
ORIGEN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

verde()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
amar()   { printf '\033[0;33m%s\033[0m\n' "$*"; }
rojo()   { printf '\033[0;31m%s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  rojo "Hay que ejecutarlo como root:  sudo bash deploy.sh"
  exit 1
fi

if [[ "$ORIGEN" == "$DESTINO" ]]; then
  rojo "No clones el repositorio dentro de $DESTINO. Usa por ejemplo ~/dashboard-radioenlaces"
  exit 1
fi

# --- comprobaciones previas -------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  amar "python3 no esta instalado. Instalandolo..."
  apt-get update -qq && apt-get install -y python3
fi
verde "Python: $(python3 --version)"

for archivo in proxy.py dashboard.html radios.json.example "${SERVICIO}.service"; do
  if [[ ! -f "$ORIGEN/$archivo" ]]; then
    rojo "Falta $archivo en $ORIGEN. ¿Has clonado el repositorio completo?"
    exit 1
  fi
done

# --- usuario de servicio ----------------------------------------------------
if ! id -u "$USUARIO" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$USUARIO"
  verde "Usuario de servicio creado: $USUARIO"
fi

# --- archivos ---------------------------------------------------------------
mkdir -p "$DESTINO"
install -m 644 "$ORIGEN/proxy.py"             "$DESTINO/proxy.py"
install -m 644 "$ORIGEN/dashboard.html"       "$DESTINO/dashboard.html"
install -m 644 "$ORIGEN/radios.json.example"  "$DESTINO/radios.json.example"
for opcional in README.md ejemplo_respuesta.json; do
  if [[ -f "$ORIGEN/$opcional" ]]; then
    install -m 644 "$ORIGEN/$opcional" "$DESTINO/$opcional"
  fi
done
verde "Codigo copiado en $DESTINO"

if [[ -f "$DESTINO/radios.json" ]]; then
  verde "radios.json ya existe: se conserva tu configuracion"
else
  install -m 640 "$ORIGEN/radios.json.example" "$DESTINO/radios.json"
  amar "Creado $DESTINO/radios.json a partir del ejemplo. Revisa las IPs antes de continuar."
fi

chown -R "$USUARIO:$USUARIO" "$DESTINO"
chmod 640 "$DESTINO/radios.json"

# --- servicio systemd -------------------------------------------------------
install -m 644 "$ORIGEN/${SERVICIO}.service" "/etc/systemd/system/${SERVICIO}.service"
systemctl daemon-reload
systemctl enable "$SERVICIO"
# Si el arranque falla no cortamos aqui: mas abajo damos un mensaje util
systemctl restart "$SERVICIO" || true
verde "Servicio $SERVICIO instalado y habilitado en el arranque"

# --- comprobacion -----------------------------------------------------------
PUERTO="$(python3 - "$DESTINO/radios.json" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("puerto", 8080))
except Exception:
    print(8080)
PY
)"

sleep 3
echo
if systemctl is-active --quiet "$SERVICIO"; then
  verde "Estado: activo"
else
  rojo "El servicio no ha arrancado. Mira el log:  journalctl -u $SERVICIO -n 50 --no-pager"
  exit 1
fi

IP="$( (hostname -I 2>/dev/null || true) | awk '{print $1}' || true)"
echo
verde "===================================================================="
verde " Dashboard disponible en:  http://${IP:-<ip-del-servidor>}:${PUERTO}"
verde "===================================================================="
echo
echo "Comandos utiles:"
echo "  journalctl -u $SERVICIO -f          # ver el log en directo"
echo "  systemctl status $SERVICIO          # estado del servicio"
echo "  systemctl restart $SERVICIO         # reiniciar tras tocar radios.json"
echo "  nano $DESTINO/radios.json           # añadir o cambiar radios"
echo
echo "Prueba rapida de lectura de los radios (sin tocar el servicio):"
echo "  sudo -u $USUARIO python3 $DESTINO/proxy.py --once"
echo
