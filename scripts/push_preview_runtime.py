"""Copy an assembled preview release and the private runtime files it needs to the preview server.

    .venv/bin/python scripts/push_preview_runtime.py --release /abs/release --dest root@DROPLET_IP --dry-run
    .venv/bin/python scripts/push_preview_runtime.py --release /abs/release --dest root@DROPLET_IP --link-checkout /srv/glen/runtime

The release (scripts/build_preview_release.py) goes to <remote-root>/releases/<id>/. The private files listed in
docs/preview-runtime-manifest.json (scripts/preview_runtime_manifest.py) go to <remote-root>/private/, at the same
repository-relative paths, never under a release. With --link-checkout, the code checkout on the server gets one
symlink per manifest link point (e.g. <checkout>/mcr_data -> <remote-root>/private/mcr_data), because the backend
reads its data relative to its own code.

Before copying, the script checks every local file against the manifest's size and SHA-256 and the release against
its own manifest, refuses credentials (.env, keys, tokens) and the preview state (usage.sqlite3, STOP_ANALYST),
and checks free disk space on the server. After copying, it hashes every file again on the server. It never writes
to the state directory (GLEN_PREVIEW_STATE, default /var/lib/glen/state, the only folder the
hardened service unit can write) and never deletes anything.

--dest local runs the same steps on this machine, for tests and rehearsals. Nothing is provisioned or deployed
by this script beyond the copy: starting the service is a separate operator step.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
DEFAULT_MANIFEST = APP / 'docs/preview-runtime-manifest.json'
DEFAULT_STATE = '/var/lib/glen/state'  # GLEN_PREVIEW_STATE in the server kit's systemd unit
SAFE_PATH = re.compile(r'^/[A-Za-z0-9._/-]+$')
# Manifest paths are sent through rsync --files-from (never a shell), so spaces are fine; control characters,
# backslashes and leading dashes are not.
MANIFEST_PATH = re.compile(r'^[^\x00-\x1f\x7f\\-][^\x00-\x1f\x7f\\]*$')
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
SECRET = re.compile(rb'\bsk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{24,}|-----BEGIN [A-Z ]*PRIVATE KEY-----')
SCANNED = {'.json', '.csv', '.txt', '.xml', '.md', '.tsv', '.js', '.html', '.py', '.yml', '.yaml', '.toml', '.cfg', '.ini', ''}
SCAN_LIMIT = 16 * 1024 * 1024
# rsync filters are a second line of defence; the checks below refuse these names before anything is sent.
EXCLUDES = ['.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', '*.token', 'id_rsa*', 'id_ed25519*',
            'usage.sqlite3', 'usage.sqlite3-*', 'STOP_ANALYST', '.git']
CHMOD = 'Du=rwx,Dg=rx,Do=,Fu=rw,Fg=r,Fo='


class Refused(Exception):
    pass


def forbidden(name):
    """Names that hold credentials or the preview's own state; never copied, whatever a manifest says."""
    base = PurePosixPath(name).name
    lower = base.lower()
    return (base == '.env' or base.startswith('.env.') or lower.endswith(('.pem', '.key', '.p12', '.pfx', '.token'))
            or lower.startswith(('id_rsa', 'id_ed25519')) or 'token' in lower and lower.endswith(('.json', '.txt'))
            or base.startswith('usage.sqlite3') or base == 'STOP_ANALYST' or base in {'.netrc', 'credentials'})


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def scan(path, label):
    """Refuse a small text file that looks like it holds a provider key or a private key."""
    if path.suffix.lower() in SCANNED and path.stat().st_size <= SCAN_LIMIT and SECRET.search(path.read_bytes()):
        raise Refused(f'Possible credential in {label}; refusing to copy it.')


# ---------------------------------------------------------------- local checks

