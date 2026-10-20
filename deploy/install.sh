#!/usr/bin/env bash
# 在 Linux 服务器上装成 systemd 服务（不用 Docker 的那条路）。
#
#   sudo bash deploy/install.sh
#
# 可覆盖的环境变量：
#   APP_DIR=/opt/biliduty  DATA_DIR=/var/lib/biliduty  ENV_FILE=/etc/biliduty/env  SERVICE_USER=biliduty
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/biliduty}
DATA_DIR=${DATA_DIR:-/var/lib/biliduty}
ENV_FILE=${ENV_FILE:-/etc/biliduty/env}
SERVICE_USER=${SERVICE_USER:-biliduty}
UNIT_NAME=${UNIT_NAME:-bili-duty.service}

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${PYTHON:-python3}

if [[ ${EUID} -ne 0 ]]; then
  echo "要以 root 跑（要建用户、装 systemd 单元）：sudo bash deploy/install.sh" >&2
  exit 1
fi

"${PYTHON}" - <<'PY'
import sys
if sys.version_info < (3, 10):
    sys.exit("需要 Python 3.10+，现在是 %s" % sys.version.split()[0])
PY

echo "==> 用户与目录"
id -u "${SERVICE_USER}" >/dev/null 2>&1 || useradd --system --create-home --home-dir "${APP_DIR}" \
  --shell /usr/sbin/nologin "${SERVICE_USER}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}" "${DATA_DIR}"
install -d -m 750 -o root -g "${SERVICE_USER}" "$(dirname "${ENV_FILE}")"

echo "==> 虚拟环境 + 安装"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  "${PYTHON}" -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet "${SRC_DIR}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${APP_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "==> 生成密钥文件模板 ${ENV_FILE}"
  cat >"${ENV_FILE}" <<'ENV'
# 必填
DEEPSEEK_API_KEY=
# 控制页写操作口令（openssl rand -hex 24）
BILIDUTY_TOKEN=
ENV
  chmod 600 "${ENV_FILE}"
  chown root:"${SERVICE_USER}" "${ENV_FILE}"
  echo "   ⚠️ 先填这两个值，再 systemctl start ${UNIT_NAME}"
fi

echo "==> systemd"
install -m 644 "${SRC_DIR}/deploy/bili-duty.service" "/etc/systemd/system/${UNIT_NAME}"
systemctl daemon-reload
systemctl enable "${UNIT_NAME}"

cat <<EOF

装好了。接下来三步：

  1. 填密钥：  sudo \$EDITOR ${ENV_FILE}
  2. 扫码：    sudo -u ${SERVICE_USER} env BILIDUTY_DATA=${DATA_DIR} \\
                 ${APP_DIR}/.venv/bin/python -m biliduty login
               （二维码 PNG 在 ${DATA_DIR}/qrcode_login.png，用 scp 拉下来用手机 B站 扫）
  3. 起服务：  sudo systemctl start ${UNIT_NAME} && journalctl -u ${UNIT_NAME} -f

控制页：http://127.0.0.1:8787（建议 ssh -L 8787:127.0.0.1:8787 转发着看）
数据：  ${DATA_DIR}（config.json / seen.md / out/ / digest/ / duty.log）
EOF
