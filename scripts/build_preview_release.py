"""Assemble a gated hybrid release; private datasets and credentials never enter web/."""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {'.html','.js','.css','.json','.geojson','.svg','.png','.jpg','.jpeg','.webp','.ico','.woff','.woff2','.md','.txt'}
SECRET = re.compile(rb'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}')


def checked_copy(source, target):
    if source.is_symlink() or source.suffix.lower() not in ALLOWED or any(p.startswith('.') for p in source.name.split('/')):
        raise ValueError(f'Unapproved web asset: {source.name}')
    if source.suffix.lower() in {'.html','.js','.json','.md','.txt'} and SECRET.search(source.read_bytes()):
        raise ValueError(f'Possible credential in web asset: {source.name}')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def tracked(directory, prefix):
    data = subprocess.check_output(['git','-C',str(directory),'ls-files','-z','--',prefix])
    return [directory / p.decode() for p in data.split(b'\0') if p]


def assemble(output, tile_release):
    if output.exists():
        raise ValueError('Use a new output directory; releases are never overwritten.')
    if not re.fullmatch(r'v[A-Za-z0-9._-]{1,63}', tile_release):
        raise ValueError('Invalid tile release name.')
    tiles = ROOT / 'site/static-data' / tile_release
    if not (tiles/'manifest.json').is_file():
        raise ValueError('Build static chart tiles first.')
    output.mkdir(parents=True)
    web = output/'web'
    for source in tracked(ROOT.parent, 'glen-explorer/site'):
        relative = source.relative_to(ROOT/'site')
        if relative.parts[0]=='static-data' or relative.suffix.lower() not in ALLOWED or (relative.suffix=='.html' and relative.as_posix() not in {'index.html','datasets.html'}):
            continue
        # The standalone local SQL/evaluation tools are not preview pages.
        if relative.name in {'checks.html','checks.js','model-eval.html','model-eval.js','config.json','layers-input.json'}:
            continue
        checked_copy(source, web/relative)
    # New files on the working branch must be staged before assembling a candidate.
    for needed in ['preview.js','preview.css']:
        if not (web/needed).is_file():
            raise ValueError(f'Stage {needed} before building.')
    upstream=ROOT/'upstream/geo-agent'
    for source in tracked(upstream,'app'):
        relative=source.relative_to(upstream/'app')
        if source.suffix.lower() in ALLOWED - {'.html'} and source.name not in {'config.json','layers-input.json'}:
            checked_copy(source,web/'glen'/relative)
    from backend.server import glen_bootstrap_source
    (web/'glen/main.js').write_text(glen_bootstrap_source())
    for source in tiles.rglob('*'):
        if source.is_file():
            checked_copy(source,web/'static-data'/tile_release/source.relative_to(tiles))
    (web/'static-data/current.json').write_text(json.dumps({'release':tile_release})+'\n')
    flags='<script>window.GLEN_STATIC=true;window.GLEN_PREVIEW=true;window.GLEN_STATIC_RELEASE='+json.dumps(tile_release)+';</script>'
    for page in ['index.html','datasets.html']:
        html=(web/page).read_text()
        html=html.replace('</head>',flags+'<link rel="stylesheet" href="/preview.css"></head>')
        html=html.replace('</body>','<script type="module" src="/preview.js"></script></body>')
        if page=='datasets.html':
            html=html.replace('Browsing and SQL previews require no model key.','Schemas and scientific review notes are available here; use the analyst for questions.')
        (web/page).write_text(html)
    def pack(path):
        data=path.read_bytes()
        if path.suffix in {'.json','.geojson','.js','.css','.html','.svg'}:
            path.with_name(path.name+'.gz').write_bytes(gzip.compress(data,compresslevel=6,mtime=0))
        return {'path':path.relative_to(web).as_posix(),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    paths=sorted(p for p in web.rglob('*') if p.is_file())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        entries=list(pool.map(pack,paths))
    manifest={'format':1,'access':'private-preview','public_release':False,'tile_release':tile_release,
              'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'working_tree_changes':subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).splitlines(),
              'mode':'hybrid: static chart tiles; authenticated backend for other views, imagery, MCP and analyst',
              'files':entries,'web_bytes':sum(p.stat().st_size for p in web.rglob('*') if p.is_file())}
    (output/'release-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    verify(output)
    return {'output':str(output),'files':len(entries),'bytes':manifest['web_bytes'],'tile_release':tile_release}


def verify(output):
    manifest=json.loads((output/'release-manifest.json').read_text());web=output/'web'
    expected=set()
    for item in manifest['files']:
        p=web/item['path']
        if not p.resolve().is_relative_to(web.resolve()) or p.is_symlink():raise ValueError('Unsafe release path.')
        raw=p.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError(f'Changed asset: {item["path"]}')
        expected.add(p)
        gz=p.with_name(p.name+'.gz')
        if p.suffix in {'.json','.geojson','.js','.css','.html','.svg'}:
            if gzip.decompress(gz.read_bytes())!=raw:raise ValueError('Invalid precompressed asset.')
            expected.add(gz)
    actual=set(p for p in web.rglob('*') if p.is_file())
    if actual!=expected or any(p.is_symlink() for p in web.rglob('*')):raise ValueError('Unlisted files or symlinks in release.')
    for p in actual:
        original=p.with_suffix('') if p.suffix=='.gz' else p
        if original.suffix.lower() not in ALLOWED or original.name.startswith('.'):
            raise ValueError('Forbidden asset in release.')
    return len(expected)


if __name__=='__main__':
    import sys
    sys.path.insert(0,str(ROOT))
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--release');p.add_argument('--verify',action='store_true');a=p.parse_args()
    if a.verify:print(json.dumps({'verified_files':verify(a.output.resolve())}))
    else:print(json.dumps(assemble(a.output.resolve(),a.release or json.loads((ROOT/'site/static-data/current.json').read_text())['release'])))
