param(
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$ManifestPath = Join-Path $ProjectRoot "deploy\FINAL_FRONTEND_RELEASE.json"
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "Final frontend manifest is missing: $ManifestPath"
}

$Manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
if ($Manifest.status -ne "FINAL_LOCKED") {
    throw "Frontend release is not locked."
}

$Failures = @()
foreach ($entry in $Manifest.files.PSObject.Properties) {
    $RelativePath = $entry.Name.Replace("/", [IO.Path]::DirectorySeparatorChar)
    $Path = Join-Path $ProjectRoot $RelativePath
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $Failures += "missing: $($entry.Name)"
        continue
    }
    $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
    $Expected = [string]$entry.Value
    if ($Actual -ne $Expected.ToUpperInvariant()) {
        $Failures += "changed: $($entry.Name)"
    }
}

if ($Failures.Count -gt 0) {
    throw "Final frontend lock failed for release $($Manifest.release_id): $($Failures -join '; '). Review the change and explicitly update the manifest before publishing."
}

Write-Output "FINAL_FRONTEND_OK $($Manifest.release_id)"
