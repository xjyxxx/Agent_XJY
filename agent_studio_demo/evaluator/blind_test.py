"""人工盲测（L3）：10 轮 × 3 人 + 一致性系数。

为什么需要它：L1 规则分偏袒模板模型，L2 是模型自评（有偏好）。要宣称"像不像"，
只有人工盲测站得住。

流程：
    python -m evaluator.blind_test build --out runtime/blind
    # 把 runtime/blind/blind_sheet.csv 发给 3 位标注者，各填一列（A 或 B）
    python -m evaluator.blind_test score runtime/blind/blind_sheet.csv

题单设计：每轮给**同一句话的两个回复**——
    有版 = 走完整链路（人设 + 历史证据）
    无版 = 同一个模型、但不给人设和证据
标注者只需回答"哪条更像记录里的那个人"。这是产品的核心主张，也是唯一不靠指标自证的问题。

一致性用 **Fleiss kappa**（多人）与 **Cohen kappa**（两两）。kappa < 0.4 说明题目本身有歧义，
先改题而不是下结论。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.service import SessionService
from ingest import session_store
from llm_client.factory import create_reply_model, load_dotenv

FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")
GOLDEN = "王哥今晚加班，晚饭你自己先吃？"

ROUNDS: List[str] = [
    GOLDEN,
    "那周六呢？",
    "在吗",
    "谢谢王哥",
    "晚上想吃面",
    "你最近是不是特别忙",
    "行吧",
    "周末的电影票别忘了",
    "你几点下班",
    "我先去开会了",
]

RATER_COLUMNS = ("rater_1", "rater_2", "rater_3")
NO_PERSONA_SYSTEM = "你是一个聊天助手，用一句简短的中文回复对方。"


def _plain_reply(message: str) -> str:
    """对照组：同一个模型，但不给人设、不给历史证据。"""

    from llm_client.deepseek import DeepSeekReplyModel

    model = DeepSeekReplyModel(temperature=0.7)
    return model._chat(system=NO_PERSONA_SYSTEM, user=message).strip() or "嗯"


def build(out_dir: Path, seed: int = 42) -> Path:
    load_dotenv()
    session_store.clear()
    out_dir.mkdir(parents=True, exist_ok=True)
    random.seed(seed)

    model = create_reply_model("auto")
    if hasattr(model, "temperature"):
        model.temperature = 0.7
    service = SessionService(":memory:", model=model)
    rows = []
    key: Dict[str, dict] = {}
    try:
        session = service.create_session(FIXTURE, "老王", "小林")
        thread = session.thread_id
        for index, message in enumerate(ROUNDS, start=1):
            with_persona = service.send(thread, message).reply or ""
            without = _plain_reply(message)
            # 随机左右，避免位置偏置
            flip = random.random() < 0.5
            option_a, option_b = (without, with_persona) if flip else (with_persona, without)
            real = "B" if flip else "A"
            round_id = f"r{index:02d}"
            rows.append(
                {
                    "round": round_id,
                    "用户说": message,
                    "选项A": option_a,
                    "选项B": option_b,
                    **{column: "" for column in RATER_COLUMNS},
                }
            )
            key[round_id] = {
                "user_input": message,
                "with_persona": with_persona,
                "without_persona": without,
                "real_option": real,
            }
    finally:
        service.close()

    sheet = out_dir / "blind_sheet.csv"
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "blind_key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"题单: {sheet}")
    print(f"答案: {out_dir / 'blind_key.json'}（不要给标注者）")
    print(f"轮数: {len(rows)}；请 3 位标注者在 {', '.join(RATER_COLUMNS)} 列各填 A 或 B")
    return sheet


# ------------------------------------------------------------------ 统计
def cohen_kappa(left: Sequence[str], right: Sequence[str]) -> float:
    pairs = [(a, b) for a, b in zip(left, right) if a and b]
    if not pairs:
        return 0.0
    n = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / n
    labels = {a for a, _ in pairs} | {b for _, b in pairs}
    expected = sum(
        (sum(1 for a, _ in pairs if a == label) / n) * (sum(1 for _, b in pairs if b == label) / n)
        for label in labels
    )
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return round((observed - expected) / (1 - expected), 4)


def fleiss_kappa(table: Sequence[Sequence[int]]) -> float:
    """table: 每行是一个题目上各标签的计数（顺序一致）。"""

    rows = [row for row in table if sum(row) >= 2]
    if not rows:
        return 0.0
    n_subjects = len(rows)
    p_i = []
    for row in rows:
        total = sum(row)
        p_i.append((sum(count * count for count in row) - total) / (total * (total - 1)) if total > 1 else 0.0)
    observed = sum(p_i) / n_subjects
    totals = [sum(row[j] for row in rows) for j in range(len(rows[0]))]
    grand = sum(totals)
    expected = sum((value / grand) ** 2 for value in totals)
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return round((observed - expected) / (1 - expected), 4)


def score(sheet_path: Path, key_path: Optional[Path] = None) -> dict:
    key_path = key_path or (sheet_path.parent / "blind_key.json")
    key = json.loads(Path(key_path).read_text(encoding="utf-8"))

    with Path(sheet_path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    raters = [column for column in RATER_COLUMNS if any(row.get(column, "").strip() for row in rows)]
    if not raters:
        raise ValueError("题单里没有任何标注（rater_* 列全空）")

    per_rater = {}
    for column in raters:
        correct = total = 0
        for row in rows:
            answer = (row.get(column) or "").strip().upper()
            expected = key[row["round"]]["real_option"]
            if answer in ("A", "B"):
                total += 1
                correct += int(answer == expected)
        per_rater[column] = round(correct / total, 3) if total else 0.0

    labels = ["A", "B"]
    table = []
    for row in rows:
        counts = [0, 0]
        for column in raters:
            answer = (row.get(column) or "").strip().upper()
            if answer in ("A", "B"):
                counts[labels.index(answer)] += 1
        table.append(counts)

    pairwise = {}
    for i in range(len(raters)):
        for j in range(i + 1, len(raters)):
            left = [(row.get(raters[i]) or "").strip().upper() for row in rows]
            right = [(row.get(raters[j]) or "").strip().upper() for row in rows]
            pairwise[f"{raters[i]}~{raters[j]}"] = cohen_kappa(left, right)

    fleiss = fleiss_kappa(table)
    mean_accuracy = round(sum(per_rater.values()) / len(per_rater), 3)
    verdict = (
        "可用" if fleiss >= 0.6 else "勉强" if fleiss >= 0.4 else "题目有歧义，先改题"
    )
    return {
        "rounds": len(rows),
        "raters": raters,
        "accuracy_per_rater": per_rater,
        "accuracy_mean": mean_accuracy,
        "cohen_pairwise": pairwise,
        "fleiss_kappa": fleiss,
        "verdict": verdict,
    }


def simulate(sheet_path: Path, key_path: Optional[Path] = None, out: Optional[Path] = None) -> Path:
    """用 LLM judge 填一遍，只用于**验证流程**，不是真的人工结论。"""

    load_dotenv()
    from evaluator.llm_judge import JUDGE_SYSTEM
    from llm_client.deepseek import chat_once

    with Path(sheet_path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    for row in rows:
        prompt = (
            f"用户说：{row['用户说']}\n选项A：{row['选项A']}\n选项B：{row['选项B']}\n"
            '只输出 JSON：{"更像": "A" 或 "B"}'
        )
        raw = chat_once(JUDGE_SYSTEM, prompt)
        try:
            choice = json.loads(raw[raw.find("{") : raw.rfind("}") + 1]).get("更像", "A")
        except Exception:  # noqa: BLE001
            choice = "A"
        choice = str(choice).strip().upper()
        row["rater_1"] = choice if choice in ("A", "B") else "A"
        row["rater_2"] = row["rater_1"]
        row["rater_3"] = row["rater_1"]

    out = out or sheet_path.with_name("blind_sheet_simulated.csv")
    with Path(out).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[simulated] 已写入 {out}（单人/同源，kappa 恒为 1，仅验证流程）")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Blind test (L3)")
    sub = parser.add_subparsers(dest="command")

    build_parser = sub.add_parser("build", help="生成 10 轮盲测题单")
    build_parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "runtime" / "blind")
    build_parser.add_argument("--seed", type=int, default=42)

    score_parser = sub.add_parser("score", help="从填写后的 CSV 算准确率与 kappa")
    score_parser.add_argument("sheet", type=Path)
    score_parser.add_argument("--json", action="store_true")

    simulate_parser = sub.add_parser("simulate", help="用 LLM judge 模拟填表（仅验证流程）")
    simulate_parser.add_argument("sheet", type=Path)

    args = parser.parse_args(argv)
    if args.command == "build":
        build(args.out, args.seed)
        return 0
    if args.command == "score":
        try:
            result = score(args.sheet)
        except (ValueError, FileNotFoundError) as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"轮数 {result['rounds']}；标注者 {result['raters']}")
            print("人均准确率:", result["accuracy_per_rater"], "均值", result["accuracy_mean"])
            print("两两 Cohen kappa:", result["cohen_pairwise"])
            print(f"Fleiss kappa = {result['fleiss_kappa']} → {result['verdict']}")
        return 0
    if args.command == "simulate":
        simulate(args.sheet)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
