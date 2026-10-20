set -u
TOKEN=$(sed -n 's/^BILIDUTY_TOKEN=//p' /etc/biliduty/env)
BEFORE=$(curl -sS http://127.0.0.1:8787/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["lastRound"].get("startedAt",""))')
echo "before_startedAt=$BEFORE"
echo "== POST /run =="
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}' http://127.0.0.1:8787/run
echo
for i in $(seq 1 100); do
  S=$(curl -sS http://127.0.0.1:8787/status)
  RUN=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin)["running"])')
  NOWSTART=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin)["lastRound"].get("startedAt",""))')
  if [ "$RUN" = "False" ] && [ "$NOWSTART" != "$BEFORE" ]; then
    echo "== done (round $((i*3))s) =="
    echo "$S" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d["lastRound"], ensure_ascii=False, indent=2)); print("quota:", d["quota"])'
    break
  fi
  sleep 3
done
echo "== log tail =="
tail -25 /var/lib/biliduty/duty.log
