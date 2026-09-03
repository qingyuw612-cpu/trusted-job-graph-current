param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkspaceRoot = (Split-Path $ProjectRoot -Parent)
$CrawlerRoot = Get-ChildItem -LiteralPath $WorkspaceRoot -Directory | Where-Object {
    Test-Path -LiteralPath (Join-Path $_.FullName "spider_zhilian_step1.py")
} | Select-Object -First 1 -ExpandProperty FullName

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "deploy\install_systemd.sh"))) {
    throw "The current directory is not a deployable TalentGraph project."
}
if ([string]::IsNullOrWhiteSpace($CrawlerRoot)) {
    throw "Crawler source directory was not found beside the project."
}

# Prevent an older frontend copy from being bundled into a production release.
# Any intentional frontend change must first be reviewed and explicitly recorded
# in deploy/FINAL_FRONTEND_RELEASE.json.
& (Join-Path $PSScriptRoot "check_final_frontend.ps1") -ProjectRoot $ProjectRoot

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $ProjectRoot "deploy\releases"
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Archive = Join-Path $OutputDirectory "talentgraph-closed-loop-$Timestamp.tar.gz"
$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$StagingRoot = [IO.Path]::GetFullPath(
    (Join-Path $TempRoot "talentgraph-release-staging-$Timestamp-$PID")
)
if (-not $StagingRoot.StartsWith($TempRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Release staging path escaped the system temporary directory."
}
$ProjectStage = Join-Path $StagingRoot "trusted-job-graph-current"
$CrawlerStage = Join-Path $StagingRoot (Split-Path $CrawlerRoot -Leaf)

# The archive keeps the workspace layout expected by install_systemd.sh. It
# intentionally excludes credentials, databases, raw user/JD data and generated
# output while including the integrated frontend and resume service in this repo.
try {
    New-Item -ItemType Directory -Path $ProjectStage, $CrawlerStage | Out-Null
    $ProjectExcludeDirs = @(
        (Join-Path $ProjectRoot ".git"),
        (Join-Path $ProjectRoot ".venv"),
        (Join-Path $ProjectRoot "__pycache__"),
        (Join-Path $ProjectRoot ".pytest_cache"),
        (Join-Path $ProjectRoot ".claude"),
        (Join-Path $ProjectRoot "output"),
        (Join-Path $ProjectRoot "raw_jd_archive"),
        (Join-Path $ProjectRoot "models\hf_cache"),
        (Join-Path $ProjectRoot "deploy\releases")
    )
    $ProjectCopyArgs = @(
        $ProjectRoot, $ProjectStage, "/E", "/XJ", "/R:1", "/W:1",
        "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XD"
    ) + $ProjectExcludeDirs + @(
        "/XF", ".coverage", ".coverage.*", "*.pyc", ".env",
        "neo4j_connection.json"
    )
    & robocopy.exe @ProjectCopyArgs | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Project staging failed; robocopy exit code: $LASTEXITCODE"
    }

    & robocopy.exe $CrawlerRoot $CrawlerStage /E /XJ /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD `
        (Join-Path $CrawlerRoot "__pycache__") /XF "*.pyc" ".env" | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Crawler staging failed; robocopy exit code: $LASTEXITCODE"
    }

    & tar.exe -czf $Archive -C $StagingRoot `
        "trusted-job-graph-current" (Split-Path $CrawlerRoot -Leaf)
    if ($LASTEXITCODE -ne 0) {
        throw "Release archive creation failed; tar exit code: $LASTEXITCODE"
    }
} catch {
    Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
    throw
} finally {
    if (
        (Test-Path -LiteralPath $StagingRoot) -and
        $StagingRoot.StartsWith($TempRoot, [StringComparison]::OrdinalIgnoreCase)
    ) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
}

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
[pscustomobject]@{
    Archive = $Archive
    Sha256 = $Hash
    SizeBytes = (Get-Item -LiteralPath $Archive).Length
    ReleaseId = "closed-loop-2026.08"
}
