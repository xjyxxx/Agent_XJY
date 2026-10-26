"""Day14：FastAPI + SSE 契约。

Day14 的 HTTP 层是**预留依赖**（fastapi / uvicorn / httpx）。没装时：
- `import api.main` 仍然成功；
- 本文件整体 skip，而不是报错；
- 有一个用例专门验证"缺依赖时的报错是可操作的"。

钉住四件事：
1. 七个接口能串起完整一轮扮演；`session_id` 就是 thread_id。
2. SSE 事件顺序：persona → retrieved → draft → critique → reply_delta → done。
3. 中断走 `interrupt` 事件，`POST /resume` 能接着跑。
4. 错误码：未知会话 404、状态冲突 409、参数错误 422/400。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api import main as api_main
from ingest import session_store
from llm_client.scripted import ScriptedModel

try:  # 预留依赖：没装就跳过，等统一安装后再启用
    from fastapi.testclient import TestClient

    HTTP_STACK_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]
    HTTP_STACK_AVAILABLE = False

SKIP_REASON = "可选依赖未安装（pip install -r requirements-optional.txt）"


FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


def make_client(model=None) -> "TestClient":
    session_store.clear()
    return TestClient(create_app_safe(model))


def create_app_safe(model=None):
    return api_main.create_app(checkpoint_path=":memory:", model=model)


def create_session(client) -> str:
    response = client.post(
        "/sessions",
        json={
            "path": FIXTURE,
            "user_speaker": "老王",
            "target_speaker": "小林",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


class OptionalDependencyTests(unittest.TestCase):
    """不依赖是否真的装了 fastapi：模拟缺失，验证报错可操作。"""

    def test_missing_stack_raises_actionable_error(self) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.split(".")[0] in {"fastapi", "pydantic", "starlette"}:
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(
                api_main.DependencyMissing, "requirements-optional"
            ):
                api_main.create_app(checkpoint_path=":memory:")


@unittest.skipUnless(HTTP_STACK_AVAILABLE, SKIP_REASON)
class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = make_client()

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_import_lists_two_speakers(self) -> None:
        response = self.client.post(
            "/chats/import",
            json={"path": FIXTURE, "user_speaker": "老王", "target_speaker": "小林"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["speakers"], ["老王", "小林"])
        self.assertEqual(body["message_count"], 20)
        self.assertEqual(body["persona"]["speaker"], "小林")
        self.assertTrue(body["chat_id"])

    def test_import_rejects_missing_file(self) -> None:
        response = self.client.post("/chats/import", json={"path": "nope.txt"})
        self.assertEqual(response.status_code, 400)

    def test_session_creation_runs_planner(self) -> None:
        response = self.client.post(
            "/sessions",
            json={"path": FIXTURE, "user_speaker": "老王", "target_speaker": "小林"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["session_id"], body["thread_id"])
        self.assertEqual(body["persona"]["speaker"], "小林")
        self.assertEqual(body["turn_count"], 0)
        self.assertEqual(body["status"], "ok")

    def test_full_turn_with_evidence(self) -> None:
        session_id = create_session(self.client)
        response = self.client.post(
            f"/sessions/{session_id}/messages", json={"message": GOLDEN_QUERY}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["reply"], "哈哈知道了")
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["critique"]["verdict"], "pass")
        self.assertEqual(body["retrieved"][0]["line_number"], 15)
        self.assertEqual(body["steps"], ["planner", "retrieve", "draft", "critic"])

        current = self.client.get(f"/sessions/{session_id}").json()
        self.assertEqual(current["turn_count"], 1)

    def test_second_turn_keeps_thread(self) -> None:
        session_id = create_session(self.client)
        self.client.post(f"/sessions/{session_id}/messages", json={"message": GOLDEN_QUERY})
        response = self.client.post(
            f"/sessions/{session_id}/messages", json={"message": "那周六呢？"}
        )
        self.assertEqual(response.json()["turn_count"], 2)

    def test_unknown_session_is_404(self) -> None:
        self.assertEqual(self.client.get("/sessions/nope").status_code, 404)
        self.assertEqual(
            self.client.post("/sessions/nope/messages", json={"message": "hi"}).status_code,
            404,
        )
        self.assertEqual(self.client.get("/sessions/nope/stream?message=hi").status_code, 404)

    def test_delete_removes_the_checkpoint(self) -> None:
        session_id = create_session(self.client)
        self.assertEqual(self.client.delete(f"/sessions/{session_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/sessions/{session_id}").status_code, 404)

    def test_empty_message_is_rejected(self) -> None:
        session_id = create_session(self.client)
        response = self.client.post(
            f"/sessions/{session_id}/messages", json={"message": ""}
        )
        self.assertEqual(response.status_code, 422)


@unittest.skipUnless(HTTP_STACK_AVAILABLE, SKIP_REASON)
class StreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = make_client()

    def test_sse_event_order(self) -> None:
        session_id = create_session(self.client)
        with self.client.stream(
            "GET", f"/sessions/{session_id}/stream", params={"message": GOLDEN_QUERY}
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())

        events = [
            line[len("event: ") :]
            for line in body.splitlines()
            if line.startswith("event: ")
        ]
        self.assertEqual(events[0], "persona")
        self.assertIn("retrieved", events)
        self.assertIn("draft", events)
        self.assertIn("critique", events)
        self.assertIn("reply_delta", events)
        self.assertEqual(events[-1], "done")

        # 事件顺序必须固定：依据先于草稿，草稿先于判定，判定先于正文。
        self.assertLess(events.index("retrieved"), events.index("draft"))
        self.assertLess(events.index("draft"), events.index("critique"))
        self.assertLess(events.index("critique"), events.index("reply_delta"))

        deltas = [
            payload for event, payload in _parse(body) if event == "reply_delta"
        ]
        self.assertEqual("".join(delta["delta"] for delta in deltas), "哈哈知道了")

    def test_sse_payloads_are_json(self) -> None:
        session_id = create_session(self.client)
        with self.client.stream(
            "GET", f"/sessions/{session_id}/stream", params={"message": GOLDEN_QUERY}
        ) as response:
            body = "".join(response.iter_text())
        for event, payload in _parse(body):
            self.assertIsInstance(payload, dict, event)


@unittest.skipUnless(HTTP_STACK_AVAILABLE, SKIP_REASON)
class HitlApiTests(unittest.TestCase):
    def test_sensitive_reply_interrupts_then_edit_resumes(self) -> None:
        client = make_client(model=ScriptedModel(["我银行卡密码是123456"]))
        session_id = create_session(client)

        response = client.post(
            f"/sessions/{session_id}/messages", json={"message": GOLDEN_QUERY}
        )
        body = response.json()
        self.assertEqual(body["status"], "interrupted")
        self.assertEqual(body["pending"]["reason"], "sensitive")
        self.assertEqual(body["pending"]["categories"], ["credential"])

        resumed = client.post(
            f"/sessions/{session_id}/resume",
            json={"action": "edit", "reply": "哈哈这个不说"},
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["reply"], "哈哈这个不说")
        self.assertEqual(resumed.json()["human_decision"], "edit")

    def test_resume_on_running_session_is_409(self) -> None:
        client = make_client()
        session_id = create_session(client)
        response = client.post(
            f"/sessions/{session_id}/resume", json={"action": "approve"}
        )
        self.assertEqual(response.status_code, 409)

    def test_resume_validates_action(self) -> None:
        client = make_client()
        session_id = create_session(client)
        response = client.post(
            f"/sessions/{session_id}/resume", json={"action": "maybe"}
        )
        self.assertEqual(response.status_code, 422)

    def test_stream_emits_interrupt_instead_of_reply(self) -> None:
        client = make_client(model=ScriptedModel(["我银行卡密码是123456"]))
        session_id = create_session(client)
        with client.stream(
            "GET", f"/sessions/{session_id}/stream", params={"message": GOLDEN_QUERY}
        ) as response:
            body = "".join(response.iter_text())

        events = [
            line[len("event: ") :]
            for line in body.splitlines()
            if line.startswith("event: ")
        ]
        self.assertIn("interrupt", events)
        self.assertNotIn("reply_delta", events)
        done = [payload for event, payload in _parse(body) if event == "done"][0]
        self.assertEqual(done["status"], "interrupted")


def _parse(body: str):
    """Minimal SSE parser: returns (event, json payload) pairs."""

    events = []
    current_event = None
    for line in body.splitlines():
        if line.startswith("event: "):
            current_event = line[len("event: ") :]
        elif line.startswith("data: ") and current_event:
            events.append((current_event, json.loads(line[len("data: ") :])))
            current_event = None
    return events


if __name__ == "__main__":
    unittest.main()