def check_manifest(manifest_path, source_root):
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get('format') != 1 or not isinstance(manifest.get('files'), list):
        raise Refused('Not a preview runtime manifest (format 1).')
    entries, seen = [], set()
    for entry in manifest['files']:
        rel = entry.get('path', '')
        pure = PurePosixPath(rel)
        if not rel or pure.is_absolute() or '..' in pure.parts or '\\' in rel or rel in seen or not MANIFEST_PATH.match(rel):
            raise Refused(f'Unsafe manifest path: {rel!r}')
        if any(forbidden(part) for part in pure.parts):
            raise Refused(f'Manifest lists a credential or state file: {rel}')
        seen.add(rel)
        path = source_root / rel
        if not path.is_file():
            raise Refused(f'Missing local file: {rel}. Restore it or rebuild the manifest.')
        if path.stat().st_size != entry['bytes']:
            raise Refused(f'Size changed since the manifest was built: {rel}. Rebuild the manifest (preview_runtime_manifest.py trace).')
        scan(path, rel)
        entries.append(entry)
    points = manifest.get('link_points', [])
    for point in points:
        pure = PurePosixPath(point)
        if pure.is_absolute() or '..' in pure.parts or not MANIFEST_PATH.match(point) or forbidden(point):
            raise Refused(f'Unsafe link point: {point!r}')
        if not any(e['path'] == point or e['path'].startswith(point + '/') for e in entries):
            raise Refused(f'Link point {point} covers no manifest file.')
    if any(not any(e['path'] == p or e['path'].startswith(p + '/') for p in points) for e in entries):
        raise Refused('A manifest file lies under no link point.')
    return manifest, entries


def hash_entries(entries, source_root):
    for entry in entries:
        if sha256(source_root / entry['path']) != entry['sha256']:
            raise Refused(f"Content changed since the manifest was built: {entry['path']}. Rebuild the manifest.")


def check_release(release):
    sys.path.insert(0, str(APP))
    from scripts.build_preview_release import verify
    if not (release / 'release-manifest.json').is_file():
        raise Refused('Not an assembled release: release-manifest.json is missing.')
    verify(release)  # every web file listed and unchanged, no extras, no symlinks
    files = []
    for path in sorted(release.rglob('*')):
        rel = path.relative_to(release).as_posix()
        if path.is_symlink():
            raise Refused(f'Symlink in release: {rel}')
        if path.is_dir():
            continue
        if any(forbidden(part) for part in PurePosixPath(rel).parts):
            raise Refused(f'Credential or state file in release: {rel}')
        if not (rel == 'release-manifest.json' or rel.startswith('web/')):
            raise Refused(f'Unexpected file in release: {rel}')
        files.append(rel)
    return files


# ---------------------------------------------------------------- server side

# Runs on the server with its own python3 and the standard library only. Takes one JSON argument.
HELPER = r'''
import hashlib, json, os, sys
args = json.loads(ARGS)
state = os.path.realpath(args['state'])

def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)

def guard(path):
    if inside(path, state) or inside(state, path):
        raise SystemExit('Refusing to touch the preview state directory: ' + path)

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def existing_ancestor(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return path

op, out = args['op'], {}
for p in args.get('paths', []):
    guard(p)
if op == 'inspect':
    probe = existing_ancestor(args['root'])
    s = os.statvfs(probe)
    out['free_bytes'] = s.f_bavail * s.f_frsize
    out['probe'] = probe
    release = args['release_dir']
    m = os.path.join(release, 'release-manifest.json')
    out['release_exists'] = os.path.exists(release)
    out['release_manifest_sha256'] = digest(m) if os.path.isfile(m) else None
    sizes = {}
    for rel in args['private_files']:
        p = os.path.join(args['private_dir'], rel)
        if os.path.isfile(p) and not os.path.islink(p):
            sizes[rel] = os.path.getsize(p)
    out['private_sizes'] = sizes
elif op == 'prepare':
    for d in (os.path.dirname(args['release_dir']), args['private_dir']):
        os.makedirs(d, mode=0o750, exist_ok=True)
elif op == 'verify':
    # Owner read/write, group read, nothing for others; set here because some rsync builds ignore --chmod.
    for base in (args['release_dir'], args['private_dir']):
        os.chmod(base, 0o750)
        for d, dirs, names in os.walk(base):
            for n in dirs:
                if not os.path.islink(os.path.join(d, n)):
                    os.chmod(os.path.join(d, n), 0o750)
            for n in names:
                if not os.path.islink(os.path.join(d, n)):
                    os.chmod(os.path.join(d, n), 0o640)
    bad = []
    for base, expected in ((args['release_dir'], args['release_files']), (args['private_dir'], args['private_files'])):
        for rel, want in expected.items():
            p = os.path.join(base, rel)
            if os.path.islink(p) or not os.path.isfile(p):
                bad.append('missing or not a regular file: ' + p)
            elif digest(p) != want:
                bad.append('hash differs: ' + p)
    for base in (args['release_dir'], args['private_dir']):
        for d, dirs, names in os.walk(base):
            for n in dirs + names:
                if os.path.islink(os.path.join(d, n)):
                    bad.append('symlink: ' + os.path.join(d, n))
    out['problems'] = bad
    out['checked'] = sum(len(e) for e in (args['release_files'], args['private_files']))
elif op == 'link':
    made, kept = [], []
    for point in args['points']:
        link, target = os.path.join(args['checkout'], point), os.path.join(args['private_dir'], point)
        if os.path.islink(link):
            if os.readlink(link) != target:
                raise SystemExit('Existing link points elsewhere; fix it by hand: ' + link + ' -> ' + os.readlink(link))
            kept.append(point)
        elif os.path.exists(link):
            raise SystemExit('A real file or folder is in the way; move it aside by hand: ' + link)
        else:
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(target, link)
            made.append(point)
    out['made'], out['kept'] = made, kept
print(json.dumps(out))
'''


