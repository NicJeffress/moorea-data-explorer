"""Recompute auto_score in result files after a change to the checks."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_sampling_eval import HERE, score
bank = {q['id']: q for q in json.loads((HERE / 'sampling-questions.json').read_text())['questions']}
for path in sys.argv[1:]:
    data = json.loads(Path(path).read_text())
    for r in data['runs']:
        if r.get('answer'):
            r['auto_score'] = score(bank[r['question']], r)
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + '\n')
    print(path.split('/')[-1], [(r['question'], sum(r['auto_score'].values()), len(r['auto_score'])) for r in data['runs']])
