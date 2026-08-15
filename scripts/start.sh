#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"

expand_path() {
  local value="$1"
  case "${value}" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${value#\~/}" ;;
    /*) printf '%s\n' "${value}" ;;
    *) printf '%s/%s\n' "${PROJECT_ROOT}" "${value#./}" ;;
  esac
}

resolve_executable() {
  local candidate="$1"
  local expanded=""
  case "${candidate}" in
    */*)
      expanded="$(expand_path "${candidate}")"
      [ -x "${expanded}" ] || return 1
      printf '%s\n' "${expanded}"
      ;;
    *) command -v "${candidate}" 2>/dev/null ;;
  esac
}

if [ -n "${PYTHON_BIN:-}" ]; then
  app_python="$(resolve_executable "${PYTHON_BIN}" || true)"
elif [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
  app_python="${PROJECT_ROOT}/.venv/bin/python"
else
  app_python="$(command -v python3 2>/dev/null || true)"
fi
if [ -z "${app_python}" ] || [ ! -x "${app_python}" ]; then
  printf '错误：找不到可用 Python。请先运行 ./scripts/setup.sh。\n' >&2
  exit 1
fi
if ! "${app_python}" -c 'import fastapi, httpx, pydantic, uvicorn; import dotenv' >/dev/null 2>&1; then
  printf '错误：后端依赖不完整。请先运行 ./scripts/setup.sh。\n' >&2
  exit 1
fi

PATH="$(dirname "${app_python}"):${PATH}"
export PATH

config_value() {
  "${app_python}" - "${ENV_FILE}" "$1" "$2" <<'PY'
import os
import sys

from dotenv import dotenv_values

env_file, name, fallback = sys.argv[1:]
values = dotenv_values(env_file) if os.path.isfile(env_file) else {}
value = os.environ[name] if name in os.environ else values.get(name, fallback)
if value is None:
    value = fallback
sys.stdout.write(str(value))
PY
}

host="$(config_value HOST 127.0.0.1)"
port="$(config_value PORT 8000)"
work_dir="$(expand_path "$(config_value WORK_DIR ./work)")"
cosyvoice_url="$(config_value COSYVOICE_URL http://127.0.0.1:50001)"
cosyvoice_host="$(config_value COSYVOICE_HOST 127.0.0.1)"
cosyvoice_port="$(config_value COSYVOICE_PORT 50001)"
cosyvoice_env="$(config_value COSYVOICE_CONDA_ENV cosyvoice)"
cosyvoice_timeout="$(config_value COSYVOICE_START_TIMEOUT 300)"
conda_bin="$(config_value CONDA_BIN conda)"

for port_value in "${port}" "${cosyvoice_port}"; do
  case "${port_value}" in
    ''|*[!0-9]*)
      printf '错误：端口必须为数字，当前值为 %s。\n' "${port_value}" >&2
      exit 1
      ;;
  esac
  if [ "${port_value}" -lt 1 ] || [ "${port_value}" -gt 65535 ]; then
    printf '错误：端口必须在 1–65535 之间，当前值为 %s。\n' "${port_value}" >&2
    exit 1
  fi
done
case "${cosyvoice_timeout}" in
  ''|*[!0-9]*)
    printf '错误：COSYVOICE_START_TIMEOUT 必须是整数秒。\n' >&2
    exit 1
    ;;
esac

mkdir -p "${work_dir}/logs"
cosyvoice_log="${work_dir}/logs/cosyvoice_server.log"
cosyvoice_pid=""
cosyvoice_started=0

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  if [ "${cosyvoice_started}" -eq 1 ] && [ -n "${cosyvoice_pid}" ]; then
    if kill -0 "${cosyvoice_pid}" 2>/dev/null; then
      printf '\n正在停止本次启动的 CosyVoice 服务（PID %s）…\n' "${cosyvoice_pid}"
      kill -TERM "${cosyvoice_pid}" 2>/dev/null || true
      wait "${cosyvoice_pid}" 2>/dev/null || true
    fi
  fi
  exit "${status}"
}

handle_signal() {
  exit "$1"
}

trap cleanup EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM HUP

cosyvoice_health_state() {
  "${app_python}" - "${cosyvoice_url}" <<'PY'
import json
import sys
import urllib.error
import urllib.request

url = sys.argv[1].rstrip("/") + "/health"
try:
    # Local model traffic must never be routed through HTTP(S)_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:
        payload = json.load(response)
except Exception:
    print("unreachable")
else:
    print("ready" if payload.get("loaded") is True else "not_ready")
PY
}

