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

python_is_supported() {
  "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' \
    >/dev/null 2>&1
}

python_version() {
  "$1" -c 'import platform; print(platform.python_version())' 2>/dev/null
}

select_python() {
  local candidate=""
  local resolved=""
  local seen_paths=":"

  if [ -n "${PYTHON_BIN:-}" ]; then
    if ! resolved="$(resolve_executable "${PYTHON_BIN}")"; then
      printf '错误：PYTHON_BIN 指向的可执行文件不存在：%s\n' "${PYTHON_BIN}" >&2
      return 1
    fi
    if ! python_is_supported "${resolved}"; then
      printf '错误：PYTHON_BIN=%s 是 Python %s，项目需要 Python 3.10 或更高版本。\n' \
        "${resolved}" "$(python_version "${resolved}" || printf '未知')" >&2
      return 1
    fi
    printf '%s\n' "${resolved}"
    return 0
  fi

  # macOS 自带的 /usr/bin/python3 可能仍是 3.9，而 Homebrew/Conda 的新版
  # Python 常以 `python` 或带版本的名称安装。逐个校验，不仅依赖命令名。
  for candidate in python python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    resolved="$(resolve_executable "${candidate}" || true)"
    [ -n "${resolved}" ] || continue
    case "${seen_paths}" in
      *":${resolved}:"*) continue ;;
    esac
    seen_paths="${seen_paths}${resolved}:"
    if python_is_supported "${resolved}"; then
      printf '%s\n' "${resolved}"
      return 0
    fi
  done

  printf '错误：找不到 Python 3.10 或更高版本。macOS 可运行 `brew install python`，\n' >&2
  printf '或用 PYTHON_BIN=/绝对路径/python ./scripts/setup.sh 指定已安装的 Python。\n' >&2
  return 1
}

if ! python_path="$(select_python)"; then
  exit 1
fi
printf '使用 Python：%s（%s）\n' "${python_path}" "$(python_version "${python_path}")"

venv_dir="$(expand_path "${VENV_DIR:-.venv}")"
case "${venv_dir}" in
  "/"|"${HOME}"|"${PROJECT_ROOT}")
    printf '错误：VENV_DIR 不能是根目录、用户主目录或项目根目录：%s\n' "${venv_dir}" >&2
    exit 1
    ;;
esac

venv_backup=""
venv_rebuild_reason=""
if [ -e "${venv_dir}" ] || [ -L "${venv_dir}" ]; then
  if [ ! -x "${venv_dir}/bin/python" ]; then
    venv_rebuild_reason="现有路径不是可用的 Python 虚拟环境"
  elif ! python_is_supported "${venv_dir}/bin/python"; then
    venv_rebuild_reason="现有虚拟环境使用 Python $(python_version "${venv_dir}/bin/python" || printf '未知')，低于要求的 3.10"
  fi
fi

if [ -n "${venv_rebuild_reason}" ]; then
  backup_base="${venv_dir}.backup-$(date '+%Y%m%d-%H%M%S')"
  venv_backup="${backup_base}"
  backup_index=1
  while [ -e "${venv_backup}" ] || [ -L "${venv_backup}" ]; do
    venv_backup="${backup_base}-${backup_index}"
    backup_index=$((backup_index + 1))
  done
  printf '需要重建虚拟环境：%s。\n' "${venv_rebuild_reason}"
  printf '为保留旧环境，将其移动到：%s\n' "${venv_backup}"
  mv "${venv_dir}" "${venv_backup}"
fi

if [ ! -x "${venv_dir}/bin/python" ]; then
  printf '创建虚拟环境：%s\n' "${venv_dir}"
  mkdir -p "$(dirname "${venv_dir}")"
  if ! "${python_path}" -m venv "${venv_dir}"; then
    failed_venv="${venv_dir}.failed-$(date '+%Y%m%d-%H%M%S')"
    if [ -e "${venv_dir}" ] || [ -L "${venv_dir}" ]; then
      mv "${venv_dir}" "${failed_venv}"
      printf '未完成的新环境已保留在：%s\n' "${failed_venv}" >&2
    fi
    if [ -n "${venv_backup}" ]; then
      mv "${venv_backup}" "${venv_dir}"
      printf '已将原虚拟环境恢复到：%s\n' "${venv_dir}" >&2
    fi
    printf '错误：无法使用 %s 创建虚拟环境。\n' "${python_path}" >&2
    exit 1
  fi
else
  printf '复用虚拟环境：%s（Python %s）\n' \
    "${venv_dir}" "$(python_version "${venv_dir}/bin/python")"
fi

printf '安装后端依赖…\n'
"${venv_dir}/bin/python" -m pip install --upgrade pip setuptools wheel
"${venv_dir}/bin/python" -m pip install --upgrade -r "${PROJECT_ROOT}/requirements.txt"

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
