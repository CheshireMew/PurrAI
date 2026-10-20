param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = 'D:\Tools\venvs\PurrAI\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请按 README 创建 D:\Tools\venvs\PurrAI 虚拟环境。' }
Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -m purrai.cli init-db
    if ($LASTEXITCODE -ne 0) { throw '数据库迁移失败。' }
    & $pythonPath -m purrai.cli run --port $Port
} finally { Pop-Location }
