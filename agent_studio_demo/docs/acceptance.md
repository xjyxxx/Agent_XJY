# 7 条验收：实测记录

> 对应方案文档「场景验收标准（Demo 完成的定义）」。
> 脚本：`tests/acceptance.py`（可重复执行，失败退出码 1）

## 复现

```powershell
cd D:\code_2026\AI_Agent\agent_studio_demo

# 真模型（.env 里有 key）
python -m tests.acceptance --provider deepseek

# 离线（不花钱，但回复是模板）
python -m tests.acceptance --provider heuristic
```

## 实测输出（2026-10-23，DeepSeek）

```text
provider=deepseek

[PASS] 1. 导入：解析、列说话人、建人设卡
        speakers=['老王', '小林'] messages=20 persona=小林 avg_len=4.2
[PASS] 2. 主路径：检索后按对方语气回复
        user='王哥今晚加班，晚饭你自己先吃？' reply='哈哈知道了' verdict=pass model=DeepSeekReplyModel
[PASS] 3. 依据可见：SSE 能打出召回原句
        order=['persona', 'retrieved', 'draft', 'critique', 'reply_delta', 'reply_delta']…
        top1='哈哈知道了' line=15 score=0.5274
[PASS] 4. 语气约束：Critic 拦助手腔与串台
        assistant_tone=拦下；crosstalk=拦下
[PASS] 5. 反幻觉：记录里没有的事不编造
        我们什么时候结的婚→'啥呀，没说过啊'
        你银行卡密码是多少→'啥呀，问这个'
        我们不是约好去三亚吗→'啥呀，谁说的'
[PASS] 6. 记忆：多轮不丢主题 + 同 thread 续跑
        turn_count=8 window=6 summary_len=46 重新加载 turn_count=8
[PASS] 7. 服务：import / sessions / stream 演示完整一轮
        turn.reply='哈哈知道了' sse=['persona', 'retrieved', 'draft', 'critique', 'reply_delta']…done

7/7 条通过
```

## 每条在验什么

| # | 验收项 | 判定方式 | 失败时会怎样 |
|---|---|---|---|
| 1 | 导入 | speakers 恰好两个、消息数 20、人设卡 speaker 正确 | 群聊/单人记录会被 `require_two_speakers` 拒绝 |
| 2 | 主路径 | 回复非空 + Critic `verdict=pass` | 助手腔/串台会被判 `review` 并中断 |
| 3 | 依据可见 | SSE 有 `retrieved` 事件且 top1 带 `line_number` | 检索为空时事件仍是 `retrieved`，但 hits=[] |
| 4 | 语气约束 | 注入"作为AI…"和"抄对方原话"两种回复，都必须被拦 | 规则漏判 → 该轮直接 `ok` 放行 |
| 5 | 反幻觉 | 三条诱导问题，回复不得含年份/密码/"记录"字眼 | 出现数字或元话语即失败 |
| 6 | 记忆 | 连续 8 轮后 `turn_count=8`、窗口 6、摘要非空、重载仍在 | 摘要为空 → 第 7 轮起主题丢失 |
| 7 | 服务 | 三个接口串起来，SSE 首事件 `persona`、末事件 `done` | 顺序错或 404 即失败 |

## 注意（别把结论扩大）

- 第 5 条通过的是**prompt 层**的反幻觉（模型自己躲开了）。规则层仍拦不住"没有数字的编造"，
  例如"我们上个月办过婚礼"——这类需要 LLM judge 兜底（见 `docs/plan-status.md` 缺失 B-4）。
- **第 5 条是必要条件，不是充分条件**：`--provider heuristic` 也 7/7 通过，因为模板模型
  从不编造——但它也不回答（"我们什么时候结的婚" → "可以呀，你定时间～"）。
  这种"答非所问"在 L2 语义判定里会被判成幻觉（实测 16.7%，见 `real-model-report.md`）。
  所以这条验收验的是"没有编造"，验不了"答得像人"。
- 第 6 条的摘要是**抽取式**（把被挤出去的那一轮压成一行），不是生成式摘要；
  长度上限 400 字，只保留尾部。
