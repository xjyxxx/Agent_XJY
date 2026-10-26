#!/usr/bin/env bash
# macOS / Linux 快捷入口：先构建检查，再启动服务
#
#   ./run.sh
#   ./run.sh --port 8100 --provider deepseek --no-open
#
# 真正的逻辑在 manage.py（跨平台），这里只做转发。

set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[xx] 找不到 python3，请先安装 Python 3.10+" >&2
  exit 1
fi

exec python3 manage.py run "$@"
