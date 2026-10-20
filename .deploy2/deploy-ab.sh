set -euo pipefail
echo "== install pipeline.py =="
install -m 644 -o biliduty -g biliduty /root/new2/pipeline.py /opt/biliduty/src/biliduty/pipeline.py
install -m 644 -o biliduty -g biliduty /root/new2/pipeline.py /opt/biliduty/.venv/lib/python3.10/site-packages/biliduty/pipeline.py

echo "== bump searchPerKeyword to 30 =="
python3 - <<'PY'
import json
path = "/var/lib/biliduty/config.json"
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
data["searchPerKeyword"] = 30
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("config searchPerKeyword =", data["searchPerKeyword"])
PY

echo "== restart =="
systemctl restart bili-duty.service
sleep 3
systemctl is-active bili-duty.service
curl -sS --max-time 5 http://127.0.0.1:8787/healthz; echo
/opt/biliduty/.venv/bin/python -c "import biliduty.pipeline as p; print('pipeline ok'); print('has empty_reason:', hasattr(p, '_empty_reason'))"
cat /var/lib/biliduty/config.json
