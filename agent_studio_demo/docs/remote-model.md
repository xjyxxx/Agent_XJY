# 远程模型：不下载本地模型，直接挂 DeepSeek（或任何 OpenAI 兼容端点）

> 结论：**可以，而且不需要改代码**。`llm_client/deepseek.py` 只依赖 OpenAI 的
> `/chat/completions` 协议，所以 DeepSeek、月之暗面、通义、vLLM、Ollama 的
> OpenAI 兼容层、公司内网网关都能直接接。

## 1. 三步接通

```powershell
cd D:\code_2026\AI_Agent\agent_studio_demo

# ① 配 key（二选一）
Copy-Item .env.example .env      # 然后编辑 .env 填 DEEPSEEK_API_KEY
$env:DEEPSEEK_API_KEY = "sk-..." # 或只在当前会话临时设置

# ② 预检：网络 / 鉴权 / 余额 / 延迟，一次说清
python -m llm_client.check

# ③ 用远程模型跑一轮
python -m agent.demo --provider deepseek --message "王哥今晚加班，晚饭你自己先吃？"
```

预检成功长这样：

```text
config: {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
         "key_present": true, "key_hint": "sk-***abcd"}
[OK] deepseek-flash @ https://api.deepseek.com latency=812.4ms reply='OK'
```

失败时退出码可区分，能直接进 CI：

| 退出码 | 含义 | 你该做什么 |
|---|---|---|
| 0 | 通 | — |
| 1 | 网络/鉴权/余额/限流失败 | 看提示：401 查 key、402 充值、429 降并发 |
| 2 | 没配 key | 配 `.env`，或用 `--provider heuristic` 离线跑 |

## 2. 它到底验证了什么（没有 key 也能验的部分）

真实 API 需要你的 key，所以协议层用**同协议的本地假服务**验证（`tests/fake_deepseek.py`）：

| 验证点 | 测试 |
|---|---|
| 请求体带 system+user、`stream` 标志正确 | `test_turn_runs_through_the_remote_endpoint` |
| `Authorization: Bearer` 头、401/402/429 映射成中文可读错误 | `test_auth_failure_is_mapped` 等 |
| SSE 帧解析（`data: {...}` + `[DONE]`） | `test_generate_stream_yields_multiple_deltas` |
| 预检 CLI 三个退出码 | `test_exit_*` |
| 免费额度用完（402）不会被当成网络错误 | `test_balance_failure_is_mapped` |

**换成真 key 后同一段代码直接可用**——这是"用 mock 验证协议"而不是"用 mock 假装功能"。

## 3. 真流式（`STREAM=1`）

远程模型的价值一半在首 token 延迟，所以实现了 token 级流式：

```powershell
$env:STREAM = "1"
python -m transport --port 8000
curl.exe -N --get "http://127.0.0.1:8000/sessions/<sid>/stream" --data-urlencode "message=在吗"
```

事件顺序会变（这是流式的必然代价）：

| 模式 | 顺序 |
|---|---|
| 默认（回放） | `retrieved → draft → critique → reply_delta* → done` |
| `STREAM=1` | `retrieved → draft → reply_delta* → critique → done` |

Critic 重写时会先发一个 `reply_reset`，再发第二版的增量，否则两版正文会拼在一起：

```text
event: reply_delta  {delta:"作为"}      ← 第一版（会被判不合格）
event: critique     {verdict:"rewrite"}
event: reply_reset  {reason:"critic_rewrite"}   ← 客户端应清空
event: reply_delta  {delta:"哈哈"}      ← 第二版
event: done         {reply:"哈哈知道了"}
```

实现要点：`draft_node` 的 `on_delta(delta, reset)` 是**按调用**传递的，不按图传递——
用 `ContextVar` 而不是实例属性，这样同一张编译好的图能被多个请求并发使用。

## 4. 切别的模型（改一个环境变量）

```powershell
# 通义 / 月之暗面 / 任何 OpenAI 兼容网关
$env:DEEPSEEK_BASE_URL = "https://your-gateway.example.com/v1"
$env:DEEPSEEK_MODEL    = "your-model-name"
python -m llm_client.check    # 先证明这条路通，再换到业务上
```

代码里 `.../v1` 会被自动去掉（适配器自己拼 `/chat/completions`），所以带不带 `/v1` 都行。

## 5. 为什么不再下载 Ollama

| 方案 | 下载量 | 首跑成本 | 隐私 | 本项目选择 |
|---|---|---|---|---|
| Ollama + 7B | 4–8 GB 权重 | 下载 + 显存/内存占用 | 全本地 | ❌ 放弃（只为跑通链路不值得） |
| DeepSeek 远程 | 0 | 一个 key | 内容离开本机 | ✅ 合成样例用它 |
| 离线 heuristic | 0 | 0 | 不出本机 | ✅ 无 key 时的兜底 |

**隐私边界（必须写清楚）**：走远程 API 意味着对话内容会离开本机。
仓库里只有合成样例（老王/小林），所以没问题；**真实私聊记录不要配远程模型**——
这也是原方案里"真实记录优先本地模型"那条约束的由来。真要接真实数据，再回到 Ollama
或内网网关（适配器不用改，只换 `DEEPSEEK_BASE_URL`）。

## 6. 已知限制

1. `deepseek-flash` 这个默认模型名是按 DeepSeek 当前命名写的；换供应商记得改 `DEEPSEEK_MODEL`。
2. 没有重试/退避：429 会直接把错误抛给调用方（提示里写了降并发）。要生产化得加指数退避。
3. 没有 token 计数与成本统计（响应里能拿 `usage`，但没落库）。
4. 流式只在 `STREAM=1` 下开启；默认仍是回放（保证事件顺序稳定）。
5. DeepSeek 没有公开的 embeddings 接口，所以语义检索那层仍走 `EMBEDDING_*`（另一家或本地）。
