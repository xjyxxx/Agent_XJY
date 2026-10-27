"""在 CI 里跑全量测试，并把失败点写成 GitHub 注解。

为什么需要它：GitHub 的「下载日志」接口要仓库管理员权限，第三方拿不到；
而 **annotation 是公开可读的**（`/check-runs/{id}/annotations`）。
把 `FAIL:`/`ERROR:` 转成 `::error file=...,line=...::` 之后，失败原因可以直接
从 API 读出来，也方便在手机上看 Actions 页面。

用法：
    python tools/ci_report.py                    # 跑 tests/ 下所有 test_*.py
    python tools/ci_report.py --pattern test_day*.py
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')


def escape(text: str) -> str:
    """GitHub workflow command 的转义规则：% 先转，再转换行。"""

    return (
        str(text)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def annotate(test, traceback_text: str, kind: str) -> None:
    frames = FRAME_RE.findall(traceback_text)
    # 取最后一个"测试文件里"的栈帧：那才是断言发生的位置，而不是框架内部
    location = None
    for file_name, line in frames:
        if "tests" in file_name.replace("\\", "/"):
            location = (file_name, line)
    detail = [line for line in traceback_text.strip().splitlines() if line.strip()]
    message = detail[-1] if detail else kind
    title = test.id()
    if location:
        file_name, line = location
        print(
            f"::error file={escape(file_name)},line={line},"
            f"title={escape(title)}::{escape(message[:600])}"
        )
    else:
        print(f"::error title={escape(title)}::{escape(message[:600])}")


def from_junit(path: Path) -> int:
    """把 pytest 的 junit xml 转成注解（pytest 跑挂了时用）。"""

    import xml.etree.ElementTree as ET

    if not path.is_file():
        print(f"::warning::没找到 {path}，跳过失败注解")
        return 1

    failures = 0
    for case in ET.parse(path).getroot().iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is None:
                continue
            failures += 1
            message = (node.get("message") or node.text or tag).strip().splitlines()
            detail = message[0] if message else tag
            title = f"{case.get('classname', '')}.{case.get('name', '')}".strip(".")
            file_name = case.get("file") or ""
            line = case.get("line") or "1"
            print(
                f"::error file={escape(file_name)},line={line},"
                f"title={escape(title)}::{escape(detail[:600])}"
            )
    print(f"junit: {failures} 个失败", flush=True)
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run tests and emit GitHub annotations")
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--verbosity", type=int, default=1)
    parser.add_argument(
        "--from-junit",
        type=Path,
        help="不从 unittest 跑，而是读 pytest 的 junit xml 生成注解",
    )
    args = parser.parse_args(argv)

    if args.from_junit:
        return from_junit(args.from_junit)

    suite = unittest.TestLoader().discover(
        str(PROJECT_ROOT / "tests"),
        pattern=args.pattern,
        top_level_dir=str(PROJECT_ROOT),
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=args.verbosity).run(suite)

    output = stream.getvalue()
    print(output, flush=True)

    for test, trace in result.failures:
        annotate(test, trace, "failure")
    for test, trace in result.errors:
        annotate(test, trace, "error")

    print(
        "ran={0} failures={1} errors={2} skipped={3}".format(
            result.testsRun, len(result.failures), len(result.errors), len(result.skipped)
        ),
        flush=True,
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
