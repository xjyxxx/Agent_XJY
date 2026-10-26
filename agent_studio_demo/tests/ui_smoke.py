"""用本机已装的 Edge 跑一遍前端交互，并截图（仅供验证，不参与运行时）。

    python -m tests.ui_smoke http://127.0.0.1:8150/ runtime/ui-flow.png

为什么用 channel="msedge"：不下载 Playwright 自带的 Chromium（几百 MB），
直接复用系统 Edge。这个脚本是**验证工具**，不是产品代码。
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    url = argv[0] if argv else "http://127.0.0.1:8150/"
    out = Path(argv[1] if len(argv) > 1 else "runtime/ui-flow.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(url, wait_until="networkidle")

        page.click("#btn-import")
        page.wait_for_selector("#session-section:not(.hidden)", timeout=15000)
        page.click("#btn-session")
        page.wait_for_selector("#chat-section:not(.hidden)", timeout=15000)

        page.fill("#message", "王哥今晚加班，晚饭你自己先吃？")
        page.click("#composer button[type=submit]")
        # 等 done 事件把气泡定稿
        page.wait_for_function(
            "() => { const b = document.querySelector('.msg.ai .bubble');"
            " return b && b.textContent.trim().length > 0; }",
            timeout=60000,
        )
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out), full_page=True)

        reply = page.inner_text(".msg.ai .bubble")
        evidence = page.eval_on_selector_all(".ev-item", "els => els.length")
        verdict = page.inner_text("#critique-box")
        events = page.inner_text("#event-log")
        browser.close()

    print("reply   :", reply.strip())
    print("evidence:", evidence)
    print("critique:", " ".join(verdict.split())[:120])
    print("events  :", [line.split("] ", 1)[-1].split("\n")[0] for line in events.splitlines() if line.startswith("[")][:8])
    print("js errors:", errors or "none")
    print("screenshot:", out)
    return 0 if reply.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
