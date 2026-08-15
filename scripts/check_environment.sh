#!/usr/bin/env bash

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"

failures=0
warnings=0

pass() {
  printf '  [OK] %s\n' "$1"
}

warn() {
  warnings=$((warnings + 1))
  printf '  [WARN] %s\n' "$1"
}

fail() {
  failures=$((failures + 1))
  printf '  [FAIL] %s\n' "$1"
}

dotenv_value() {
  local key="$1"
  local fallback="$2"
  local value=""

  if printenv "${key}" >/dev/null 2>&1; then
    printenv "${key}"
    return
  fi
  if [ -f "${ENV_FILE}" ]; then
    value="$(awk -v wanted="${key}" '
      {
        line = $0
        sub(/\r$/, "", line)
        sub(/^[ \t]*/, "", line)
        if (line == "" || line ~ /^#/) next
        sub(/^export[ \t]+/, "", line)
        equals = index(line, "=")
        if (!equals) next
        name = substr(line, 1, equals - 1)
        gsub(/[ \t]+$/, "", name)
        if (name != wanted) next
        result = substr(line, equals + 1)
        sub(/^[ \t]*/, "", result)
        sub(/[ \t]*$/, "", result)
        print result
        exit
      }
    ' "${ENV_FILE}")"
    case "${value}" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
  fi
  if [ -n "${value}" ]; then
    printf '%s\n' "${value}"
  else
    printf '%s\n' "${fallback}"
  fi
}

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
  if [ -z "${candidate}" ]; then
    return 1
  fi
  case "${candidate}" in
    */*)
      expanded="$(expand_path "${candidate}")"
      if [ -x "${expanded}" ]; then
        printf '%s\n' "${expanded}"
        return 0
      fi
      ;;
    *)
      command -v "${candidate}" 2>/dev/null
      return $?
      ;;
  esac
  return 1
}

printf 'AutoTranslate 环境检查\n'
printf '项目目录：%s\n\n' "${PROJECT_ROOT}"

if [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
  PATH="${PROJECT_ROOT}/.venv/bin:${PATH}"
  export PATH
fi

python_candidate="${PYTHON_BIN:-}"
if [ -z "${python_candidate}" ] && [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
  python_candidate="${PROJECT_ROOT}/.venv/bin/python"
elif [ -z "${python_candidate}" ]; then
  python_candidate="python3"
fi

printf '核心工具\n'
if python_path="$(resolve_executable "${python_candidate}")"; then
  python_version="$("${python_path}" --version 2>&1)"
  if "${python_path}" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    pass "Python：${python_path}（${python_version}）"
  else
    fail "需要 Python 3.9 或更高版本；当前为 ${python_version}"
  fi
else
  fail "找不到 Python。请安装 Python 3，并运行 scripts/setup.sh"
fi

ffmpeg_bin="$(dotenv_value FFMPEG_BIN ffmpeg)"
if ffmpeg_path="$(resolve_executable "${ffmpeg_bin}")"; then
  pass "FFmpeg：${ffmpeg_path}"
else
  fail "找不到 FFmpeg（FFMPEG_BIN=${ffmpeg_bin}）"
fi

ffprobe_bin="$(dotenv_value FFPROBE_BIN ffprobe)"
if ffprobe_path="$(resolve_executable "${ffprobe_bin}")"; then
  pass "ffprobe：${ffprobe_path}"
else
  fail "找不到 ffprobe（FFPROBE_BIN=${ffprobe_bin}）"
fi

whisper_bin="$(dotenv_value WHISPER_BIN whisper)"
if whisper_path="$(resolve_executable "${whisper_bin}")"; then
  pass "Whisper CLI：${whisper_path}"
else
  fail "找不到 Whisper CLI（WHISPER_BIN=${whisper_bin}）"
fi

printf '\n可选输入与高级功能\n'
ytdlp_bin="$(dotenv_value YTDLP_BIN yt-dlp)"
if ytdlp_path="$(resolve_executable "${ytdlp_bin}")"; then
  ytdlp_version="$("${ytdlp_path}" --version 2>/dev/null || true)"
  pass "yt-dlp：${ytdlp_path}${ytdlp_version:+（${ytdlp_version}）}"
else
  warn "找不到 yt-dlp；本地视频仍可用，但 YouTube 下载不可用"
fi

if demucs_path="$(resolve_executable demucs)"; then
  pass "Demucs：${demucs_path}"
else
  warn "未安装 Demucs；Fast 模式可用，Demucs 人声分离不可用"
fi

printf '\nCosyVoice3\n'
conda_bin="$(dotenv_value CONDA_BIN conda)"
conda_env="$(dotenv_value COSYVOICE_CONDA_ENV cosyvoice)"
conda_path=""
if conda_path="$(resolve_executable "${conda_bin}")"; then
  pass "Conda：${conda_path}"
  if "${conda_path}" env list 2>/dev/null | awk -v wanted="${conda_env}" '$1 == wanted { found = 1 } END { exit !found }'; then
    pass "Conda 环境：${conda_env}"
  else
    warn "找不到 Conda 环境 ${conda_env}；字幕任务仍可运行"
  fi
else
  warn "找不到 Conda；无法由 start.sh 自动启动 CosyVoice"
fi

cosyvoice_root="$(expand_path "$(dotenv_value COSYVOICE_ROOT '~/CosyVoice')")"
cosyvoice_model="$(expand_path "$(dotenv_value COSYVOICE_MODEL "${cosyvoice_root}/pretrained_models/Fun-CosyVoice3-0.5B")")"
if [ -f "${cosyvoice_root}/cosyvoice/cli/cosyvoice.py" ]; then
  pass "CosyVoice 源码：${cosyvoice_root}"
else
  warn "CosyVoice 源码不完整或路径不正确：${cosyvoice_root}"
fi
if [ -d "${cosyvoice_root}/third_party/Matcha-TTS" ]; then
  pass "Matcha-TTS：${cosyvoice_root}/third_party/Matcha-TTS"
else
  warn "找不到 third_party/Matcha-TTS"
fi
if [ -f "${cosyvoice_model}/cosyvoice3.yaml" ]; then
  pass "CosyVoice3 模型：${cosyvoice_model}"
else
  warn "模型目录缺少 cosyvoice3.yaml：${cosyvoice_model}"
fi

cosyvoice_url="$(dotenv_value COSYVOICE_URL http://127.0.0.1:50001)"
if curl_path="$(resolve_executable curl)"; then
  cosy_health="$("${curl_path}" --fail --silent --show-error --max-time 2 "${cosyvoice_url%/}/health" 2>/dev/null || true)"
  if printf '%s' "${cosy_health}" | grep -Eq '"loaded"[[:space:]]*:[[:space:]]*true'; then
    pass "CosyVoice 服务已就绪：${cosyvoice_url}"
  elif [ -n "${cosy_health}" ]; then
    warn "CosyVoice 服务可访问但模型未就绪：${cosyvoice_url}"
  else
    warn "CosyVoice 服务当前未运行：${cosyvoice_url}（start.sh 会尝试启动）"
  fi
else
  warn "找不到 curl，无法探测本地模型服务"
fi

printf '\n翻译服务\n'
translation_provider="$(dotenv_value TRANSLATION_PROVIDER ollama)"
if [ "${translation_provider}" = "ollama" ]; then
  ollama_url="$(dotenv_value OLLAMA_BASE_URL http://127.0.0.1:11434)"
  if [ -n "${curl_path:-}" ] && "${curl_path}" --fail --silent --max-time 2 "${ollama_url%/}/api/tags" >/dev/null 2>&1; then
    pass "Ollama 已响应：${ollama_url}"
  else
    warn "Ollama 未响应；启动 ollama serve 并确认模型已下载"
  fi
elif [ "${translation_provider}" = "openai" ]; then
  openai_url="$(dotenv_value OPENAI_BASE_URL '')"
  openai_model="$(dotenv_value OPENAI_MODEL '')"
  if [ -n "${openai_url}" ] && [ -n "${openai_model}" ]; then
    pass "OpenAI-compatible endpoint 与模型已配置（未显示密钥）"
  else
    warn "TRANSLATION_PROVIDER=openai，但 OPENAI_BASE_URL 或 OPENAI_MODEL 为空"
  fi
else
  warn "未知翻译提供方：${translation_provider}"
fi

printf '\n检查结果：%d 个失败，%d 个提醒。\n' "${failures}" "${warnings}"
if [ "${failures}" -gt 0 ]; then
  exit 1
fi
