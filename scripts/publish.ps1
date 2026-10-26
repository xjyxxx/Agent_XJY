# 把当前快照提交到 Git，并（可选）推送到 GitHub
#
#   .\scripts\publish.ps1 -Name "你的名字" -Email "you@example.com"
#   .\scripts\publish.ps1 -Name "..." -Email "..." -RemoteUrl "https://github.com/<你>/<repo>.git"
#
# 设计原则：
# 1. **不伪造历史**：这是"把现有快照按模块分组提交"，commit 时间就是现在，
#    不是模拟 15 天演进。真实历史只能靠以后每次改动都提交。
# 2. **不碰密钥**：提交前检查 `.env` 是否被忽略；被跟踪就直接中止。
# 3. **可重复执行**：已有提交就跳过提交阶段，只做 remote/push。
#
# 兼容性：Windows PowerShell 5.1 对"数组参数 + 原生命令 stderr"的处理和 7 不一样，
# 所以这里**不封装** git，直接调用 + 逐条检查 $LASTEXITCODE，并对可能分页的命令加 --no-pager。

param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Email,
    [string]$RemoteUrl = "",
    [switch]$SkipPush
)

$ErrorActionPreference = "Continue"

function Fail($message) {
    Write-Host "[xx] $message" -ForegroundColor Red
    exit 1
}
function Info($message) { Write-Host "[..] $message" -ForegroundColor Cyan }
function Ok($message) { Write-Host "[ok] $message" -ForegroundColor Green }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "找不到 git" }

if (-not (Test-Path .git)) {
    Info "初始化仓库（main 分支）"
    & git init -b main 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "git init 失败" }
}

& git config user.name $Name 2>$null
if ($LASTEXITCODE -ne 0) { Fail "写入 git user.name 失败" }
& git config user.email $Email 2>$null
if ($LASTEXITCODE -ne 0) { Fail "写入 git user.email 失败" }
Ok "提交身份：$Name <$Email>"

# --- 密钥闸门 -------------------------------------------------------------
$ignored = @(& git check-ignore agent_studio_demo/.env 2>$null)
if ($ignored.Count -eq 0) {
    Fail "agent_studio_demo/.env 没有被 .gitignore 忽略，拒绝继续（先修好 .gitignore）"
}
$trackedNow = @(& git --no-pager ls-files 2>$null)
foreach ($path in $trackedNow) {
    if ($path -eq "agent_studio_demo/.env") {
        Fail "agent_studio_demo/.env 已被跟踪，拒绝继续（先 git rm --cached agent_studio_demo/.env）"
    }
}
Ok ".env 已被忽略，且未被跟踪"

# --- 分组提交（只在还没有任何 commit 时执行）-------------------------------
& git rev-parse --verify HEAD 2>$null | Out-Null
$hasCommit = ($LASTEXITCODE -eq 0)

