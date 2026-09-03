param(
    [string]$Neo4jConfig = "",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SiblingResumePython = Join-Path (Split-Path $ProjectRoot -Parent) "resume-analysis-agent\.venv\Scripts\python.exe"
$BundledResumePython = Join-Path $ProjectRoot "resume-analysis-agent\.venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $SiblingResumePython) { $SiblingResumePython } else { $BundledResumePython }

if ([string]::IsNullOrWhiteSpace($Neo4jConfig)) {
    $Neo4jConfig = Join-Path $ProjectRoot "config\neo4j_connection.json"
}
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $ProjectRoot "..\deploy\display-package"
}

if (-not (Test-Path -LiteralPath $Neo4jConfig)) {
    throw "找不到完整本地 Neo4j 配置：$Neo4jConfig"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

Push-Location $ProjectRoot
try {
    & $Python -u display_graph_handoff.py export `
        --neo4j-config $Neo4jConfig `
        --output-dir $OutputDir
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
