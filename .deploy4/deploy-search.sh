set -euo pipefail
echo "== install =="
for f in bilibili.py pipeline.py config.py; do
  install -m 644 -o biliduty -g biliduty "/root/new3/$f" "/opt/biliduty/src/biliduty/$f"
  install -m 644 -o biliduty -g biliduty "/root/new3/$f" "/opt/biliduty/.venv/lib/python3.10/site-packages/biliduty/$f"
done
echo "== config =="
python3 - <<'PY'
import json
path = "/var/lib/biliduty/config.json"
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
data["searchPerKeyword"] = 60
data["minAgeHours"] = 12
data["candidatePool"] = 60
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print({k: data[k] for k in ("searchDays", "searchPerKeyword", "minAgeHours", "candidatePool", "candidatesPerRound")})
PY

echo "== restart =="
systemctl restart bili-duty.service
sleep 3
systemctl is-active bili-duty.service
/opt/biliduty/.venv/bin/python -c "import biliduty.pipeline, biliduty.bilibili, biliduty.config; print('import ok')"

echo "== trigger a round =="
TOKEN=$(sed -n 's/^BILIDUTY_TOKEN=//p' /etc/biliduty/env)
BEFORE=$(curl -sS http://127.0.0.1:8787/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["lastRound"].get("startedAt",""))')
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}' http://127.0.0.1:8787/run
echo
for i in $(seq 1 300); do
  S=$(curl -sS http://127.0.0.1:8787/status)
  RUN=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin)["running"])')
  NOWSTART=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin)["lastRound"].get("startedAt",""))')
  if [ "$RUN" = "False" ] && [ "$NOWSTART" != "$BEFORE" ]; then
    echo "== done (~$((i*3))s) =="
    echo "$S" | python3 -c 'import json,sys; d=json.load(sys.stdin); lr=d["lastRound"]; print(json.dumps({k:lr.get(k) for k in ("startedAt","summary","noSubtitle","reviewed","searched","candidates","comments","errors")}, ensure_ascii=False, indent=2)); print("quota:", d["quota"])'
    break
  fi
  sleep 3
done
echo "== log tail =="
tail -15 /var/lib/biliduty/duty.log
