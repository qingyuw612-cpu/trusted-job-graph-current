$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

& (Join-Path $PSScriptRoot "start_local_full.ps1") -Check
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "`n本地完整图谱状态："
$GraphHealth = Invoke-RestMethod "http://127.0.0.1:8010/api/health"
$EvolutionHealth = Invoke-RestMethod "http://127.0.0.1:8070/api/v1/evolution/health"

[pscustomobject]@{
    graph_backend = $GraphHealth.backend
    graph_status = $GraphHealth.status
    graph_jds = $GraphHealth.counts.jds
    graph_roles = $GraphHealth.counts.roles
    graph_skills = $GraphHealth.counts.skills
    normalized_graph = $GraphHealth.summary.normalized_graph
    evolution_source = $EvolutionHealth.source.label
    evolution_usable_jds = $EvolutionHealth.source.usable_jds
    active_normalization_run = $EvolutionHealth.source.active_normalization_run_id
    active_ingestion_runs = $EvolutionHealth.source.active_ingestion_runs
    active_processing_runs = $EvolutionHealth.source.active_processing_runs
    spark_lite_configured = $EvolutionHealth.spark_lite_configured
} | Format-List
