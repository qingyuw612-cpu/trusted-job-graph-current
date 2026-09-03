$ErrorActionPreference = "Continue"

$root = "C:\Users\Mully.Min\Desktop\xiaotiao"
$graphConfig = Join-Path $root "trusted-job-graph-main\config\neo4j_connection.json"
$displayGraph = Join-Path $root "display_graph.json"

Write-Host "== Talent Graph Local Services Check =="
Write-Host ""

if (Test-Path $graphConfig) {
  Write-Host "[OK] Neo4j config:" $graphConfig
} else {
  Write-Host "[MISSING] Neo4j config:" $graphConfig
  Write-Host "  Copy trusted-job-graph-main\config\neo4j_connection.example.json to neo4j_connection.json and fill password."
}

if (Test-Path $displayGraph) {
  Write-Host "[OK] Display graph package:" $displayGraph
} else {
  Write-Host "[MISSING] Display graph package:" $displayGraph
}

function Test-Http($Name, $Url) {
  try {
    $res = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
    Write-Host "[OK] $Name" $Url
    Write-Host "  " ($res.Content.Substring(0, [Math]::Min(160, $res.Content.Length)))
  } catch {
    Write-Host "[DOWN] $Name" $Url
    Write-Host "  " $_.Exception.Message
  }
}

Test-Http "Graph API" "http://127.0.0.1:8010/api/health"
Test-Http "Resume API" "http://127.0.0.1:8000/health"
Test-Http "Frontend" "http://127.0.0.1:8090/index.html"

Write-Host ""
Write-Host "Graph import command:"
Write-Host "cd $root\trusted-job-graph-main"
Write-Host "python display_graph_handoff.py import --package `"$displayGraph`" --neo4j-config config\neo4j_connection.json"
Write-Host ""
Write-Host "Graph serve command:"
Write-Host "python display_graph_handoff.py serve --neo4j-config config\neo4j_connection.json"
