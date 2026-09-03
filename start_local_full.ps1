param(
    [switch]$Check,
    [string]$Neo4jConfig = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SiblingResumePython = Join-Path (Split-Path $ProjectRoot -Parent) "resume-analysis-agent\.venv\Scripts\python.exe"
$BundledResumePython = Join-Path $ProjectRoot "resume-analysis-agent\.venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $SiblingResumePython) { $SiblingResumePython } else { $BundledResumePython }

if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到简历分析服务 Python：$SiblingResumePython 或 $BundledResumePython"
}

if ([string]::IsNullOrWhiteSpace($Neo4jConfig)) {
    $Neo4jConfig = Join-Path $ProjectRoot "config\neo4j_connection.json"
}
if (-not (Test-Path -LiteralPath $Neo4jConfig)) {
    throw "找不到本地完整 Neo4j 配置：$Neo4jConfig"
}

Push-Location $ProjectRoot
try {
    if ($Check) {
        & $Python -B start_demo.py --check
    } else {
        & $Python start_demo.py --neo4j-config $Neo4jConfig
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
