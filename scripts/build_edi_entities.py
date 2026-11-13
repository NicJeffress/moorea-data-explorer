#!/usr/bin/env python3
"""Pin the EDI facts that the "Get this data from EDI" scripts need (plan step U19).

For each EDI file a script downloads, this reads the package's EML and records the download URL, the
checksum, the delimiter and header lines, and for each column its EML name, type and missing-value codes.
The citation and the rights text come from the same EML. The result, backend/edi_entities.json, is
committed, so the server can write scripts without the downloaded packages.

    .venv/bin/python scripts/build_edi_entities.py          # rewrite backend/edi_entities.json
    .venv/bin/python scripts/build_edi_entities.py --check  # fail if it no longer matches the EML
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'backend' / 'edi_entities.json'
MCR = ROOT.parent / 'mcr_data'
# Table name used in the scripts → (package, EML objectName). Add a line here to make a new file available.
WANTED = {
    'annual_fish_survey': ('knb-lter-mcr.6.65', 'MCR_LTER_Annual_Fish_Survey_20260304.csv'),
}


def identifier(s):
    # Same rule as scripts/prepare_library.py, so the scripts use the explorer's column names.
    s = re.sub('[^a-z0-9]+', '_', str(s).lower()).strip('_') or 'field'
    return 'c_' + s if s[0].isdigit() else s


def text(el):
    return ' '.join(' '.join(el.itertext()).split()) if el is not None else ''


def column_type(a):
    scale = a.find('measurementScale')
    scale = scale[0].tag if scale is not None and len(scale) else None
    if scale in ('ratio', 'interval'):
        return 'BIGINT' if a.findtext('.//numberType') in ('integer', 'natural', 'whole') else 'DOUBLE'
    return 'VARCHAR'


def citation(dataset, package):
    names = []
    for c in dataset.findall('creator'):
        person = c.find('individualName')
        if person is not None:
            names.append(' '.join(x for x in [*(g.text or '' for g in person.findall('givenName')), person.findtext('surName') or ''] if x))
        elif c.findtext('organizationName'):
            names.append(c.findtext('organizationName'))
    doi = next((t for t in (text(x) for x in dataset.findall('alternateIdentifier')) if 'doi' in t), '')
    year = (dataset.findtext('pubDate') or '')[:4]
    revision = package.rsplit('.', 1)[1]
    link = 'https://doi.org/' + doi.removeprefix('doi:') if doi else ''
    return f"{', '.join(names)}. {year}. {text(dataset.find('title'))} ver {revision}. Environmental Data Initiative. {link}".strip()


def entity(package, object_name):
    eml = MCR / package / f'{package}.xml'
    dataset = ET.parse(eml).getroot().find('dataset')
    for dt in dataset.findall('dataTable'):
        if dt.findtext('physical/objectName') != object_name:
            continue
        auth = dt.find('physical/authentication')
        if auth is None or auth.get('method') != 'MD5':
            raise ValueError(f'{package}/{object_name}: the EML gives no MD5 checksum.')
        urls = [u.text for u in dt.findall('physical/distribution/online/url') if u.get('function', 'download') == 'download']
        if len(urls) != 1 or not urls[0].startswith('https://pasta.lternet.edu/package/data/eml/'):
            raise ValueError(f'{package}/{object_name}: expected one EDI download URL, found {urls}.')
        return {
            'package': package,
            'entity_name': dt.findtext('entityName'),
            'object_name': object_name,
            'url': urls[0],
            'md5': auth.text.strip(),
            'size_bytes': int(dt.findtext('physical/size') or 0),
            'rows': int(dt.findtext('numberOfRecords') or 0),
            'header_lines': int(dt.findtext('physical/dataFormat/textFormat/numHeaderLines') or '1'),
            'delimiter': dt.findtext('.//simpleDelimited/fieldDelimiter') or ',',
            'landing_page': f'https://portal.edirepository.org/nis/mapbrowse?packageid={package}',
            'citation': citation(dataset, package),
            'rights': text(dataset.find('intellectualRights')),
            'columns': [{'source': a.findtext('attributeName'), 'name': identifier(a.findtext('attributeName')),
                         'type': column_type(a), 'missing_codes': [text(c) for c in a.findall('missingValueCode/code')]}
                        for a in dt.findall('attributeList/attribute')],
        }
    raise ValueError(f'{package}: no dataTable with objectName {object_name}.')


def whole_files(package):
    """Every zip archive (otherEntity) of a package: EDI URL, size and MD5, for pages that link to the file on EDI."""
    dataset = ET.parse(MCR / package / f'{package}.xml').getroot().find('dataset')
    out = {}
    for e in dataset.findall('otherEntity'):
        name = e.findtext('physical/objectName') or ''
        if not name.endswith('.zip'):
            continue
        auth = e.find('physical/authentication')
        size = e.find('physical/size')
        scale = {'byte': 1, 'bytes': 1, 'kb': 1e3, 'mb': 1e6, 'gb': 1e9}[(size.get('unit') or 'byte').lower()] if size is not None else 0
        out[name] = {'url': e.findtext('physical/distribution/online/url'), 'size_bytes': int(float(size.text) * scale) if scale else 0,
                     'md5': auth.text.strip() if auth is not None and auth.get('method') == 'MD5' else None}
    return out


# Packages whose archives a page points to on EDI instead of serving their contents (Coral photos' originals).
ARCHIVES = ['knb-lter-mcr.5006.3', 'knb-lter-mcr.5013.3']


def build():
    return {'tables': {table: entity(*where) for table, where in WANTED.items()},
            'archives': {package: whole_files(package) for package in ARCHIVES}}


if __name__ == '__main__':
    built = json.dumps(build(), indent=1, ensure_ascii=False) + '\n'
    if '--check' in sys.argv:
        if OUT.read_text() != built:
            sys.exit(f'{OUT.relative_to(ROOT)} no longer matches the EML; rerun scripts/build_edi_entities.py.')
        print('edi_entities.json matches the EML.')
    else:
        OUT.write_text(built)
        print(f'Wrote {OUT.relative_to(ROOT)} ({len(WANTED)} tables, {len(ARCHIVES)} packages of archives).')
