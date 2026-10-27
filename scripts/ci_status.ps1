# 查 GitHub Actions 最近几次运行（含失败注解）
#
#   .\scripts\ci_status.ps1
#   .\scripts\ci_status.ps1 -Repo xjyxxx/Agent_XJY -Limit 10
#
# 为什么用 curl 而不是 Invoke-RestMethod：本机 PowerShell 走代理访问 api.github.com
# 会 403，curl 直连正常。
#
# 如果嫌未登录的 API 有频控（60 次/小时），设一个只读 token：
#   $env:GITHUB_TOKEN = "ghp_xxx"

param(
    [string]$Repo = "xjyxxx/Agent_XJY",
    [int]$Limit = 5
)

$ErrorActionPreference = "Continue"
$headers = @("-H", "User-Agent: dsh")
if ($env:GITHUB_TOKEN) {
    $headers += @("-H", "Authorization: Bearer $env:GITHUB_TOKEN")
}

function Get-Json($url) {
    $raw = & curl.exe -sS -m 20 @headers $url 2>&1
    if ($LASTEXITCODE -ne 0) { throw "curl 失败：$url" }
    return ($raw -join "`n") | ConvertFrom-Json
}

$runs = Get-Json "https://api.github.com/repos/$Repo/actions/runs?per_page=$Limit"
if (-not $runs.workflow_runs) {
    Write-Host "还没有任何运行记录。若是新仓库，去 Settings → Actions → General 允许工作流。"
    exit 0
}

foreach ($run in $runs.workflow_runs) {
    $sha = $run.head_sha.Substring(0, 7)
    $when = ([datetime]$run.created_at).ToLocalTime().ToString("MM-dd HH:mm")
    $icon = if ($run.conclusion -eq "success") { "PASS" } elseif ($run.conclusion -eq "failure") { "FAIL" } else { $run.status.ToUpper() }
    Write-Host ("[{0}] #{1} {2} {3} {4}" -f $icon, $run.run_number, $when, $sha, $run.head_branch)

    if ($run.conclusion -ne "failure") { continue }

    $jobs = Get-Json ("https://api.github.com/repos/$Repo/actions/runs/" + $run.id + "/jobs")
    foreach ($job in $jobs.jobs) {
        if ($job.conclusion -ne "failure") { continue }
        Write-Host ("    job: {0}" -f $job.name)
        $failed = @($job.steps | Where-Object { $_.conclusion -eq "failure" } | ForEach-Object { $_.name })
        if ($failed.Count) { Write-Host ("      step: {0}" -f ($failed -join ", ")) }

        $annotations = Get-Json ("https://api.github.com/repos/$Repo/check-runs/" + $job.id + "/annotations")
        foreach ($note in $annotations) {
            if ($note.annotation_level -eq "warning") { continue }
            $msg = ($note.message -split "`n")[0]
            Write-Host ("      !! {0} :: {1}" -f $note.path, $msg)
        }
    }
}
