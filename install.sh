#!/usr/bin/env bash
#
# Instalador automatico do Telemetry Portal (Debian / Ubuntu)
# Uso:  sudo bash install.sh
#
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Por favor rode como root:  sudo bash install.sh"
  exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
echo ">> Instalando o Telemetry Portal em: $DIR"

echo ">> [1/4] Instalando dependencias do sistema (python3, venv, pip)..."
apt-get update
apt-get install -y python3 python3-venv python3-pip

echo ">> [2/4] Criando ambiente virtual e instalando libs Python..."
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --upgrade pip
"$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"

echo ">> [3/4] Instalando o servico systemd (telemetry-portal)..."
sed "s|__DIR__|$DIR|g" "$DIR/telemetry.service" > /etc/systemd/system/telemetry-portal.service
systemctl daemon-reload
systemctl enable telemetry-portal.service
systemctl restart telemetry-portal.service

echo ">> [4/4] Verificando..."
sleep 2
systemctl --no-pager --lines=0 status telemetry-portal.service || true

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "============================================================"
echo "  Telemetry Portal instalado e rodando!"
echo ""
echo "  Acesse:  http://$IP:8080"
echo "  Login:   admin  /  admin"
echo ""
echo "  >> Troque a senha na aba 'Usuarios' apos o primeiro login."
echo "============================================================"
