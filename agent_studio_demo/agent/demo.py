"""Run the Persona Replay vertical slice from the terminal.

Day7-Day9：输出 critique（语气/串台/事实判定）、status 和执行路径，
单轮失败时能直接看出是检索错、生成错，还是被 Critic 拦下。

Day11：Critic 判 `review` 时不再静默降级，而是中断等人。命令行可以用
`--decision approve|edit|reject`（`edit` 需配 `--reply`）继续这一轮。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.graph import PersonaReplayRuntime
from agent.react import ReActAgent
from agent.state import AgentState
from llm_client.factory import create_reply_model, load_dotenv


def print_state(state: AgentState, title: str = "") -> None:
    if title:
        print(f"--- {title} ---")
    print(f"thread_id: {state.thread_id}")
    print(f"chat_id: {state.chat_id}")
    print(f"speakers: {', '.join(state.speakers)}")
    print(f"persona: {json.dumps(state.persona, ensure_ascii=False)}")
    print("retrieved:")
    if not state.retrieved:
        print("  (no evidence above threshold)")
    for hit in state.retrieved:
        print(
            f"  - score={hit['score']} "
            f"(ctx={hit['context_score']}, content={hit['content_score']}): "
            f"{hit['content']} (line {hit['line_number']})"
        )
    print(f"draft: {state.draft}")
    print(f"reply: {state.reply}")
    print(f"critique: {json.dumps(state.critique, ensure_ascii=False)}")
    print(f"rewrites: {state.rewrite_count}")
    print(f"status: {state.status}")
    if state.human_decision:
        print(f"human_decision: {state.human_decision}")
    if state.pending:
        print(f"pending: {json.dumps(state.pending, ensure_ascii=False)}")
    print(f"steps: {' -> '.join(state.steps)}")
    print(f"saved_turns: {state.turn_count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Persona Replay graph")
    parser.add_argument(
        "--chat-path",
        type=Path,
        default=Path("fixtures/chat_laowang_xiaolin.txt"),
    )
    parser.add_argument("--user-speaker", default="老王")
    parser.add_argument("--target-speaker", default="小林")
    parser.add_argument("--message", required=True, help="message sent as user_speaker")
    parser.add_argument("--thread-id", default="demo-thread")
    parser.add_argument(
        "--checkpoint",
        default=":memory:",
        help="SQLite file path, or :memory: for one process",
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "heuristic", "deepseek"),
        default="auto",
        help="reply model: auto uses DeepSeek when DEEPSEEK_API_KEY is set",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=12,
        help="hard cap on node executions for one turn (rewrite loop guard)",
    )
    parser.add_argument(
        "--decision",
        choices=("approve", "edit", "reject"),
        help="when the turn is interrupted, resolve it with this human decision",
    )
    parser.add_argument(
        "--reply",
        help="replacement text for --decision edit",
    )
    parser.add_argument(
        "--show-react",
        action="store_true",
        help="also print the Day2 ReAct trace",
    )
    args = parser.parse_args()

    load_dotenv()
    model = create_reply_model(args.provider)
    print(f"provider: {args.provider} -> {type(model).__name__}")

    runtime = PersonaReplayRuntime(
        args.checkpoint, model=model, max_steps=args.max_steps
    )
    try:
        state = runtime.invoke(
            thread_id=args.thread_id,
            chat_path=str(args.chat_path),
            user_speaker=args.user_speaker,
            target_speaker=args.target_speaker,
            user_input=args.message,
        )
        print_state(state, "turn 1")

        if state.status == "interrupted":
            if args.decision:
                state = runtime.resume(
                    args.thread_id,
                    {"action": args.decision, "reply": args.reply or ""},
                )
                print_state(state, f"after resume: {args.decision}")
            else:
                print(
                    "\n[interrupted] 这一轮需要人工裁决。加 --decision approve|edit|reject "
                    "（edit 配 --reply）再跑一次同一条命令即可恢复。"
                )

        if args.show_react:
            result = ReActAgent().run(
                question=args.message,
                chat_path=args.chat_path,
                user_speaker=args.user_speaker,
                target_speaker=args.target_speaker,
            )
            print("react_trace:")
            for number, step in enumerate(result.steps, 1):
                print(f"  {number}. {step.action} -> {step.observation}")
            print(f"react_answer: {result.answer}")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
