set -u
python3 - <<'PY'
import json
d = json.load(open('/var/lib/biliduty/last-round.json', encoding='utf-8'))
text = d['comments'][0]['text']
print('rpid:', d['comments'][0]['rpid'])
print('len:', len(text))
print('starts:', text[:24])
PY
echo "== config =="
python3 - <<'PY'
import json
d = json.load(open('/var/lib/biliduty/config.json', encoding='utf-8'))
print({k: d.get(k) for k in ('searchDays','searchPerKeyword','minAgeHours','candidatePool')})
PY
