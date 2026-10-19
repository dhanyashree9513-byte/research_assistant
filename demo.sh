#!/usr/bin/env bash
# Demo: one research run through the API. Start the server first:
#   docker compose up -d && uv run uvicorn api:app --app-dir src --port 8001
set -e
TOPIC=${1:-"Should a 10-person startup use Kubernetes?"}
echo "▶ POST /research  \"$TOPIC\""
ID=$(curl -s -X POST localhost:8001/research -H 'content-type: application/json' \
  -d "{\"topic\": \"$TOPIC\"}" | python3 -c "import json,sys; print(json.load(sys.stdin)['run_id'])")
echo "  run_id = $ID"
echo "▶ GET /research/$ID/events   (live)"
curl -s -N localhost:8001/research/$ID/events | sed -n 's/^data: //p' | python3 -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line); print('  ', e['type'].ljust(8), e.get('msg', ''))"
echo "▶ GET /research/$ID"
curl -s localhost:8001/research/$ID | python3 -c "
import json, sys
d = json.load(sys.stdin); r = d['report']
print('  ', d['outcome'], '|', d['tokens'], 'tokens')
if r:
    print('\n  SUMMARY:', r['summary'])
    for s in r['sections']:
        print('\n  ##', s['heading'])
        for f in s['findings']:
            print('   -', f['claim'], '\n     ', f['source_urls'][0])"
echo; echo "▶ trace: localhost:8001/research/$ID/trace"
