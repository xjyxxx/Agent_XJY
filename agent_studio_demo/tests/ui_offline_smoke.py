"""前端容错验证：连接中断时是否出现横幅、能否点「重试」恢复。

    python -m tests.ui_offline_smoke http://127.0.0.1:8150/

用系统 Edge（channel="msedge"），不下载浏览器。这个脚本只做验证，不参与运行时。
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    url = argv[0] if argv else "http://127.0.0.1:8150/"

    from playwright.sync_api import sync_playwright

    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        # 第一屏：横幅必须是隐藏的
        page.goto(url, wait_until="networkidle")
        if not page.locator("#banner").evaluate("node => node.classList.contains('hidden')"):
            failures.append("初始状态横幅不应显示")

        page.click("#btn-import")
        page.wait_for_selector("#session-section:not(.hidden)", timeout=15000)
        page.click("#btn-session")
        page.wait_for_selector("#composer", state="visible", timeout=15000)

        # 让流式请求直接失败，触发前端重试 + 横幅
        page.route("**/sessions/*/stream*", lambda route: route.abort())
        page.fill("#message", "在吗")
        page.click("#composer button[type=submit]")
        page.wait_for_selector("#banner:not(.hidden)", timeout=20000)
        text = page.inner_text("#banner-text")
        if "失败" not in text and "中断" not in text:
            failures.append(f"横幅文案不合适：{text}")
        if page.locator("#banner-retry").evaluate("node => node.classList.contains('hidden')"):
            failures.append("横幅上应有「重试」按钮")
        if not errors:
            pass  # 重试过程中的 fetch 失败由 catch 处理，不应产生未捕获异常

        # 恢复网络后点重试，应能正常拿到回复
        page.unroute("**/sessions/*/stream*")
        page.click("#banner-retry")
        page.wait_for_function(
            "() => { const e = document.querySelector('#messages .msg.ai .bubble');"
            " return e && e.textContent.trim().length > 0"
            " && !e.textContent.includes('没有收到回复'); }",
            timeout=30000,
        )
        if not page.locator("#banner").evaluate("node => node.classList.contains('hidden')"):
            failures.append("重试成功后横幅应自动隐藏")

        page.screenshot(path=str(Path("runtime") / "ui-retry.png"), full_page=True)
        if errors:
            failures.extend(f"页面异常：{item}" for item in errors)
        browser.close()

    if failures:
        print("[FAIL]")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("[ok] 断线横幅 → 重试 → 恢复，全部符合预期")
    print("     截图: runtime/ui-retry.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
