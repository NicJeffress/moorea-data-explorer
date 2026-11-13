"""Package/restore the complete GLEN data snapshot using only Python's standard library."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
REPO = 'OWNER/PRIVATE-REPO'
TAG = 'data-snapshot-v1'
PART_SIZE = 1900 * 1024 * 1024
PATHS = ['mcr_data', 'glen-explorer/library', 'glen-explorer/data/imagery',
         'glen-explorer/data/explorer.duckdb', 'glen-explorer/data/published.duckdb',
         'glen-explorer/data/serving-catalog.json']


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class PartsWriter:
    def __init__(self, directory, limit):
        self.directory, self.limit = directory, limit
        self.parts, self.file, self.size = [], None, 0

    def write(self, data):
        total = len(data)
        while data:
            if self.file is None:
                name = f'data.tar.gz.part-{len(self.parts):03d}'
                self.file = (self.directory / name).open('wb')
                self.parts.append({'name': name})
                self.size = 0
                self.hash = hashlib.sha256()
            piece = data[:self.limit - self.size]
            self.file.write(piece)
            self.hash.update(piece)
            self.size += len(piece)
            data = data[len(piece):]
            if self.size == self.limit:
                self.close()
        return total

    def close(self):
        if self.file:
            self.file.close()
            self.parts[-1].update(size=self.size, sha256=self.hash.hexdigest())
            print('Packed', self.parts[-1]['name'], flush=True)
            self.file = None


class PartsReader(io.RawIOBase):
    def __init__(self, paths):
        self.paths, self.file = iter(paths), None

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.file is None:
                path = next(self.paths, None)
                if path is None:
                    return 0
                self.file = path.open('rb')
            n = self.file.readinto(buffer)
            if n:
                return n
            self.file.close()
            self.file = None

    def close(self):
        if self.file:
            self.file.close()
        super().close()


def package(root, destination, limit=PART_SIZE):
    destination.mkdir(parents=True, exist_ok=False)
    files = []
    for name in PATHS:
        p = root / name
        if not p.exists():
            raise RuntimeError(f'Missing snapshot input: {p}')
        files.extend(sorted(p.rglob('*')) if p.is_dir() else [p])
    files = [p for p in files if p.is_file() and p.name != '.DS_Store' and '__pycache__' not in p.parts]
    if any(p.is_symlink() for p in files):
        raise RuntimeError('Snapshot inputs must not contain symlinks')
    writer = PartsWriter(destination, limit)
    inventory = []
    with tarfile.open(fileobj=writer, mode='w|gz', compresslevel=1) as archive:
        for p in files:
            before = p.stat()
            archive.add(p, arcname=p.relative_to(root).as_posix(), recursive=False)
            after = p.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError(f'Input changed during packaging: {p}. Stop writers and retry.')
            inventory.append({'path': p.relative_to(root).as_posix(), 'size': before.st_size})
    writer.close()
    revision = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    manifest = {'version': 1, 'source_root': str(root), 'source_commit': revision.stdout.strip() or None, 'paths': PATHS,
                'unpacked_bytes': sum(f['size'] for f in inventory), 'files': inventory, 'parts': writer.parts}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Snapshot ready: {len(files)} files, {manifest["unpacked_bytes"]:,} unpacked bytes')


def download(url, path):
    temporary = path.with_name(path.name + '.partial')
    print('Downloading', path.name, flush=True)
    # Public releases work without authentication. Private releases can be downloaded
    # with gh release download and supplied using --bundle.
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RuntimeError('Release not found or repository is private. Sign in with gh auth login, or download the release assets and use --bundle. See README.md.') from error
        raise
    temporary.replace(path)


def relocate(root, old_root):
    for base in [root / 'glen-explorer/library', root / 'glen-explorer/data']:
        for path in base.rglob('*'):
            if path.suffix in {'.json', '.md'} and path.is_file():
                value = path.read_text()
                if old_root not in value:
                    continue
                if path.suffix == '.json':
                    def replace(item):
                        if isinstance(item, str):
                            return item.replace(old_root, str(root))
                        if isinstance(item, list):
                            return [replace(v) for v in item]
                        if isinstance(item, dict):
                            return {replace(k): replace(v) for k, v in item.items()}
                        return item
                    path.write_text(json.dumps(replace(json.loads(value)), indent=2) + '\n')
                else:
                    path.write_text(value.replace(old_root, str(root)))
    # The operator index contains persistent absolute-path SQL views. Recreate it
    # from the relocated registry, preserving IDs used by the app's adapters.
    index = root / 'glen-explorer/library/index.duckdb'
    if index.exists():
        index.unlink()
    validation = root / 'glen-explorer/library/validation.json'
    catalog = root / 'glen-explorer/data/serving-catalog.json'
    if validation.exists() and catalog.exists():
        value = json.loads(catalog.read_text())
        value['library_validation_sha256'] = digest(validation)
        catalog.write_text(json.dumps(value, indent=2) + '\n')


def restore(root, bundle, remote=False):
    bundle.mkdir(parents=True, exist_ok=True)
    base = f'https://github.com/{REPO}/releases/download/{TAG}'
    manifest_path = bundle / 'manifest.json'
    if remote and shutil.which('gh'):
        signed_in = subprocess.run(['gh', 'auth', 'status'], capture_output=True).returncode == 0
        if signed_in:
            subprocess.run(['gh', 'release', 'download', TAG, '--repo', REPO, '--dir', str(bundle), '--skip-existing'], check=True)
    if remote and not manifest_path.exists():
        download(base + '/manifest.json', manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if manifest['version'] != 1 or manifest['paths'] != PATHS:
        raise RuntimeError('Unsupported snapshot manifest')
    for name in PATHS:
        if (root / name).exists():
            raise RuntimeError(f'Refusing to overwrite existing data: {name}. Use a fresh clone.')
    paths = []
    for part in manifest['parts']:
        name = part['name']
        if Path(name).name != name or not name.startswith('data.tar.gz.part-'):
            raise RuntimeError('Invalid part name')
        path = bundle / name
        if remote and not path.exists():
            download(base + '/' + name, path)
        if path.stat().st_size != part['size'] or digest(path) != part['sha256']:
            raise RuntimeError(f'Checksum mismatch: {path}. Remove this part and retry.')
        paths.append(path)
    if shutil.disk_usage(root).free < manifest['unpacked_bytes'] + 1024**3:
        raise RuntimeError('Not enough free space to unpack the snapshot (plus 1 GiB reserve).')
    expected = {f['path']: f['size'] for f in manifest['files']}
    seen = set()
    with tempfile.TemporaryDirectory(prefix='.data-restore-', dir=root) as directory:
        staging = Path(directory)
        with io.BufferedReader(PartsReader(paths)) as stream, tarfile.open(fileobj=stream, mode='r|gz') as archive:
            for member in archive:
                name = member.name
                pure = PurePosixPath(name)
                allowed = any(name == p or name.startswith(p + '/') for p in PATHS)
                if (not member.isfile() or pure.is_absolute() or '..' in pure.parts or '\\' in name
                        or not allowed or name not in expected or name in seen or member.size != expected[name]):
                    raise RuntimeError(f'Unsafe or unexpected archive member: {name}')
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, target.open('wb') as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                seen.add(name)
        if seen != set(expected):
            raise RuntimeError('Snapshot is missing files')
        # Relocate to the final root, not the temporary extraction directory.
        for name in PATHS:
            source, target = staging / name, root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
    relocate(root, manifest['source_root'])
    (root / '.data-snapshot.json').write_text(json.dumps({'tag': TAG, 'manifest_sha256': digest(manifest_path)}))
    print('Complete data snapshot restored.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    pack = sub.add_parser('pack')
    pack.add_argument('destination', type=Path)
    get = sub.add_parser('restore')
    get.add_argument('--bundle', type=Path, help='Local folder containing manifest and all parts')
    args = parser.parse_args()
    if args.command == 'pack':
        package(ROOT, args.destination.resolve())
    else:
        restore(ROOT, args.bundle.resolve() if args.bundle else ROOT / '.data-download', remote=not args.bundle)


if __name__ == '__main__':
    main()
