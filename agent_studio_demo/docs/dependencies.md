# 依赖策略：核心零依赖，可选能力预留

> 一句话：**能不加就不加；必须加的，先登记、后统一安装、代码里要能降级。**

## 1. 原则

1. **核心链路零第三方依赖**：解析、检索、状态图、checkpoint、评估、A/B、CLI
   只依赖 Python 标准库（`sqlite3` / `urllib` / `hashlib` / `unittest` → `pytest` 未启用）。
2. **可选能力"预留"**：需要外部包的功能，先登记在 `requirements-optional.txt`，
   代码里做成可降级；到真正要用时再统一安装。
3. **缺依赖必须"可操作地失败"**：不允许出现裸 `ImportError`。要么给出安装指令
   （`DependencyMissing`），要么让测试自动 skip。
4. **不为"以后可能用"而装**：装了就得维护版本、就得在面试里解释为什么用，
   而没接的依赖只会变成追问点。

## 2. 现状

| 能力 | 依赖 | 状态 | 缺依赖时的行为 |
|---|---|---|---|
| 解析 / 检索 / 状态图 / checkpoint | 无 | ✅ 已实现 | — |
| 评估（10 轮 + 门禁） | 无 | ✅ 已实现 | — |
| 检索 A/B | 无（哈希向量用 `hashlib`） | ✅ 已实现 | — |
| DeepSeek 调用 | 无（`urllib` 直连） | ✅ 已实现 | 无 key 时报错并提示配置 |
| HTTP 层（Day14） | `fastapi` / `uvicorn` / `httpx` | ✅ 已实现、⚠️ 依赖预留 | 自动退回标准库适配器（`transport.http.stdlib_adapter`）；接口测试自动 skip |
| 高性能 server | `granian` | ✅ 可选、⚠️ 依赖预留 | `--server auto` 依次退回 uvicorn → stdlib（实测见 `docs/performance.md`） |
| 语义 embedding | `sentence-transformers` 或 HTTP 服务 | ⬜ 预留 | `HttpEmbedder` 缺环境变量时报错；A/B 用 `--embedder none` 照常跑 |
| 向量库 | `qdrant-client` / `chromadb` | ⬜ 预留 | 当前用内存索引，无影响 |
| 远程模型 | 无（`urllib` 直连 OpenAI 兼容端点） | ✅ 已实现 | 无 key 时 `--provider auto` 自动退回 heuristic；`python -m llm_client.check` 给出可操作提示 |
| 本地模型 | `ollama` | ❌ 放弃（不下载权重） | 已改走远程模型；真要本地化只需换 `DEEPSEEK_BASE_URL` 指向内网网关/Ollama 兼容层 |
| 长期记忆 | `mem0ai` | ⬜ 预留 | 自研人设卡 |
| 容器化 | Docker | ⬜ 预留（Day15） | — |

## 3. 预留机制（代码层面）

三处，都是为了"现在不装也能跑，装了行为不变"：

**① HTTP 层延迟导入**
`transport/http/fastapi_adapter.py` 不在模块顶层 import fastapi/pydantic；`create_app()` 内部才导入，
缺包时抛：

```python
DependencyMissing: 缺少可选依赖 'fastapi'：Day14 的 HTTP 层属于预留能力，
核心链路（测试 / CLI / 评估 / A-B）不需要它。
需要时统一安装：pip install -r requirements-optional.txt
```

注意：该模块**不能**加 `from __future__ import annotations`——Pydantic 模型定义在
`create_app()` 内，注解必须在定义路由时求值成真实类型。

**② 测试按能力跳过**
`tests/test_day14_api.py` 用 `skipUnless(HTTP_STACK_AVAILABLE)`；没装 fastapi
时跑全量测试仍然全绿（92 个用例里 skip 15 个，其余 77 个照常跑）。

已验证的两种情况：

```text
装了 fastapi/uvicorn/httpx   → Ran 92 tests  OK
模拟全部缺失（blocked import）→ Ran 92 tests  OK (skipped=15)
```

**③ 有一个用例专门验证降级**
`OptionalDependencyTests` 用 `mock.patch("builtins.__import__")` 模拟缺包，
断言报错里带 `requirements-optional`。**降级路径本身要有测试**，否则它只会在
别人 clone 你的代码时第一次失效。

## 4. 统一安装

```powershell
cd D:\code_2026\AI_Agent\agent_studio_demo

# 只装已启用的能力（fastapi / uvicorn / httpx）
python -m pip install -r requirements-optional.txt

# 验证：核心链路
python -m unittest discover -s tests -t . -p "test_*.py"

# 验证：HTTP 层
python -m api.main
```

> 当前这台开发机为了验证 Day14，已经装过 `fastapi / uvicorn / httpx`。
> 想还原成"纯净环境"（回到只有标准库的状态）执行：
>
> ```powershell
> python -m pip uninstall -y fastapi uvicorn httpx pydantic starlette
> python -m unittest discover -s tests -t . -p "test_*.py"   # 应为 OK (skipped=15)
> ```

取消注释某个预留项后，**必须同时补三样东西**，否则就是把依赖变成事故：

1. 该项的降级路径（缺包时给出可操作提示）；
2. 对应测试的 `skipUnless`（没装时不影响其他人跑全量）；
3. 本节表格里把状态从 ⬜ 改成 ✅。

## 5. 版本策略

- 预留文件里写**下限**（`>=`）而不是精确版本：学习项目不需要 lock 文件，
  但也不要写死到补丁号，否则换个 Python 小版本就装不上。
- 真要交付，再生成 lock（`pip freeze > requirements-lock.txt`）并提交，
  那是"可复现"的需求，不是"能跑"的需求。

## 6. 为什么不现在全装上

| 现在装 | 代价 |
|---|---|
| Qdrant / Chroma | 服务或本地二进制；20 条样例下词法检索已满分（见 `rag/ab_test.py`），没有可验证收益 |
| Ollama | 需要拉模型权重；当前 `HeuristicReplyModel` 已能跑通全链路，换成模型只改 factory 一行 |
| mem0 | 多一层抽取不确定性；人设卡事实量太小，接了反而增加 badcase 排查成本 |
| sentence-transformers | 首次下载数百 MB；真实收益是"同义改写召回"，但基准只有 2 条改写 query，**样本不足以下结论** |

一句话：**没有度量就没有收益，加了只是把复杂度提前。**
