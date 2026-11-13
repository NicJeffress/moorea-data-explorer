"""`--tools live` for the question and sampling runners: replay what the browser analyst sends.

live-request.json (written by capture_live_request.py) holds the `tools` array and system
message of a real /api/llm request from the page: GLEN's MCP tools, the geo-agent map tools,
render_chart and the workspace tools, and the system prompt with the map catalog text and the
MCP geospatial-analyst prompt. The runners send those exactly. MCP tools run against this
checkout's server; every other tool runs in the browser, so here it is a stub that returns a
short plausible success, and its rounds do not count toward the checkpoint (as in agent.js).
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FIXTURE = HERE / 'live-request.json'

# A short answer of the shape each geo-agent map tool returns on success.
MAP_STATE = {'center': [-149.842, -17.535], 'zoom': 10.8, 'projection': 'mercator', 'basemap': 'plain',
             'visible_layers': ['moorea-sites/monitoring-sites'], 'filters': {}}


def map_stub(name, args):
    layer = args.get('layer_id')
    if name == 'render_chart':  # the same stub as the default tool list
        return {'success': True, 'note': 'evaluation stub'}
    if name == 'get_map_state':
        return {'success': True, **MAP_STATE}
    if name == 'geocode':
        return {'success': True, 'results': [{'name': f'{args.get("query", "")} (evaluation stub)', 'lat': -17.535,
                                              'lon': -149.842, 'bbox': [-149.93, -17.61, -149.75, -17.47]}]}
    if name == 'fly_to':
        return {'success': True, 'center': args.get('center', MAP_STATE['center']), 'zoom': args.get('zoom', 12)}
    if name in ('show_layer', 'hide_layer'):
        return {'success': True, 'layer_id': layer, 'visible': name == 'show_layer'}
    if name == 'add_hex_tile_layer':
        return {'success': True, 'layer_id': 'hex-tile-1'}
    if name == 'filter_by_query':
        return {'success': True, 'layer_id': layer, 'matched_features': 3}
    if name == 'set_projection':
        return {'success': True, 'projection': args.get('type')}
    return {'success': True, **({'layer_id': layer} if layer else {})}


def load(path=FIXTURE):
    return json.loads(Path(path).read_text())


async def setup(server, path=FIXTURE):
    """(tools, system prompt, browser-side tool names, staleness notes) from the fixture."""
    fixture = load(path)
    remote = {t.name for t in await server.mcp.list_tools()}
    names = [t['function']['name'] for t in fixture['tools']]
    stale = []
    if remote - set(names):
        stale.append(f'MCP tools added since the capture: {sorted(remote - set(names))}')
    base = (ROOT / 'site/system-prompt.md').read_text()
    if not fixture['system'][0].startswith(base):
        stale.append('site/system-prompt.md changed since the capture')
    if server.analyst_prompt() not in fixture['system'][-1]:
        stale.append('the MCP geospatial-analyst prompt changed since the capture (or the capture missed it)')
    return fixture['tools'], '\n\n'.join(fixture['system']), set(names) - remote, stale


def stub_call(workspace_call):
    """A tool caller: map tools and render_chart answer from map_stub, the rest go to workspace_call."""
    async def call(server, name, args):
        if name in MAP_TOOLS:
            return json.dumps(map_stub(name, args))
        return await workspace_call(server, name, args)
    return call


MAP_TOOLS = {'show_layer', 'hide_layer', 'set_filter', 'clear_filter', 'reset_filter', 'create_slider', 'set_tooltip',
             'reset_tooltip', 'set_legend', 'reset_legend', 'set_style', 'reset_style', 'get_map_state', 'fly_to',
             'geocode', 'get_user_location', 'add_hex_tile_layer', 'remove_hex_tile_layer', 'filter_by_query',
             'set_projection', 'get_drawn_region', 'get_uploaded_dataset', 'render_chart'}