if (-not $hasCommit) {
    & git reset -q 2>$null

    # 按目录分组：一个 commit 一个模块，便于 review 和 revert
    $groups = @(
        @{ msg = "chore: 仓库配置（.gitignore / .gitattributes）"; paths = @(".gitignore", ".gitattributes") },
        @{ msg = "docs: 学习方案、源码阅读清单与架构图"; paths = @("AI Agent 开源项目源码阅读指南 + Demo 整合方案 + Mermaid 架构图.md", "README.md") },
        @{ msg = "feat(day1): 双人聊天解析器、多格式导入与合成样例"; paths = @("agent_studio_demo/tools", "agent_studio_demo/fixtures/chat_laowang_xiaolin.txt") },
        @{ msg = "feat(day2-3): 手写 ReAct 与只读工具白名单"; paths = @("agent_studio_demo/agent/react.py") },
        @{ msg = "feat(day4): 人设卡、短期记忆与滚动摘要"; paths = @("agent_studio_demo/memory") },
        @{ msg = "feat(day5-6): 状态图、条件边与 checkpoint"; paths = @("agent_studio_demo/agent/graph.py", "agent_studio_demo/agent/state.py", "agent_studio_demo/agent/control.py") },
        @{ msg = "feat(day7-9): Planner/Worker/Critic 与单轮竖切 CLI"; paths = @("agent_studio_demo/agent/nodes.py", "agent_studio_demo/agent/engine.py", "agent_studio_demo/agent/langgraph_runtime.py", "agent_studio_demo/agent/demo.py", "agent_studio_demo/ingest") },
        @{ msg = "feat(day10): 词法/向量检索、查询扩写与检索 A/B"; paths = @("agent_studio_demo/rag") },
        @{ msg = "feat(day11-12): HITL 中断恢复、10 轮评估与人工盲测工具"; paths = @("agent_studio_demo/evaluator") },
        @{ msg = "feat(day14): 双适配器、TCP+UDP 传输层与 SSE"; paths = @("agent_studio_demo/api", "agent_studio_demo/transport") },
        @{ msg = "feat(day14): 前端单页（原生 JS，无构建）"; paths = @("agent_studio_demo/web") },
        @{ msg = "feat: DeepSeek(OpenAI 兼容) 适配器、离线兜底与预检"; paths = @("agent_studio_demo/llm_client") },
        @{ msg = "feat(day15): 一键构建运行、依赖声明与容器镜像"; paths = @("agent_studio_demo/manage.py", "agent_studio_demo/run.ps1", "agent_studio_demo/run.sh", "agent_studio_demo/Dockerfile", "agent_studio_demo/.dockerignore", "agent_studio_demo/requirements.txt", "agent_studio_demo/requirements-optional.txt", "agent_studio_demo/.env.example") },
        @{ msg = "test: 单测、7 条验收脚本与 UI 冒烟"; paths = @("agent_studio_demo/tests") },
        @{ msg = "bench: 三后端压测与延迟/token 探针"; paths = @("agent_studio_demo/bench") },
        @{ msg = "docs: 依赖策略、传输层、性能、真实模型与检索报告"; paths = @("agent_studio_demo/docs", "agent_studio_demo/README.md", "agent_studio_demo/.gitignore", "agent_studio_demo/fixtures/chat_export.html", "agent_studio_demo/fixtures/chat_export.jsonl", "agent_studio_demo/fixtures/chat_wechat_like.csv", "agent_studio_demo/fixtures/chat_multi_topic.txt", "agent_studio_demo/fixtures/eval_rounds.json", "agent_studio_demo/fixtures/retrieval_eval.json", "agent_studio_demo/fixtures/retrieval_eval_large.json") },
        @{ msg = "docs: 面试准备与进阶篇"; paths = @("面试准备｜Persona Replay 项目讲述与追问应对手册.md", "进阶篇｜从讲得住到值钱.md") },
        @{ msg = "chore(tooling): 提交与发布辅助脚本"; paths = @("scripts") },
        @{ msg = "chore: 根目录单文件 Demo 与空包（历史遗留）"; paths = @("demo.py", "agent") },
        @{ msg = "chore: llm_agents 源码快照（MIT，仅作阅读对照）"; paths = @("agent_studio_demo/third_party") }
    )

    $count = 0
    foreach ($group in $groups) {
        $existing = @()
        foreach ($path in $group.paths) {
            if (Test-Path $path) { $existing += $path }
        }
        if ($existing.Count -eq 0) { continue }

        & git add -- @existing 2>$null
        $staged = @(& git --no-pager diff --cached --name-only 2>$null)
        if ($staged.Count -eq 0) { continue }

        & git commit -q -m $group.msg 2>$null
        if ($LASTEXITCODE -ne 0) { Fail "提交失败：$($group.msg)" }
        $count++
        Write-Host "    [$count] $($group.msg)"
    }

    # 兜底：把还没进版本库的文件收进来（例如根目录散落脚本）
    & git add -A 2>$null
    $staged = @(& git --no-pager diff --cached --name-only 2>$null)
    if ($staged.Count -gt 0) {
        & git commit -q -m "chore: 其余文件" 2>$null
        if ($LASTEXITCODE -ne 0) { Fail "兜底提交失败" }
        $count++
        Write-Host "    [$count] chore: 其余文件"
    }
    Ok "共创建 $count 个提交"
}
else {
    Info "已有提交，跳过分组提交（只做 remote/push）"
}

# --- 自检 -----------------------------------------------------------------
$trackedTotal = @(& git --no-pager ls-files 2>$null)
Ok "已跟踪文件：$($trackedTotal.Count) 个"
foreach ($path in $trackedTotal) {
    if ($path -eq "agent_studio_demo/.env") { Fail "危险：.env 进入了版本库，立即中止" }
}
Ok "确认 .env 未进入版本库"

# --- 远程 -----------------------------------------------------------------
if ($RemoteUrl -and -not $SkipPush) {
    $remotes = @(& git remote 2>$null)
    if ($remotes -contains "origin") {
        & git remote set-url origin $RemoteUrl 2>$null
    }
    else {
        & git remote add origin $RemoteUrl 2>$null
    }
    if ($LASTEXITCODE -ne 0) { Fail "设置 remote 失败：$RemoteUrl" }

    & git branch -M main 2>$null
    Info "推送到 $RemoteUrl"
    & git push -u origin main 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail "推送失败：检查仓库地址、仓库是否为空、以及凭据（PAT / Git Credential Manager）"
    }
    Ok "已推送"
}

Write-Host ""
if (-not $RemoteUrl) {
    Write-Host "下一步："
    Write-Host "  1) 在 GitHub 建一个空仓库（不要勾 README / .gitignore）"
    Write-Host "  2) .\scripts\publish.ps1 -Name `"$Name`" -Email `"$Email`" -RemoteUrl `"https://github.com/<你>/<repo>.git`""
}
else {
    Write-Host "打开仓库页面确认：文件数、.env 不在列表里、README 显示正常"
}
