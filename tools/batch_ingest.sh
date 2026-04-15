#!/usr/bin/env bash
# 批量处理目录内视频。用法：
#   export RAW_INGEST_BATCH_INPUT=/root/autodl-tmp/某目录
#   ./tools/batch_ingest.sh
# 或：
#   ./tools/batch_ingest.sh /root/autodl-tmp/某目录

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "${CONDA_PREFIX:-}" ]]; then
  PY="python"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="python3"
fi

INPUT="${1:-${RAW_INGEST_BATCH_INPUT:-}}"
if [[ -z "$INPUT" ]]; then
  echo "请指定输入目录：$0 /path/to/videos 或设置 RAW_INGEST_BATCH_INPUT" >&2
  exit 1
fi

export RAW_INGEST_BATCH_INPUT="$(cd "$INPUT" && pwd)"
export RAW_INGEST_INPUT_ROOT="$RAW_INGEST_BATCH_INPUT"

shopt -s nullglob
files=("$RAW_INGEST_BATCH_INPUT"/*.{mp4,mkv,mov,MP4,MKV,MOV})
if [[ ${#files[@]} -eq 0 ]]; then
  echo "未找到 mp4/mkv/mov: $RAW_INGEST_BATCH_INPUT" >&2
  exit 1
fi

for f in "${files[@]}"; do
  echo "======== $(basename "$f") ========"
  "$PY" -m video_raw_ingest run "$f" || exit $?
done

echo "全部完成。"