if [ "${SKIP_COSYVOICE:-0}" = "1" ]; then
  printf 'SKIP_COSYVOICE=1：不检查或启动 CosyVoice；请在 WebUI 关闭中文配音。\n'
else
  health_state="$(cosyvoice_health_state)"
  if [ "${health_state}" = "ready" ]; then
    printf '复用已运行的 CosyVoice 服务：%s\n' "${cosyvoice_url}"
  elif [ "${health_state}" = "not_ready" ]; then
    printf '错误：%s 已有服务响应，但 CosyVoice 模型未就绪。\n' "${cosyvoice_url}" >&2
    printf '请先修复或停止该服务；也可用 SKIP_COSYVOICE=1 启动纯字幕模式。\n' >&2
    exit 1
  else
    if ! conda_path="$(resolve_executable "${conda_bin}")"; then
      printf '错误：找不到 Conda，无法自动启动 CosyVoice。\n' >&2
      printf '如仅需字幕，请运行 SKIP_COSYVOICE=1 ./scripts/start.sh。\n' >&2
      exit 1
    fi
    if ! "${conda_path}" env list 2>/dev/null | awk -v wanted="${cosyvoice_env}" '$1 == wanted { found = 1 } END { exit !found }'; then
      printf '错误：找不到 Conda 环境 %s。\n' "${cosyvoice_env}" >&2
      exit 1
    fi

    export COSYVOICE_HOST="${cosyvoice_host}"
    export COSYVOICE_PORT="${cosyvoice_port}"
    cosyvoice_prefix="$("${conda_path}" env list 2>/dev/null | \
      awk -v wanted="${cosyvoice_env}" '$1 == wanted { print $NF; exit }')"
    cosyvoice_python="${cosyvoice_prefix}/bin/python"
    if [ ! -x "${cosyvoice_python}" ]; then
      printf '错误：无法解析 Conda 环境 %s 的 Python 路径。\n' "${cosyvoice_env}" >&2
      exit 1
    fi
    printf '启动 CosyVoice 服务；日志：%s\n' "${cosyvoice_log}"
    "${cosyvoice_python}" "${PROJECT_ROOT}/services/cosyvoice_server.py" \
      >>"${cosyvoice_log}" 2>&1 &
    cosyvoice_pid=$!
    cosyvoice_started=1

    wait_started=${SECONDS}
    next_report=10
    while true; do
      if ! kill -0 "${cosyvoice_pid}" 2>/dev/null; then
        printf '错误：CosyVoice 启动进程已退出。最近日志：\n' >&2
        tail -n 40 "${cosyvoice_log}" >&2 || true
        exit 1
      fi
      health_state="$(cosyvoice_health_state)"
      if [ "${health_state}" = "ready" ]; then
        printf 'CosyVoice 模型已就绪：%s\n' "${cosyvoice_url}"
        break
      fi
      if [ "${health_state}" = "not_ready" ]; then
        printf '错误：CosyVoice 服务已启动，但模型加载失败。最近日志：\n' >&2
        tail -n 40 "${cosyvoice_log}" >&2 || true
        exit 1
      fi
      elapsed=$((SECONDS - wait_started))
      if [ "${elapsed}" -ge "${cosyvoice_timeout}" ]; then
        printf '错误：等待 CosyVoice 超过 %s 秒。最近日志：\n' "${cosyvoice_timeout}" >&2
        tail -n 40 "${cosyvoice_log}" >&2 || true
        exit 1
      fi
      if [ "${elapsed}" -ge "${next_report}" ]; then
        printf 'CosyVoice 正在加载模型（已等待 %s 秒）…\n' "${elapsed}"
        next_report=$((next_report + 10))
      fi
      sleep 1
    done
  fi
fi

cd "${PROJECT_ROOT}"
printf '启动 WebUI：http://%s:%s\n' "${host}" "${port}"
uvicorn_args=(backend.main:app --host "${host}" --port "${port}")
if [ "${UVICORN_RELOAD:-0}" = "1" ]; then
  uvicorn_args+=(--reload)
fi

set +e
"${app_python}" -m uvicorn "${uvicorn_args[@]}"
app_status=$?
set -e
exit "${app_status}"
