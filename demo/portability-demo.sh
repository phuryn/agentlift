#!/usr/bin/env bash
# agentlift portability demo - one neutral agent folder, audited across three
# providers and compiled to two runtime formats. Everything here is OFFLINE:
# no API key, nothing deployed. It shows the compiler, not a live deploy.
#
#   pip install agentlift   # or: pip install -e .  (from a clone)
#   ./demo/portability-demo.sh
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENT="$ROOT/examples/team"
OUT="${1:-$ROOT/demo/out}"
# Use the installed CLI if present, else the module form from a checkout.
AL="agentlift"; command -v agentlift >/dev/null 2>&1 || AL="python -m agentlift.cli"
rm -rf "$OUT"

echo "### 1. AUDIT - how portable is examples/team across providers?"
$AL audit "$AGENT" --targets anthropic,google,openai

echo
echo "### 2. EXPORT -> Anthropic YAML (the shape the official 'ant' CLI consumes)"
$AL export anthropic-yaml "$AGENT" --out "$OUT/anthropic"

echo
echo "### 3. EXPORT -> Google ADK (Vertex AI Agent Engine scaffold, preview)"
$AL export google-adk "$AGENT" --out "$OUT/google"

echo
echo "Done. One folder -> audited across 3 providers and compiled to 2 formats."
echo "Artifacts: $OUT"
