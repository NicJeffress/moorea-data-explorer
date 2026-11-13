"""Save what the browser analyst really sends to /api/llm as live-request.json, without a model call.

The question runners build their own tool list; the live page adds the geo-agent map tools,
render_chart, the workspace tools and the map catalog text. This captures the browser's own
request so `run_question_eval.py --tools live` can replay its tools and system prompt.

1. Start GLEN with the provider keys blanked, so no request can reach a model:
       env ANTHROPIC_API_KEY= OPENAI_API_KEY= GLEN_ANALYST_MODEL=claude-haiku-5-5@medium \\
           .venv/bin/python -m uvicorn backend.server:app --app-dir . --port 8793
   (/api/health then reports openai_configured false.)
2. Start this sink:  .venv/bin/python evals/analyst-models/capture_live_request.py
3. Open http://127.0.0.1:8793/ (the default page, the one testers land on), run the snippet
   this script prints in the page's console once the page has finished loading (the console
   shows "[main] Loaded MCP geospatial-analyst prompt"), open the analyst and ask any question. The snippet
   answers the page itself, so nothing is sent on; it posts the request body here, and this
   script writes the fixture and exits.

The fixture keeps the request's `tools`, `system` messages, `tool_choice` and `max_tokens`,
plus the page address and the commit. The per-session `user` id and the question are dropped.
"""
import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'live-request.json'
PORT = 8794

SNIPPET = """(() => { const orig = window.fetch; window.fetch = async function (u, o) {
  const url = String(u instanceof Request ? u.url : u);
  if (!url.includes('/api/llm/')) return orig.apply(this, arguments);
  orig('http://127.0.0.1:%d/save', {method: 'POST', mode: 'no-cors', headers: {'Content-Type': 'text/plain'},
       body: JSON.stringify({page: location.href, body: o.body})});
  return new Response(JSON.stringify({choices: [{index: 0, finish_reason: 'stop', message: {role: 'assistant',
       content: '(capture stub: no model was called)'}}]}), {status: 200, headers: {'Content-Type': 'application/json'}});
}; })();""" % PORT


def fixture(page, body):
    request = json.loads(body)
    root = HERE.parents[2]
    commit = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=root, capture_output=True, text=True).stdout.strip()
    return {
        'captured': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'commit': commit, 'page': page,
        'how': 'evals/analyst-models/capture_live_request.py (browser fetch intercepted; provider keys blank)',
        'model': request.get('model'), 'tool_choice': request.get('tool_choice'), 'max_tokens': request.get('max_tokens'),
        'system': [m['content'] for m in request['messages'] if m['role'] == 'system'],
        'tools': request['tools'],
    }


class Sink(BaseHTTPRequestHandler):
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        out = fixture(data['page'], data['body'])
        OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n')
        self.send_response(204)
        self.end_headers()
        print(f'wrote {OUT.name}: {len(out["tools"])} tools, system {sum(map(len, out["system"]))} chars', flush=True)
        self.server.done = True


if __name__ == '__main__':
    print('Paste in the page console:\n' + SNIPPET, flush=True)
    server = HTTPServer(('127.0.0.1', PORT), Sink)
    server.done = False
    while not server.done:
        server.handle_request()
