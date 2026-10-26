# 传输层：业务 / 协议 / 观测分离

> 目标：把 HTTP 框架变成**可替换的适配器**，并让**一个进程同时提供 TCP 服务与 UDP 上报**。

## 1. 先把一个前提说清楚

**HTTP 不能跑在 UDP 上。** HTTP/3 是 QUIC（基于 UDP），但那需要专门的 QUIC 库，
属于预留依赖，不是这里手写的东西。SSE 也只能走 TCP。

所以"TCP + UDP 组合"的合理分工只有一种：

| 通道 | 承载 | 特性要求 |
|---|---|---|
| **TCP**（HTTP/1.1） | 请求/响应、SSE、HITL 裁决、checkpoint | 不丢、有序、要确认 |
| **UDP** | 节点级观测事件（trace） | 可丢、无序、fire-and-forget |

把业务数据放 UDP 是错的（丢帧=丢状态）；把观测数据放 HTTP 也是错的（为了上报拖慢主链路）。

## 2. 分层

```mermaid
flowchart TB
    subgraph 业务["业务层（无框架依赖）"]
        H[ApiHandlers<br/>入参 dict → (status, payload)<br/>SSE 用事件迭代器]
    end

    subgraph 协议["协议适配层（可替换）"]
        F[fastapi_adapter<br/>可选依赖]
        S[stdlib_adapter<br/>零依赖]
    end

    subgraph 观测["观测通道（UDP）"]
        B[EventBus]
        U[UdpSink]
        J[JsonlSink]
        N[NullSink]
    end

    subgraph 组合["生命周期"]
        T[Transport<br/>start/stop]
    end

    Client[客户端] -->|TCP| F
    Client -->|TCP| S
    F --> H
    S --> H
    H --> Svc[SessionService → Graph]
    Svc -->|on_event| B
    B --> U
    B --> J
    B --> N
    T --> F
    T --> S
    T --> B
```

为什么值得这么切：

- **业务可测**：`ApiHandlers` 用普通单测覆盖，不用起服务器；
- **依赖可控**：fastapi 只是其中一种适配器，缺了就用标准库的；
- **协议可换**：将来上 gRPC / 内部 RPC，只加一个适配器，业务不动。

## 3. 两个适配器是等价的（有测试保证）

`tests/test_transport.py::ContractTests` 里 `exercise_contract()` 是同一份断言，
分别跑在 FastAPI 适配器和标准库适配器上：

```text
/health 200 · import 200 · create 201 · turn 200 · get 200
404 未知会话 · 404 payload · 409 对未中断会话 resume · 422 非法 action · 422 空消息
delete 200 → get 404
```

如果哪天有人把业务逻辑写进某个适配器，这组测试会立刻红。

两者的差异（写在 stdlib 适配器头部）：

| 维度 | fastapi_adapter | stdlib_adapter |
|---|---|---|
| 依赖 | fastapi / uvicorn / pydantic | 只用标准库 |
| OpenAPI | 有 | 无 |
| 并发模型 | ASGI（单 worker 可并发） | 一连接一线程 |
| SSE 收尾 | chunked | `Connection: close` |
| 422 来源 | Pydantic 校验 | 适配器里捕获 `ValidationError` |

## 4. UDP 观测事件长什么样

```json
{
  "ts": "2026-10-22T11:32:54.649+08:00",
  "event": "critic",
  "ms_since_prev": 9.849,
  "status": "ok",
  "turn": 1,
  "rewrites": 0,
  "hits": 1,
  "reply_len": 5,
  "verdict": "pass",
  "problems": [],
  "steps": ["planner", "retrieve", "draft", "critic"]
}
```

**故意不含对话内容**——没有 `reply`、没有 `user_input`，只有长度和判定结果。
观测数据是最容易被打包旁路、被写进第三方平台的，默认脱敏比事后擦除便宜。

`ms_since_prev` 是"距上一个事件"的间隔；因为事件在节点返回之后才发，
所以它 ≈ 上一个节点的耗时。想要精确的单节点耗时，得让图自己计时（TODO）。

## 5. 运行

```powershell
# 终端 A：接收观测事件
python -m transport.udp_listen --port 8126

# 终端 B：一个进程，TCP 8000 + UDP 上报到 8126
python -m transport --port 8000 --udp-port 8126 --adapter auto

# 终端 A 会实时看到：
# 11:32:54.649 planner   status=ok hits=0
# 11:32:54.659 retrieve  status=ok hits=1  ms_prev=9.657
# 11:32:54.669 draft     status=ok hits=1  ms_prev=8.088
# 11:32:54.679 critic    status=ok verdict=pass ms_prev=9.849
# 11:32:54.689 done      status=ok turn=1
```

其他入口：

```powershell
python -m transport --adapter stdlib --port 0    # 强制零依赖适配器，端口随机
python -m transport --jsonl runtime/trace.jsonl  # 同时落本地 JSONL
python -m api.main                               # 兼容老入口（= transport 的薄封装）
uvicorn --factory transport.http.fastapi_adapter:create_app --port 8000
```

## 6. 取舍（为什么不做别的）

| 方案 | 为什么没选 |
|---|---|
| 全链路 asyncio + httpx.AsyncClient | 正确方向，但要重写模型客户端的调用路径；当前用线程池 + 队列包住，先把接口跑对 |
| gRPC / WebSocket | 这个场景是单向流 + 一请求一应答，SSE 足够；gRPC 会引入 protobuf 编译链 |
| 手写 QUIC / HTTP/3 | 不现实，需要专用库；hypercorn + aioquic 是预留方案，且要有 QUIC 客户端才能验证 |
| 把 trace 也走 HTTP 上报 | 每轮 4–9 个事件会把主链路拖慢，且失败重试逻辑要自己写 |
| granian 线程内嵌 | 它注册全局信号处理器，线程内 `serve()` 直接崩；已改为**子进程**，见 `docs/performance.md` |

## 7. 已知限制

1. `ms_since_prev` 是近似值（见上）。
2. UDP 无重传：高负载下事件会丢，这是设计取舍，不是 bug。
3. stdlib 适配器一连接一线程，并发上去要换 ASGI。
4. SSE 仍是"回合结束后按块回放"，不是 token 级流式。
5. 没有鉴权/限流（见 README「已知限制」）。
