"""打印可选集成的状态：python -m integrations"""

from __future__ import annotations

import json

from integrations.optional import status


def main() -> int:
    info = status()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print()
    for name, item in info.items():
        if item["available"]:
            print(f"[ok] {name} {item['version']} 已安装，可用")
        else:
            print(f"[--] {name} 未安装（不装也能跑，走内置实现）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
