# 语义检索与人工盲测：实验结果

> 目标：把"改写也能召回"和"像不像"这两件事，从"没法判断"变成"有数据、有工具"。
> 结论先说：**检索侧试了扩写，没有净收益；真正缺的是语义 embedding。盲测工具已就绪，等 3 位标注者。**

---

## 一、先修基准（不然任何改动都测不出来）

原基准只有 13 条 query、语料 10 条 target 发言，其中 same_situation 组 9/9 是"近逐字匹配"，
hard 组只有 2 条（0/2 vs 2/2 的 Fisher p=0.333，统计上不可用）。

新增：

| 文件 | 内容 |
|---|---|
| `fixtures/chat_multi_topic.txt` | 50 条双人聊天，6 个话题（电影/午饭/加班/旅行/跑步/借钱/猫/团建） |
| `fixtures/retrieval_eval_large.json` | 21 条标注 query：9 same_situation + **9 hard 改写** + 3 no_match |

现在 hard 组有 9 条，能分辨差异了。

## 二、A/B 结果（50 条语料 / 18 条有答案的 query）

```text
python -m rag.ab_test --dataset large --embedder hashing --expander llm

retriever                            n  hit@1  hit@3    mrr    fp
lexical                             18  0.667  0.778  0.724 0.000
lexical+expand（手写同义词）          18  0.667  0.778  0.724 0.000
lexical+llm（LLM 扩写）              18  0.667  0.722  0.707 0.000
vector:hashing-256                  18  0.611  0.667  0.639 0.333
hybrid(lexical+synonym+hashing)     18  0.556  0.778  0.662 0.000

hard 组 hit@1：lexical 0.333 / +synonym 0.333 / +llm 0.333
```

**LLM 查询扩写没有提升**（hit@3 还略降）。为了确认这不是实现问题，做了上界诊断：
对 9 条 hard query，把"原句 + 每个扩写关键词"各查一次，取最好名次：

| 结果 | 条数 |
|---|---|| 关键词能改善名次 | 2（rank2→1、rank5→4） |
| 无变化 | 7 |
| **关键词也召回不到** | **4** |

也就是说：**即使有一个完美的重排器，hard 组的上界也只有 ~4/9（0.44）**。
原因很具体——问题在上文压根没有语义信号：

```text
query : 假期想去海边玩几天
上文  : 十号前后，请三天假够玩        ← 没有"海边/假期"
答案  : 我看看能不能请
```

只有 embedding 能把"海边"和"三亚"、"假期"和"请假"连起来。词法 + 扩写做不到。

另外试过**多路检索 + RRF 融合**（原句一路、每个关键词一路）：hit@1 掉到 0.222，
误报率升到 1.0——短关键词命中会淹没原句信号。实现保留在 `MultiQueryRetriever` 里作为对照，
**默认不启用**，注释写明了原因。

## 三、结论与建议

| 方案 | 现状 | 建议 |
|---|---|---|
| 词法（现默认） | hard hit@1 0.333，误报 0 | 保持默认 |
| LLM 扩写 | 无净收益，每轮多一次调用 | 不接入主链路（代码保留，`--expander llm` 可复现） |
| 哈希向量 | 比词法差，且误报 1/3 | 仅作 A/B 对照 |
| **真语义 embedding** | 未接（DeepSeek 无 embeddings；你不下载本地模型） | 若要解决，需**另一家 key**（OpenAI/阿里/硅基流动），走 `HttpEmbedder` |

一句话：**"改写召回"这条卖点目前不成立**，但它不成立的原因是"没有 embedding 供应商"，
不是"没实现"。这一点写在 README 的已知限制里，别对外宣称。

## 四、人工盲测（L3）

### 题单怎么出的

`python -m evaluator.blind_test build --out runtime/blind`

- 10 轮固定对话；
- 每轮给两个回复：**有版**（完整链路：人设卡 + 历史证据）vs **无版**（同一个模型，
  但不给人设、不给证据，只要求"简短回复"）；
- 左右随机，防止位置偏置；答案存在 `blind_key.json`（不给标注者）。

实测样例（说明对比是有效的）：

```text
r01 用户：王哥今晚加班，晚饭你自己先吃？
    A: 哈哈知道了                                    ← 有版
    B: 好的，那我先吃了，你忙完早点回来。              ← 无版（助手腔）
r03 用户：在吗
    A: 在的，有什么可以帮你的？                        ← 无版
    B: 在呀                                          ← 有版
```

### 怎么算

```powershell
python -m evaluator.blind_test score runtime/blind/blind_sheet.csv
```

输出每个人的准确率、两两 Cohen kappa、Fleiss kappa，并给判定：

- `kappa >= 0.6` 可用；`0.4–0.6` 勉强；`< 0.4` **题目有歧义，先改题**。

### 现在的结果（**模拟，不是人工结论**）

用 LLM judge 填了一遍（`simulate` 子命令），只为验证流程：

