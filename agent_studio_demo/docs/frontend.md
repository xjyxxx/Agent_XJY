# 前端：一个页面把"可解释"摊开给你看

![界面](ui-screenshot.png)

## 1. 为什么是单个静态页，而不是 React

| 方案 | 代价 | 结论 |
|---|---|---|
| Vite + React | 要 Node、要 `npm install`、要构建产物 | ❌ 与"零构建、clone 即跑"的定位冲突 |
| 原生 JS + 三件套 | 无构建、无依赖、浏览器直接跑 | ✅ 选了 |
| Svelte/单文件组件 | 仍需要构建工具 | ❌ |

页面由**同一个服务进程**托管（`GET /` 和 `/app.js`、`/style.css`），
所以不存在跨域、也不需要单独起一个前端 dev server。`adapter=stdlib` 时也能用。

## 2. 四个区域

| 区域 | 内容 | 对应接口 |
|---|---|---|
| 步骤 1 | 聊天文件路径（**只能在项目内**）+ 说话人列表 | `POST /chats/import` |
| 步骤 2 | 选「我是」/「AI 扮演」+ 人设卡 | `POST /sessions` |
| 步骤 3 | 对话气泡（流式追加）+ 人工裁决框 | `GET /sessions/{id}/stream`、`POST .../resume` |
| 右侧 | 依据 / 判定 / 事件流 三个页签 | 来自 SSE 事件 |

右侧三个页签是这个项目的核心卖点：

- **依据**：每条召回的原文、行号、分数（`ctx` 上下文分 / `content` 回复分）——证明"不是纯 prompt 装腔"。
- **判定**：Critic 的 `verdict` 与 `problems`（`assistant_tone` / `crosstalk` / `unsupported_fact` …）。
- **事件流**：原样打印 SSE 帧，方便对照后端日志。

## 3. SSE 契约（前端按这个渲染）

```text
persona     {persona, speakers, user_speaker, target_speaker}
retrieved   {hits: [{content, context, line_number, score, ...}]}
draft       {draft}
critique    {critique: {verdict, problems, ...}}
reply_delta {delta}          ← 可能多次
reply_reset {reason}         ← Critic 要求重写时先清空
interrupt   {reason, problems, reply, allowed_actions}
done        {status, reply, turn_count, steps}
error       {detail, code}
```

前端用 `fetch` + `ReadableStream` 解析，而不是 `EventSource`：
后者断线会自动重连、把请求**重放**一遍（等于重复发消息），且无法精确中止。

## 4. 人工裁决（HITL）

`interrupt` 到达时页面弹出裁决框，三个按钮对应：

| 动作 | 效果 |
|---|---|
| 批准发送 | 原文放行，`human_decision=approve` |
| 按改写内容发送 | 用输入框内容替换正文，并同步改写短期记忆 |
| 拒绝 | 回退到中性短句 `嗯，知道了`，`status=rejected` |

## 5. 怎么起

```powershell
.\run.ps1                      # Windows：构建检查 + 启动 + 自动开浏览器
python manage.py run           # 跨平台等价命令
python manage.py run --provider deepseek --port 8100
```

`manage.py run` 会先跑一遍构建检查（Python 版本、前端文件、compileall、导入冒烟），
再起服务并打开 `http://127.0.0.1:<port>/`。

## 6. 验证脚本（可选）

`tests/ui_smoke.py` 用 **本机已装的 Edge**（`channel="msedge"`，不下载 Chromium）
跑完整交互并截图：

```powershell
python -m tests.ui_smoke http://127.0.0.1:8150/ runtime/ui-flow.png
```

实测输出：

```text
reply   : 哈哈知道了
evidence: 1
critique: 通过 未发现助手腔、串台、超长或事实问题
events  : ['session', 'persona', 'retrieved', 'draft', 'critique', 'reply_delta', ...]
js errors: none
```

依赖：`pip install playwright`（本机已装 Edge 的话不需要 `playwright install`）。
这是**开发期验证工具**，不参与运行时，也不在 `requirements.txt` 里。

## 7. 安全

- 静态资源只允许读 `web/` 目录、只允许白名单后缀；`..` 与符号链接逃逸一律拒绝
  （`transport/http/static.py`，有测试）。
- 导入路径限制在 `CHAT_ROOT`（默认项目根）内，越界返回 400——
  前端把 `path` 暴露给浏览器之后，这条不再是可选项。
- 页面不存任何密钥；模型调用都在后端。

## 8. 已知不足

1. 没有多会话列表：刷新页面要重新导入 + 建会话（state 只在内存里）。
2. `STREAM=1` 时正文先于 `critique` 出现，页面会在收到判定后**补**显示判定，
   不会回滚已经显示的正文。
3. 没有重连/断点续传：SSE 断了就这一轮结束，需要重发。
4. 移动端只是简单堆叠，没有针对小屏优化对话区高度。
