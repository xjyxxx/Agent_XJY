# 提交到 GitHub

> 现状：`D:\code_2026\AI_Agent` 已经是 git 仓库（`main` 分支，尚无提交），
> 132+ 个文件已暂存，`.env` 已被忽略。缺的只有**你的提交身份**和**远程地址**。

## 0. 先确认身份（只需一次）

```powershell
cd D:\code_2026\AI_Agent
git config user.name  "你的名字"
git config user.email "你的邮箱"
```

想全局生效（以后所有仓库都用）：

```powershell
git config --global user.name  "你的名字"
git config --global user.email "你的邮箱"
```

## 1. 生成提交（20 个语义提交，不用手敲）

```powershell
cd D:\code_2026\AI_Agent
.\scripts\publish.ps1 -Name "你的名字" -Email "你的邮箱"
```

它做四件事：

1. 校验 `.env` **确实被忽略且未被跟踪**，否则直接中止；
2. 按模块分组提交（仓库配置 → 文档 → day1 解析 → … → 传输层 → 测试 → 快照）；
3. 自检：打印已跟踪文件数、确认 `.env` 不在其中；
4. 没有远程地址时，提示下一步。

> 执行策略报错时（首次运行脚本常见）：
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish.ps1 -Name "..." -Email "..."
> ```

**关于历史**：这是"把现有快照按模块分组提交"，commit 时间就是现在，**不是伪造 15 天演进**。
不要让脚本去改 `GIT_COMMITTER_DATE`——被看出来是诚信问题，比没有历史更糟。

## 2. 在 GitHub 建空仓库

网页：<https://github.com/new>

- **不要勾** "Add a README file"、".gitignore"、license —— 否则远程已有提交，首次 push 会冲突；
- 建议选 **Private**：仓库根目录有两份你的面试准备文档，公开前先想清楚；
- 记下地址，形如 `https://github.com/<你>/<repo>.git`。

## 3. 关联远程并推送

```powershell
cd D:\code_2026\AI_Agent
git remote add origin https://github.com/<你>/<repo>.git
git branch -M main
git push -u origin main
```

或者交给脚本一次做完（推荐，它会顺带做安全检查）：

```powershell
.\scripts\publish.ps1 -Name "你的名字" -Email "你的邮箱" `
  -RemoteUrl "https://github.com/<你>/<repo>.git"
```

## 4. 认证（HTTPS 与 SSH 二选一）

### HTTPS（最省事）

Windows 装了 Git 就带 **Git Credential Manager**：第一次 `git push` 会弹窗让你登录 GitHub，
之后凭据存进系统凭据管理器。若提示不支持密码认证，用 **Personal Access Token** 代替密码：

1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. 勾 `repo`（私有仓库）→ 生成 → 复制（只显示一次）
3. `git push` 时用户名填 GitHub 用户名，密码填 **这个 token**

### SSH（一次配置，长期免密）

```powershell
ssh-keygen -t ed25519 -C "你的邮箱"      # 一路回车，默认路径
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub | Set-Clipboard
# 粘贴到 https://github.com/settings/keys → New SSH key
ssh -T git@github.com                    # 看到 Hi <你>! 就通了
git remote set-url origin git@github.com:<你>/<repo>.git
git push -u origin main
```

## 5. 推送后自检（30 秒）

```powershell
git log --oneline | Measure-Object -Line      # 应有 20 行
git ls-files | Measure-Object -Line           # 约 134 个文件
git ls-files | Select-String "\.env$"         # 必须为空
git check-ignore -v agent_studio_demo/.env    # 应显示 agent_studio_demo/.gitignore:6:.env
```

打开仓库页面确认：

- `agent_studio_demo/web/index.html` 在；
- **`.env` 不在文件列表里**；
- README 能正常显示（根目录 `README.md` 是项目说明）。

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `fatal: repository not found` | 地址错 / 没权限 / 私有仓库未登录 | 检查 URL；`git remote -v` |
| `Updates were rejected` | 建库时勾了 README | `git pull --rebase origin main` 后再 push |
| 推送后看到 `.env` | 曾用 `git add -f` 强行加过 | `git rm --cached agent_studio_demo/.env` + 提交；**并立刻轮换 key** |
| 中文文件名乱码 | Windows 控制台编码 | `git config --global core.quotepath false` |
| 每次都要输密码 | 未装/未用凭据管理器 | 用 PAT + `git config --global credential.helper manager` |

## 7. 安全底线（这次特别重要）

- **这个 DeepSeek key 已经在聊天记录里出现过，务必去 <https://platform.deepseek.com> 轮换**；
- `.env` 永远不要提交；脚本每次都会检查，但别依赖脚本兜底；
- 如果**不小心推上去了**：立刻轮换 key（历史里的密钥删不干净），再 `git rm --cached` + 提交；
- 仓库设成 Private 时也要按"会公开"的标准对待密钥。

## 8. 之后怎么提交

```powershell
git add -A
git commit -m "feat: 一句话说清改了什么"
git push
```

约定（够用了）：`feat:` 新功能、`fix:` 修 bug、`docs:` 文档、`test:` 测试、`chore:` 杂项。
一个提交只做一件事——这样以后回滚和 review 才有意义。
