# 可选集成：借什么、装什么、不装什么

> 一句话：**默认零依赖**。下面这些是"装了多一种能力，不装一切照旧"。

## 已经做进仓库的四件

| 能力 | 位置 | 默认 | 说明 |
|---|---|---|---|
| 依赖探测 | `integrations/optional.py` | 始终 | `module_available()` 真 import 一次（不用 `find_spec`，那个会骗人）；缺依赖时报错带安装命令 |
| mem0 适配器 | `integrations/mem0_backend.py` | **关闭** | 薄封装 `add_turn / search`；真实使用需要自己配 LLM+embedding |
| RAGAS 适配器 | `integrations/ragas_backend.py` | **关闭** | 按 ragas 0.2+ 接口写，接口不符会抛 `RagasBackendError` 并提示用内置实现 |
| RAGAS 式 faithfulness | `evaluator/faithfulness.py` | **开启** | 借定义自己实现：claim 拆分 → 数字/重合度判定 → 比例；语气句记 n/a |
| OTel 风格 trace | `observability/trace.py` | **开启** | 每轮一个 32-hex `trace_id`，每个事件一个 16-hex `span_id`，另带 `traceId/spanId/parentSpanId` 别名 |

```powershell
python -m integrations        # 看装没装
```

## 为什么 mem0 / RAGAS 默认不接主链路

| 项目 | 接进来要付的代价 | 什么时候值得 |
|---|---|---|
| **mem0** | 它自己会调 LLM 抽事实 + 需要 embedding 存储；每条消息多一次模型调用 | 真实对话有几百轮、事实密集、需要跨会话记忆 |
| **RAGAS** | 拉 `datasets` / `langchain` / `openai` 一整套；而且我们的回复大多是 2–5 字语气句，claim 拆完常常是"没有事实断言" | 要和论文/榜单对齐指标时 |
| **Guardrails / NeMo** | 依赖重、启动慢 | 对外提供服务、需要输入输出双向护栏 |
| **Langfuse / Phoenix** | 要起服务（Postgres/ClickHouse 之类） | 多人协作、需要看 trace 面板 |
| **Dify / AgentScope / crewAI / AG2** | 框架级，会把四层设计盖掉 | 换成另一套产品时 |

**判断标准**：这个库能不能只作为"接口后面的一种实现"存在？能，就接；不能，就只借想法。

## 打开 mem0（示例）

```powershell
pip install mem0ai
```

```python
from integrations.mem0_backend import Mem0Store

store = Mem0Store(
    user_id="laowang-xiaolin",
    config={
        "llm": {"provider": "deepseek", "config": {"model": "deepseek-chat"}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    },
)
store.add_turn("王哥今晚加班", "哈哈知道了")
print(store.search("加班", limit=2))
```

主链路目前**没有**调用它（默认关闭）。要接的话建议接在
`agent/nodes.py::draft_node` 之前：先 `search()` 拿事实，再拼进 prompt 的"长期记忆"段，
并且**只在 `MEMORY_BACKEND=mem0` 时**走这条路，避免默认多花钱。

## 打开 RAGAS（示例）

```powershell
pip install ragas
```

```python
from integrations.ragas_backend import score_with_backend

result = score_with_backend(
    [
        {
            "user_input": "我们什么时候结婚",
            "response": "啥呀，没说过啊",
            "retrieved_contexts": ["老王: 周末去看电影吗"],
        }
    ],
    backend="ragas",     # 默认 native
)
print(result)
```

## 许可提醒

| 项目 | 许可 | 注意 |
|---|---|---|
| mem0 | Apache-2.0 | 可用；保留 LICENSE |
| ragas | Apache-2.0 | 可用 |
| langgraph | MIT | 已作为可选依赖接入 |
| 本仓库的 `third_party/llm_agents` | MIT 快照 | 仅作阅读对照，不参与构建 |

复制代码片段（例如 RAGAS 的指标公式）时，在文档里注明出处与许可证；
**AGPL 项目不要复制进仓库**。

## 明确不做

- 向量库（Chroma/Qdrant）：50 条语料上词法已满分，接库无可验证收益；
- 本地模型（Ollama/vLLM）：项目走远程 API；
- `tenacity`：20 行重试不值得加依赖，已有指数退避实现；
- 全量 Agent 框架：会盖掉人设/检索/Critic/HITL 这四层设计。
