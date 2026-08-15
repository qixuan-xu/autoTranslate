#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

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

python_candidate="${PYTHON_BIN:-python3}"
if ! python_path="$(resolve_executable "${python_candidate}")"; then
  printf '错误：找不到 %s。请先安装 Python 3.9 或更高版本。\n' "${python_candidate}" >&2
  exit 1
fi
if ! "${python_path}" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
  printf '错误：需要 Python 3.9 或更高版本。\n' >&2
  exit 1
fi

venv_dir="$(expand_path "${VENV_DIR:-.venv}")"
if [ ! -x "${venv_dir}/bin/python" ]; then
  printf '创建虚拟环境：%s\n' "${venv_dir}"
  "${python_path}" -m venv "${venv_dir}"
else
  printf '复用虚拟环境：%s\n' "${venv_dir}"
fi

printf '安装后端依赖…\n'
"${venv_dir}/bin/python" -m pip install --upgrade pip setuptools wheel
"${venv_dir}/bin/python" -m pip install -r "${PROJECT_ROOT}/requirements.txt"

if [ ! -e "${PROJECT_ROOT}/.env" ]; then
  cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
  printf '已从 .env.example 创建 .env；请按本机路径和模型配置修改。\n'
else
  printf '保留已有 .env，不会覆盖。\n'
fi

if [ "${SKIP_COSYVOICE:-0}" = "1" ]; then
  printf 'SKIP_COSYVOICE=1：跳过 CosyVoice 服务依赖检查与安装。\n'
else
  conda_candidate="${CONDA_BIN:-conda}"
  conda_env="${COSYVOICE_CONDA_ENV:-cosyvoice}"
  if conda_path="$(resolve_executable "${conda_candidate}")"; then
    if "${conda_path}" env list 2>/dev/null | awk -v wanted="${conda_env}" '$1 == wanted { found = 1 } END { exit !found }'; then
      conda_prefix="$("${conda_path}" env list 2>/dev/null | \
        awk -v wanted="${conda_env}" '$1 == wanted { print $NF; exit }')"
      conda_python="${conda_prefix}/bin/python"
      if [ ! -x "${conda_python}" ]; then
        printf '提醒：无法解析 Conda 环境 %s 的 Python 路径。\n' "${conda_env}" >&2
      elif "${conda_python}" -c \
        'import fastapi, pydantic, soundfile, uvicorn; raise SystemExit(int(pydantic.VERSION.split(".")[0]) < 2)' \
        >/dev/null 2>&1; then
        printf 'CosyVoice 环境中的 HTTP 服务依赖已就绪。\n'
      else
        printf '向 Conda 环境 %s 安装 CosyVoice HTTP 服务依赖…\n' "${conda_env}"
        "${conda_python}" -m pip install \
          'fastapi>=0.115,<1' \
          'uvicorn[standard]>=0.30,<1' \
          'pydantic>=2.8,<3' \
          'python-dotenv>=1.0,<2' \
          'soundfile>=0.12,<1'
      fi
    else
      printf '提醒：未找到 Conda 环境 %s，已跳过 CosyVoice 服务依赖安装。\n' "${conda_env}" >&2
    fi
  else
    printf '提醒：未找到 Conda；已跳过 CosyVoice 服务依赖安装。\n' >&2
  fi
fi

chmod +x \
  "${SCRIPT_DIR}/setup.sh" \
  "${SCRIPT_DIR}/start.sh" \
  "${SCRIPT_DIR}/check_environment.sh"

printf '\n依赖安装完成，开始检查外部工具。\n'
if ! "${SCRIPT_DIR}/check_environment.sh"; then
  printf '\nPython 依赖已安装，但仍缺少上方标记为 FAIL 的系统工具。\n' >&2
  exit 1
fi

printf '\n配置确认后运行：./scripts/start.sh\n'
