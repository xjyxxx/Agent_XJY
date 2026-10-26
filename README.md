# Agent-Studio-Demo
> AI Agent 15天学习实战项目，成熟开发者向，以LangGraph为核心，可插拔模块化智能体项目。
> 配套学习文档：`AI Agent 开源项目源码阅读指南 + Demo 整合方案 + Mermaid 架构图.md`

## 📖 项目简介
本项目是15天AI Agent学习计划对应的落地 Demo。**只做一个应用场景：双人聊天语气分身（Persona Replay）**。
主链路锁定为五步，不改成调研、客服或万能角色扮演：
1. 解析双人私聊（先不做客群）
2. 你指定「我是 A，AI 扮 B」
3. 检索 B 历史上怎么回过类似的话
4. 按人设卡（称呼、句长、口头禅）起草
5. Critic 拦住助手腔、串台、以及记录里没有的事
核心目标不是堆模块，而是把这五步用工具、RAG、记忆、LangGraph 和 SSE 跑通。
- 学习思路：编码优先，开源项目源码仅作为对照参考，不通读全仓库。
- 核心框架：LangGraph；配套组件：Pydantic、FastAPI、Qdrant/Chroma、mem0（可选记忆）
- 模型支持：OpenAI / DeepSeek / Ollama；**真实私聊优先本地模型**
- 交付形态：可本地运行的后端服务，SSE 打出检索依据与回复
- 黄金样例：合成记录里「老王 vs 小林」；用户当老王，AI 学小林的短句和「哈哈」，不要助手腔

### 学习定位
1. Agent应用开发：掌握单Agent检索生成、多节点人设/生成/审查、工具系统、RAG、记忆
2. Agent底层理解：手写简易ReAct，理解状态机与Checkpoint
3. 产品化落地：导入解析、会话、SSE、评估「像不像对方」

### 使用底线
仅用于个人学习或已征得对方同意的记录。禁止用真人语气分身去冒充、诈骗或公开传播。真实聊天记录不进 git、不进示例。

## ✨ 核心功能
- ✅ 双人聊天导入与说话人选择
- ✅ ReAct：解析记录、检索历史发言/问答对
- ✅ 人设卡：称呼、口头禅、句长、标点习惯
- ✅ 分层记忆：本场对聊窗口 + 可持久人设/事实
- ✅ RAG：按「一条发言」切，避免把两人的话揉进同一 chunk
- ✅ 多节点：Planner（导入时人设）+ Worker（检索起草）+ Critic（语气/幻觉）
- ✅ Human-in-the-loop：敏感回复或 Critic 不通过时暂停
- ✅ 评估：语气像不像、是否串台、是否编造事实
- ✅ FastAPI + SSE：导入 / 建会话 / 流式回复 / 人工裁决
- ⬜ Docker 打包（Day15，未做）

> **当前进度**：Day1–Day14 已实现并通过 91 个单测；Day15（Docker）未做。
> 详细的分日说明、指标与已知限制见 `agent_studio_demo/README.md`。

## 📂 目录结构

实际落地的结构（与规划稿的差异：手写同构 `StateGraph` 代替 langgraph；
mem0 / 向量库 / Docker 尚未接入）：

```text
agent_studio_demo/
├── agent/                  # 编排
│   ├── graph.py            # 同构 StateGraph：条件边 / 步数预算 / interrupt+resume
│   ├── nodes.py            # planner / retrieve / draft / critic / human_review
│   ├── state.py            # AgentState（可 JSON 序列化）
│   ├── control.py          # GraphInterrupt
│   ├── react.py            # Day2 手写 ReAct（对照用，不进主链路）
│   └── demo.py             # CLI 入口
├── api/                    # Day14 兼容入口（薄封装）
│   ├── main.py             # 转发到 transport 包
│   └── service.py          # 会话服务（session_id = thread_id，同会话串行）
├── transport/              # 传输层：框架可替换 + TCP/UDP 同生命周期
│   ├── handlers.py         # 纯业务：(payload) -> (status, payload) / SSE 事件
│   ├── events.py           # EventBus → UdpSink / JsonlSink / NullSink
│   ├── bundle.py           # Transport：adapter × server 两个正交维度
│   ├── udp_listen.py       # UDP 观测事件接收端
│   └── http/               # fastapi_adapter（预留依赖）/ stdlib_adapter（零依赖）
├── bench/
│   └── loadtest.py         # 三后端压测（服务器独立进程，避免 GIL 污染）
├── ingest/
│   └── session_store.py    # 导入会话缓存：解析/人设/索引只做一次
├── llm_client/
│   ├── heuristic.py        # 离线确定性模型（候选池）
│   ├── deepseek.py         # OpenAI 兼容 Chat Completions
│   ├── scripted.py         # 脚本模型（评估与测试用）
│   └── factory.py          # auto / heuristic / deepseek
├── memory/
│   ├── persona.py          # PersonaCard + ShortTermMemory
│   └── checkpoint.py       # SQLite 按 thread_id 存 state（线程安全）
├── rag/
│   ├── history_index.py    # 词法索引（bigram + 上下文加权）
│   ├── embedding.py        # 哈希向量 / HTTP embedding
│   ├── retriever.py        # 词法 / 扩写 / 向量 / RRF 融合
│   └── ab_test.py          # Day10 检索 A/B
├── tools/
│   ├── chat_parser.py      # 双人聊天解析
│   └── history_tools.py    # 只读工具白名单
├── evaluator/              # Day12
│   ├── rules.py            # 客观指标
│   ├── llm_judge.py        # RuleJudge / LLMJudge + 聚合 + 门禁
│   └── run.py              # 10 轮评估 CLI
├── fixtures/               # 合成聊天 + 评估集 + 检索基准
├── tests/                  # 91 个单测
├── runtime/                # 本地 SQLite（gitignored）
├── requirements.txt
└── README.md
```

