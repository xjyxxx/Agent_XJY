"""模型连通性预检：`python -m llm_client.check`

三件事：
1. 打印将要使用的配置（key 打码，不泄露）；
2. 发一个极小请求，验证网络 / 鉴权 / 余额；
3. 失败时给可操作的提示 + 非零退出码（可以直接进 CI）。

    python -m llm_client.check
    python -m llm_client.check --json
"""

from __future__ import annotations

import argparse
import json
import sys

from llm_client.deepseek import DeepSeekReplyModel
from llm_client.factory import load_dotenv


EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_KEY = 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check remote model connectivity")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    load_dotenv()
    summary = DeepSeekReplyModel.config_summary()

    if not summary["key_present"]:
        message = (
            "未配置 DEEPSEEK_API_KEY。两种方式任选：\n"
            "  1) 复制 .env.example 为 .env，填入 key；\n"
            "  2) 当前 PowerShell 会话里 $env:DEEPSEEK_API_KEY = 'sk-...'\n"
            "不想用远程模型就用离线兜底：--provider heuristic（无需 key）。"
        )
        if args.json:
            print(json.dumps({"ok": False, "reason": "no_key", "config": summary,
                              "detail": message}, ensure_ascii=False, indent=2))
        else:
            print("config:", json.dumps(summary, ensure_ascii=False))
            print(f"[FAIL] {message}", file=sys.stderr)
        return EXIT_NO_KEY

    try:
        result = DeepSeekReplyModel(timeout=args.timeout).probe(timeout=args.timeout)
    except (ValueError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "reason": "error", "config": summary,
                              "detail": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print("config:", json.dumps(summary, ensure_ascii=False))
            print(f"[FAIL] {exc}", file=sys.stderr)
        return EXIT_FAIL

    payload = {"ok": True, "config": summary, **result}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("config:", json.dumps(summary, ensure_ascii=False))
        print(
            f"[OK] {result['model']} @ {result['base_url']} "
            f"latency={result['latency_ms']}ms reply={result['reply']!r}"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