```text
轮数 10；标注者 3
人均准确率 0.7
两两 Cohen kappa 1.0（三人同源，必然为 1）
Fleiss kappa = 1.0
```

**能说的**：判官在 7/10 轮里选了"有版"，高于随机（5/10）。
**不能说的**：这就是"人工盲测结论"。真实结论需要 3 位**真人**各填一列；
三个人独立填完，kappa 才有意义。

### 怎么组织（建议）

1. 把 `blind_sheet.csv` 发给 3 位同事/朋友，只发 `round/用户说/选项A/选项B` 四列；
2. 每人独立填 `rater_1/2/3` 一列（A 或 B），**不要讨论**；
3. 收齐后 `score` 一次；
4. kappa < 0.4 就改题（比如把"哪条更像"改成"哪条更像小林，且说明理由"），别硬下结论。

## 五、复现命令

```powershell
cd D:\code_2026\AI_Agent\agent_studio_demo

# 检索 A/B（无 LLM）
python -m rag.ab_test --dataset large --embedder none --expander synonym

# 检索 A/B（LLM 扩写，需要 key）
python -m rag.ab_test --dataset large --embedder none --expander llm

# 盲测
python -m evaluator.blind_test build --out runtime/blind
python -m evaluator.blind_test simulate runtime/blind/blind_sheet.csv   # 仅验证流程
python -m evaluator.blind_test score runtime/blind/blind_sheet_rater.csv
```

---

## 六、语义重排：不接 embedding 也能补上的那一半

没有第二家 key、又不想下载本地模型，所以走了另一条路：**词法先召回 8 条，再让模型判断
"这句话和候选是不是在说同一件事"**，一次调用给整批候选打分。

```powershell
python -m rag.ab_test --dataset large --embedder none --expander none --rerank llm
```

| 检索器 | hit@1 | hit@3 | MRR | 误报 | hard 组 hit@1 |
|---|---|---|---|---|---|
| lexical | 0.667 | 0.778 | 0.724 | 0.000 | 0.333 (3/9) |
| lexical+llm-rerank | **0.778** | **0.833** | **0.806** | 0.000 | **0.556 (5/9)** |

这是第一次真正把 hard 组拉起来。但要如实说明边界：

- **不是 embedding**：召回集合仍由词法决定，模型只能重排"已经召回到的候选"。
  上界诊断里那 4 条"关键词也召不到"的 query，重排同样救不了（这次救回来 2 条）。
- **未达统计显著**：hard 组 3/9 → 5/9，只有 2 条改变，McNemar 精确检验 p ≈ 0.5。
  方向对，但 n=9 撑不起"提升 X%"的结论。
- **成本**：每轮多一次调用（约 +0.5s、+¥0.0002）。默认关闭，`[features] rerank_llm = 1` 才开。

真正的语义召回（"海边"↔"三亚"）仍然要 embedding；重排是"花小钱先把能救的救回来"。

---

## 七、消融：检索到底有没有用（实测）

前面所有讨论都在比"排序好不好"，但真正该先回答的是：**检索对最终回复有没有影响**。
方法：同一句话、同一模型、temperature=0，只改一件事——prompt 里有没有检索到的证据。

```powershell
python -m bench.ablate_retrieval --limit 10 --judge
```

| 指标 | 结果 |
|---|---|
| 10 轮里"有证据命中"的 | 4 轮（命中率 40%） |
| 命中轮的回复是否改变 | **4/4 全变** |
| 未命中轮 | 必然相同（没有东西可注入） |
| 证据字符复用率 | 开 **37.5%** vs 关 **5.0%**（7.5 倍） |
| 盲评（判官不知道哪版有证据） | **有证据 4 胜 / 无证据 0 胜** |

具体例子：

```text
输入：周末的电影票别忘了      有证据=不会        无证据=啥电影呀
输入：你最近是不是特别忙      有证据=嗯嗯有点     无证据=啥呀
输入：王哥今晚加班…          有证据=哈哈知道了   无证据=嗯嗯好
```

**结论修正**：检索不是装饰品——命中时它确实让回复更像（4/4 盲评胜、复用率 7.5 倍）。
但它价值的**上限由命中率决定（40%）**，而不是排序质量：

- 没命中的 6 轮，排序再准也没用；
- 命中的 4 轮，纯词法 top-1 已经是"该用的那句"，重排没有改变它们。

所以"扩 hard 基准 / 继续调重排"确实是低价值投入：它优化的是那 9 条硬改写，
而真实对话里 60% 的轮次根本召不回东西。要提升这个场景，该动的是**命中率**
（同义词表、更多语料、embedding），不是排序。

> 注意：temperature=0 也**不是完全可复现**——同一句"你最近是不是特别忙"两次跑
> 分别得到"还行吧"和"啥呀"。所以 4/4 只能当方向性证据，别写成"提升 X%"。