class Remote:
    """The server, reached over ssh, or this machine for --dest local."""

    def __init__(self, dest, ssh_options):
        self.local = dest == 'local'
        if not self.local and not re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9.-]+|[A-Za-z0-9.-]+', dest):
            raise Refused('--dest must be user@host, a host alias, or "local".')
        self.dest, self.ssh = dest, ['ssh', *ssh_options]

    def helper(self, **args):
        script = 'ARGS = ' + repr(json.dumps(args)) + '\n' + HELPER
        command = [sys.executable, '-'] if self.local else [*self.ssh, self.dest, 'python3', '-']
        done = subprocess.run(command, input=script, capture_output=True, text=True)
        if done.returncode:
            raise Refused(f"Server step {args['op']} failed: {(done.stderr or done.stdout).strip()[-800:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])

    def target(self, path):
        return path if self.local else f'{self.dest}:{path}'

    def rsync(self, source, target, file_list, follow_links):
        with tempfile.NamedTemporaryFile('w', suffix='.list', delete=False) as f:
            f.write('\n'.join(file_list) + '\n')
            listing = f.name
        try:
            command = ['rsync', '-rtp', '--chmod=' + CHMOD, '--files-from=' + listing, '--stats']
            command += ['-L'] if follow_links else ['--links']
            command += ['--exclude=' + x for x in EXCLUDES]
            if not self.local:
                command += ['-e', shlex.join(self.ssh)]
            command += [str(source) + '/', self.target(target) + '/']
            subprocess.run(command, check=True)
        finally:
            os.unlink(listing)


# ---------------------------------------------------------------- plan and push

def plan(a):
    release = a.release.resolve()
    release_id = a.release_id or release.name
    if not SAFE_ID.match(release_id):
        raise Refused(f'Unsafe release id: {release_id!r}')
    root = os.path.normpath(a.remote_root)
    state = os.path.normpath(a.state_dir or DEFAULT_STATE)
    release_dir, private_dir = f'{root}/releases/{release_id}', f'{root}/private'
    for p in [root, state, a.link_checkout] if a.link_checkout else [root, state]:
        if not SAFE_PATH.match(p) or '..' in p.split('/'):
            raise Refused(f'Server paths must be absolute and plain: {p!r}')
    for p in (release_dir, private_dir) + ((a.link_checkout,) if a.link_checkout else ()):
        if p == state or p.startswith(state + '/') or state.startswith(p + '/'):
            raise Refused(f'{p} overlaps the preview state directory {state}; refusing.')
    if a.link_checkout and (a.link_checkout.startswith(private_dir + '/') or a.link_checkout.startswith(f'{root}/releases/')
                            or a.link_checkout in (private_dir, root)):
        raise Refused('The code checkout must not be inside the private or release folders.')
    return release, release_id, root, state, release_dir, private_dir


def push(a):
    release, release_id, root, state, release_dir, private_dir = plan(a)
    source_root = a.source_root.resolve()
    print(f'Checking release {release} ...', flush=True)
    release_files = check_release(release)
    release_bytes = sum((release / f).stat().st_size for f in release_files)
    print(f'Checking {a.manifest} against {source_root} ...', flush=True)
    manifest, entries = check_manifest(a.manifest, source_root)
    hash_entries(entries, source_root)
    remote = Remote(a.dest, a.ssh_option)
    private_files = [e['path'] for e in entries]
    seen = remote.helper(op='inspect', root=root, state=state, release_dir=release_dir, private_dir=private_dir,
                         private_files=private_files, paths=[release_dir, private_dir])
    local_release_manifest = sha256(release / 'release-manifest.json')
    if seen['release_exists'] and seen['release_manifest_sha256'] is None:
        raise Refused(f'{release_dir} exists without release-manifest.json, probably an interrupted first copy. '
                      'Inspect it and remove it by hand, then push again.')
    if seen['release_exists'] and seen['release_manifest_sha256'] != local_release_manifest:
        raise Refused(f'{release_dir} already holds a different release; releases are never overwritten. Use a new --release-id.')
    sizes = {e['path']: e['bytes'] for e in entries}
    private_needed = sum(b for p, b in sizes.items() if seen['private_sizes'].get(p) != b)
    needed = (0 if seen['release_exists'] else release_bytes) + private_needed
    reserve = int(a.reserve_gib * 1024 ** 3)
    report = {'dest': a.dest, 'release_id': release_id, 'release_dir': release_dir, 'private_dir': private_dir,
              'state_dir_untouched': state, 'release_files': len(release_files), 'release_bytes': release_bytes,
              'private_files': len(entries), 'private_bytes': sum(sizes.values()), 'bytes_to_send': needed,
              'free_bytes': seen['free_bytes'], 'reserve_bytes': reserve, 'link_checkout': a.link_checkout,
              'link_points': manifest['link_points'], 'dry_run': a.dry_run}
    if needed + reserve > seen['free_bytes']:
        print(json.dumps(report, indent=1))
        raise Refused(f"Not enough free disk on the server: need {needed + reserve:,} bytes including the "
                      f"{a.reserve_gib} GiB reserve, {seen['free_bytes']:,} free at {seen['probe']}.")
    if a.dry_run:
        print(json.dumps(report, indent=1))
        print('Dry run: all local checks passed and the server has room; nothing was copied or linked.')
        return report
    remote.helper(op='prepare', root=root, state=state, release_dir=release_dir, private_dir=private_dir,
                  paths=[release_dir, private_dir])
    print(f'Copying release to {remote.target(release_dir)} ...', flush=True)
    remote.rsync(release, release_dir, release_files, follow_links=False)
    print(f'Copying {len(entries)} private files to {remote.target(private_dir)} ...', flush=True)
    remote.rsync(source_root, private_dir, private_files, follow_links=True)
    print('Verifying hashes on the server ...', flush=True)
    expected_release = {f: sha256(release / f) for f in release_files}
    checked = remote.helper(op='verify', state=state, release_dir=release_dir, private_dir=private_dir,
                            release_files=expected_release, private_files={e['path']: e['sha256'] for e in entries},
                            paths=[release_dir, private_dir])
    if checked['problems']:
        raise Refused('Server copy does not match: ' + '; '.join(checked['problems'][:10]))
    report['verified_files'] = checked['checked']
    if a.link_checkout:
        links = remote.helper(op='link', state=state, checkout=a.link_checkout, private_dir=private_dir,
                              points=manifest['link_points'], paths=[a.link_checkout])
        report['links_made'], report['links_kept'] = links['made'], links['kept']
    print(json.dumps(report, indent=1))
    print(f'Done. Set GLEN_PREVIEW_RELEASE={release_dir} for the service; restarting it is a separate step.')
    return report


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--release', type=Path, required=True, help='Assembled release folder from build_preview_release.py.')
    p.add_argument('--dest', required=True, help='SSH destination such as root@DROPLET_IP (the server kit admin login), or "local" for this machine.')
    p.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    p.add_argument('--source-root', type=Path, default=REPO, help='Checkout the manifest paths are relative to.')
    p.add_argument('--remote-root', default='/srv/glen')
    p.add_argument('--state-dir', help='GLEN_PREVIEW_STATE on the server; never written. Default ' + DEFAULT_STATE + '.')
    p.add_argument('--release-id', help='Folder name under releases/. Default: the release folder name.')
    p.add_argument('--link-checkout', help='Server code checkout to link the private files into.')
    p.add_argument('--reserve-gib', type=float, default=4.0, help='Free space to leave on the server (default 4 GiB).')
    p.add_argument('--ssh-option', action='append', default=[], help='Extra ssh argument, repeatable (e.g. -i KEY).')
    p.add_argument('--dry-run', action='store_true', help='Run every check, copy and link nothing.')
    return p


def main(argv=None):
    try:
        push(parser().parse_args(argv))
    except (Refused, ValueError) as exc:
        raise SystemExit(f'Refused: {exc}')


if __name__ == '__main__':
    main()
