# Windows 快捷入口：先构建检查，再启动服务并打开前端
#
#   .\run.ps1
#   .\run.ps1 --port 8100 --provider deepseek
#   .\run.ps1 --no-open --server granian
#
# 真正的逻辑在 manage.py（跨平台），这里只做转发。

param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[xx] 找不到 python，请先安装 Python 3.10+ 并加入 PATH" -ForegroundColor Red
    exit 1
}

Push-Location $here
try {
    & python manage.py run @Rest
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
