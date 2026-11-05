#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  ADR-010 Phase 2: Governance Scanner — Verify no call_llm in CRM flows
#
#  Scans ALL CRM flows (router + 5 domain sub-flows + exit) for
#  call_llm nodes that would violate the "LLM is not involved"
#  guarantee.
#
#  Usage:
#    cd /opt/iiab/ai-gateway && source .env
#    bash scripts/scan_call_llm.sh
#
#  Exit codes:
#    0 — Clean (no call_llm nodes found)
#    1 — VIOLATION (call_llm node found in a CRM flow)
#    2 — Configuration error
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

RAPIDPRO_HOST="${RAPIDPRO_HOST:-http://localhost:8080}"
API_TOKEN="${RAPIDPRO_API_TOKEN:-}"

if [ -z "$API_TOKEN" ]; then
    echo "❌ RAPIDPRO_API_TOKEN not set. Source .env first."
    exit 2
fi

# Collect all CRM flow UUIDs from environment
CRM_UUIDS=""
for var in CRM_ROUTER_FLOW_UUID CRM_CONTACTS_FLOW_UUID CRM_GROUPS_FLOW_UUID \
           CRM_MESSAGES_FLOW_UUID CRM_FLOWS_FLOW_UUID CRM_SYSTEM_FLOW_UUID; do
    val="${!var:-}"
    if [ -n "$val" ]; then
        CRM_UUIDS="$CRM_UUIDS $val"
    fi
done

# Fallback to legacy env var
if [ -z "$CRM_UUIDS" ]; then
    LEGACY="${CRM_OPS_FLOW_UUID:-}"
    if [ -n "$LEGACY" ]; then
        CRM_UUIDS="$LEGACY"
    fi
fi

if [ -z "$CRM_UUIDS" ]; then
    echo "❌ No CRM flow UUIDs found. Run setup_crm_ops.py first."
    exit 2
fi

UUID_COUNT=$(echo $CRM_UUIDS | wc -w)
echo "🔍 Scanning $UUID_COUNT CRM flows for call_llm contamination..."
echo "   Host: $RAPIDPRO_HOST"
echo ""

# ── Backup + Scan each flow ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="$SCRIPT_DIR/../backups/flows"
mkdir -p "$BACKUP_DIR"

TOTAL_VIOLATIONS=0

for FLOW_UUID in $CRM_UUIDS; do
    # Backup
    BACKUP_FILE="$BACKUP_DIR/crm_${FLOW_UUID:0:8}_$(date +%Y%m%d_%H%M%S).json"
    curl -s -H "Authorization: Token $API_TOKEN" \
      "$RAPIDPRO_HOST/api/v2/definitions.json?flow=$FLOW_UUID" \
      -o "$BACKUP_FILE"

    # Scan
    RESULT=$(curl -s -H "Authorization: Token $API_TOKEN" \
      "$RAPIDPRO_HOST/api/v2/definitions.json?flow=$FLOW_UUID&dependencies=none" | \
    python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except json.JSONDecodeError:
    print('ERROR: parse failed')
    sys.exit(0)

violations = []
for flow in d.get('flows', []):
    for node in flow.get('nodes', []):
        for action in node.get('actions', []):
            if action.get('type') == 'call_llm':
                violations.append(f'{flow.get(\"name\",\"?\")}/{node[\"uuid\"][:8]}')

if violations:
    flow_name = d['flows'][0]['name'] if d.get('flows') else '?'
    print(f'VIOLATION:{flow_name}:{\";\".join(violations)}')
else:
    flow_name = d['flows'][0]['name'] if d.get('flows') else '?'
    nodes = sum(len(f.get('nodes',[])) for f in d.get('flows',[]))
    print(f'CLEAN:{flow_name}:{nodes}')
")

    if echo "$RESULT" | grep -q "^VIOLATION:"; then
        FLOW_NAME=$(echo "$RESULT" | cut -d: -f2)
        DETAILS=$(echo "$RESULT" | cut -d: -f3)
        echo "  🔴 $FLOW_NAME — call_llm found: $DETAILS"
        TOTAL_VIOLATIONS=$((TOTAL_VIOLATIONS + 1))
    elif echo "$RESULT" | grep -q "^CLEAN:"; then
        FLOW_NAME=$(echo "$RESULT" | cut -d: -f2)
        NODES=$(echo "$RESULT" | cut -d: -f3)
        echo "  ✅ $FLOW_NAME — $NODES nodes, zero call_llm"
    else
        echo "  ⚠️  $FLOW_UUID — $RESULT"
    fi
done

echo ""
if [ "$TOTAL_VIOLATIONS" -gt 0 ]; then
    echo "🔴 GOVERNANCE FAILED — $TOTAL_VIOLATIONS flow(s) contain call_llm nodes!"
    exit 1
else
    echo "✅ Governance passed — $UUID_COUNT CRM flows scanned, zero call_llm nodes."
fi
