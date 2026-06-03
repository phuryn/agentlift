# agentlift portability demo (Windows / PowerShell) - a twin of portability-demo.sh.
# Everything here is OFFLINE: no API key, nothing deployed. It shows the compiler,
# not a live deploy.
#
#   pip install agentlift   # or: pip install -e .  (from a clone)
#   .\demo\portability-demo.ps1
#
$ErrorActionPreference = "Stop"
$Root  = Split-Path -Parent $PSScriptRoot            # repo root (parent of demo/)
$Agent = Join-Path $Root "examples\team"
$Out   = if ($args.Count -ge 1) { $args[0] } else { Join-Path $Root "demo\out" }

# Use the installed CLI if present, else the module form from a checkout.
if (Get-Command agentlift -ErrorAction SilentlyContinue) {
    $AlExe = "agentlift"; $AlArgs = @()
} else {
    $AlExe = "python"; $AlArgs = @("-m", "agentlift.cli")
}

if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }

Write-Host "### 1. AUDIT - how portable is examples/team across providers?"
& $AlExe @AlArgs audit $Agent --targets anthropic,google,openai

Write-Host "`n### 2. EXPORT -> Anthropic YAML (the shape the official 'ant' CLI consumes)"
& $AlExe @AlArgs export anthropic-yaml $Agent --out (Join-Path $Out "anthropic")

Write-Host "`n### 3. EXPORT -> Google ADK (Vertex AI Agent Engine scaffold, preview)"
& $AlExe @AlArgs export google-adk $Agent --out (Join-Path $Out "google")

Write-Host "`nDone. One folder -> audited across 3 providers and compiled to 2 formats."
Write-Host "Artifacts: $Out"
