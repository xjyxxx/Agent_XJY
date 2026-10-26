# 服务器后端实测：granian / uvicorn / 标准库

> 结论先说：**在同一个业务负载下，"换服务器"的收益远小于"换适配器"。**
> granian 与 uvicorn 的一轮扮演吞吐基本持平（≈555 turns/s），
> 而标准库适配器是 ≈918 turns/s —— 因为它不做 pydantic 校验、不进 ASGI 栈。

## 1. 为什么会有这份文档

一开始的直觉是"Rust 服务器一定更快"。实测后发现，对这个 API 来说**框架层开销
大于服务器层开销**，所以必须把两组数字分开看，否则会得出错误结论。

## 2. 候选与取舍

| 库 | 类型 | 为什么选 / 不选 |
|---|---|---|
| **granian** | Rust ASGI/RSGI 服务器 | ✅ 选：装得上（Windows 有 wheel）、HTTP/1.1+2、WebSocket、多 worker、Prometheus 指标、静态文件；且不改业务代码 |
| uvicorn | 纯 Python ASGI 服务器 | ✅ 保留：生态默认、调试友好、API 稳定 |
| 标准库 `http.server` | 零依赖 | ✅ 保留：无框架时的兜底，实测最快 |
| uvloop | 事件循环加速 | ❌ 不支持 Windows |
| socketify.py | uWebSockets 绑定 | ❌ 非 ASGI，要重写全部路由；生态薄 |
| Litestar / Robyn | 全功能框架 | ❌ 要重写 8 个端点；收益是 OpenAPI/DI，而 FastAPI 已有 |
| hypercorn + aioquic | **HTTP/3** | ⬜ 预留：这是"HTTP over UDP"的正确路径，需要 QUIC 客户端才能验证 |
| Starlette 裸用 | 轻框架 | ⬜ 可选：能省掉 pydantic，但也会丢掉校验 |

granian 的两个硬约束（都在代码注释里）：

1. **必须主线程**：它注册信号处理器，线程内 `serve()` 直接抛
   `ValueError: signal only works in main thread`。所以本项目用**子进程**起它。
2. **端口要显式**：子进程是独立进程，父进程读不到 ephemeral 端口，所以
   `tcp_port=0` 时父进程先自己挑一个空闲端口再传下去。

## 3. 测试方法（可复现）

```powershell
python -m bench.loadtest --duration 2 --health-workers 8 --turn-workers 4
```

- **服务器在独立进程**（`python -m bench.serve`），压测在父进程。
  最初的版本把 uvicorn 和压测放在同一进程，GIL 直接把它压到 383 rps，
  granian 因为天生是子进程反而"显得快 7 倍"——那是测错，不是测出差距。
- 连接用 keep-alive，`http.client` 每线程一条。
- 两段负载：`GET /health`（纯传输）、`POST /messages`（端到端一轮，含检索/生成/Critic）。
- 预热 20 次请求后才开始计时。

## 4. 结果（本机：Windows / Python 3.10.4 / 三轮一致）

| 后端 | 适配器 | health rps | p50 | p95 | turns/s | p50 | p95 | 错误 |
|---|---|---|---|---|---|---|---|---|
| stdlib | stdlib | **7327** | 1.01 ms | 1.92 ms | **918.6** | 4.2 ms | 5.7 ms | 0 |
| uvicorn | fastapi | 2467 | 3.12 ms | 4.29 ms | 563.6 | 7.0 ms | 9.2 ms | 0 |
| granian | fastapi | 3020 | 2.53 ms | 3.72 ms | 555.4 | 7.1 ms | 9.0 ms | 0 |

第二轮 / 第三轮的波动在 ±3%，排序不变：

```text
stdlib  7327 / 918.6      uvicorn 2467 / 563.6      granian 3020 / 555.4
stdlib  6852 / 918.6      uvicorn 2533 / 567.4      granian 3011 / 554.2
```

## 5. 怎么解读

1. **纯传输路径（/health）**：granian 比 uvicorn 快约 **22%**（3020 vs 2467）。
   这是 Rust HTTP 层真实的收益，出现在"不碰业务"的地方。
2. **端到端（turns/s）**：两者基本持平（555 vs 564，差异在噪声内）。
   因为一轮的时间花在 Python 侧：pydantic 反序列化 + Starlette 中间件 + 业务逻辑。
   换服务器只是把 0.3ms 的 HTTP 解析变成 0.2ms，占 7ms 一轮的零头。
3. **标准库适配器反而最快**：health 是 uvicorn 的 **2.9 倍**，turn 是 **1.6 倍**。
   它省掉的是"每次都跑一遍 pydantic 模型校验 + ASGI 事件循环调度"。
   代价是没有 OpenAPI、没有依赖注入、要手写 422。

**所以选型逻辑不是"谁快用谁"，而是"你要不要为生态付这个钱"：**

| 你的诉求 | 选择 |
|---|---|
| 要 OpenAPI / 生态 / 团队熟悉 | FastAPI + granian（比 uvicorn 快 22% 的传输层，白拿） |
| 要极限吞吐、接口数量少而稳定 | stdlib 适配器（快 60%–190%） |
| 要 HTTP/2、WebSocket、Prometheus | granian（这些 uvicorn 需要额外组件） |
| 完全没有第三方依赖 | stdlib 适配器 |

## 6. 还能再快吗（按收益排序）

1. **去掉每请求的 pydantic 校验**：FastAPI 支持 `msgspec` 序列化（`msgspec` 有 Rust 核心）；
   或者只在边界做一次 dict 校验（我们的 `ApiHandlers` 已经这么做了）。
2. **SSE 改真流式**：现在 `reply_delta` 是回放，首字延迟没有改善。
3. **异步模型客户端**：同步 `urllib` 换 `httpx.AsyncClient`，一轮里串行等待变成并发。
4. **多 worker**：granian `--workers N`（但同一会话必须串行，别把同一 thread 打散到多进程）。
5. **HTTP/3**：hypercorn + aioquic，属于另一个量级的改动，先预留。

## 7. 一句话给面试

> "我们量过：给同一个 FastAPI 应用换 granian，传输层快 22%，端到端没变——
> 因为瓶颈在框架层不在服务器层。所以最后我保留了双适配器：要生态就用 FastAPI+granian，
> 要吞吐就用零依赖适配器，同一套契约测试保证行为一致。"
