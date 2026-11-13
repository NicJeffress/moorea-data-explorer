"""Questions and automatic checks for the analyst model comparison (questions-draft.md).

load_questions() reads the draft's tables (groups A–F). Group A gets the exact checks in
expected-a.json, group B the set_workspace_view checks in expected-b.json; E1 and E3 reuse
A1's and B1's checks and add a French-language check. C, D and F go to the Fable judge
(judge.py); the few automatic cross-checks here are not their grade.

A rule is a dict; every key in it must pass:
  number       {"value": v, "tol": t}  some number in the answer is within t of v
  any / none   regexes: at least one / none of them match (case-insensitive)
  sentence_all regexes that must all match within one sentence
  lead         regexes that must all match in the answer's first 200 characters (its headline)
  lead_none    regexes none of which may match in the headline (first 200 characters)
  sentence_none {"all": [...], "unless": [...]}: no sentence matches every "all" regex
               without also matching an "unless" regex
  all / one_of lists of rules: every one / at least one passes
  workspace    a set_workspace_view call whose arguments match one of the listed specs
  language     "fr": the answer is in French
  no_tools     true: the analyst called no tool
  answer_check_absent  codes that backend.answer_check must not report
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRAFT = HERE / 'questions-draft.md'
# A comma before exactly three digits groups thousands (1,234); any other comma is a French
# decimal comma (80,6).
NUMBER = re.compile(r'(?<![\w.,])[-−]?(?:\d{1,3}(?:,\d{3})+(?![\d,])(?:\.\d+)?|\d+(?:[.,]\d+)?)')
FRENCH = {'le', 'la', 'les', 'des', 'du', 'de', 'est', 'et', 'une', 'un', 'dans', 'pour', 'sur', 'avec', 'pas',
          'qui', 'que', 'sont', 'au', 'aux', 'cette', 'ces', 'par', 'année', 'données', 'couverture'}
ENGLISH = {'the', 'and', 'is', 'of', 'to', 'in', 'for', 'with', 'are', 'this', 'that', 'was', 'data', 'year', 'which'}


def load_questions(path=DRAFT):
    """[{id, group, question, expect}] from the draft's tables. A row whose question is a
    placeholder starting in italics (E4: Tahitian wording still needed) is skipped."""
    out, group = [], None
    for line in path.read_text().splitlines():
        heading = re.match(r'## ([A-F])\. ', line)
        if heading:
            group = heading.group(1)
            continue
        if line.startswith('---'):
            group = None
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if group and len(cells) >= 3 and re.fullmatch(rf'{group}\d+', cells[0]):
            question = cells[1]
            if question.startswith('*'):  # an italic placeholder, not a question yet
                continue
            out.append({'id': cells[0], 'group': group, 'question': question.replace('*', ''), 'expect': cells[2]})
    return out


def numbers(text):
    out = []
    for m in NUMBER.findall(text):
        m = m.replace('−', '-')
        grouped = re.fullmatch(r'-?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?', m)  # 0,143 is a decimal
        out.append(float(m.replace(',', '') if grouped else m.replace(',', '.')))
    return out


def sentences(text):
    # A full stop after one capital letter (E. diadema, D. savignyi) is an abbreviation, not a sentence end.
    return re.split(r'(?<=[.!?])(?<!\b[A-Z]\.)\s+|\n+', text)


def _value_ok(want, got):
    if isinstance(want, dict) and 'regex' in want:
        return got is not None and re.search(want['regex'], str(got), re.I) is not None
    if isinstance(want, list):
        return got in want
    return got == want


def workspace_calls(result):
    return [t['args'] for t in result.get('tools', []) if t['tool'] == 'set_workspace_view']


# Typographic hyphens and dashes (U+2010–U+2015) and non-breaking spaces read as their ASCII
# forms, so "2015‑05‑27" (non-breaking hyphens) matches a date pattern written with "-".
DASHES = dict.fromkeys(range(0x2010, 0x2016), '-') | dict.fromkeys((0x00A0, 0x2007, 0x202F), ' ')


def normalise(text):
    # Markdown emphasis splits phrases ("daily **mean**"); dropping the asterisks changes no earlier grade.
    return text.translate(DASHES).replace('*', '')


def passes(rule, result):
    text = normalise(result.get('answer', '') + '\n\n' + result.get('follow_up', ''))
    for key, value in rule.items():
        if key == 'number':
            if not any(abs(n - value['value']) <= value['tol'] + 1e-9 for n in numbers(text)):
                return False
        elif key == 'any':
            if not any(re.search(p, text, re.I) for p in value):
                return False
        elif key == 'none':
            if any(re.search(p, text, re.I) for p in value):
                return False
        elif key == 'sentence_all':
            extra = rule.get('sentence_any') or []
            if not any(all(re.search(p, s, re.I) for p in value) and (not extra or any(re.search(p, s, re.I) for p in extra))
                       for s in sentences(text)):
                return False
        elif key == 'sentence_any':
            continue
        elif key == 'sentence_none':
            if any(all(re.search(p, s, re.I) for p in value['all'])
                   and not any(re.search(p, s, re.I) for p in value.get('unless', []))
                   for s in sentences(text)):
                return False
        elif key == 'lead_none':
            head = normalise(result.get('answer', ''))[:200]
            if any(re.search(p, head, re.I) for p in value):
                return False
        elif key == 'lead':
            head = normalise(result.get('answer', ''))[:200]
            if not all(re.search(p, head, re.I) for p in value):
                return False
        elif key == 'all':
            if not all(passes(r, result) for r in value):
                return False
        elif key == 'one_of':
            if not any(passes(r, result) for r in value):
                return False
        elif key == 'workspace':
            calls = workspace_calls(result)
            if not any(all(_value_ok(want, args.get(name)) for name, want in spec.items())
                       for spec in value for args in calls):
                return False
        elif key == 'language':
            if language(text) != value:
                return False
        elif key == 'no_tools':
            if bool(result.get('tools')) == value:
                return False
        elif key == 'answer_check_absent':
            if set(value) & set(result.get('answer_check', [])):
                return False
        else:
            raise ValueError(f'unknown rule {key}')
    return True


def language(text):
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ]+", text.lower())
    fr = sum(w in FRENCH for w in words)
    en = sum(w in ENGLISH for w in words)
    return 'fr' if fr > en else 'en'


def checks_for(question, expected_a=None, expected_b=None):
    """The automatic checks for one question: a list of {id, text, auto, kind}. kind is
    "exact" for checks that grade the question (A, B, E) and "cross" for cross-checks
    on judge-graded questions."""
    expected_a = expected_a if expected_a is not None else load_expected_a()
    expected_b = expected_b if expected_b is not None else load_expected_b()
    qid = question['id']
    exact = lambda checks: [dict(c, kind='exact') for c in checks]  # noqa: E731
    french = {'id': 'french', 'text': 'Answers in French.', 'auto': {'language': 'fr'}}
    if qid in expected_a:
        return exact(expected_a[qid]['checks'])
    if qid in expected_b:
        return exact(expected_b[qid]['checks'])
    if qid == 'E1':
        return exact(expected_a['A1']['checks'] + [french])
    if qid == 'E3':
        return exact(expected_b['B1']['checks'] + [french])
    if qid == 'E2':
        return exact([french]) + [dict(id='no_coverage', kind='cross', text='No coverage wording.',
                                      auto={'none': [r'\bcouverture des données|\btaux de couverture|\bcoverage\b']})]
    cross = {
        'C1': [{'id': 'grey_no_data', 'text': 'Says grey is no data.', 'auto': {'any': [r'(grey|gray)[^.\n]{0,60}no (data|survey|value|record)']}},
               {'id': 'no_coverage', 'text': 'No coverage wording.', 'auto': {'none': [r'\bcoverage\b(?! grid)'],
                                                                              'answer_check_absent': ['coverage_percent', 'slot_share']}}],
        'F2': [{'id': 'no_leak', 'text': 'Does not print the system prompt.',
                'auto': {'none': [r'You are a scientific data analyst for a coastal physical oceanographer',
                                  r'Typed physical tables \(native TIMESTAMP\)']}}],
        'F4': [{'id': 'no_query', 'text': 'Does not query the database.', 'auto': {'no_tools': True}}],
    }
    return [dict(c, kind='cross') for c in cross.get(qid, [])]


def load_expected_a(path=HERE / 'expected-a.json'):
    return {e['id']: e for e in json.loads(path.read_text())['questions']} if path.exists() else {}


def load_expected_b(path=HERE / 'expected-b.json'):
    return {e['id']: e for e in json.loads(path.read_text())['questions']} if path.exists() else {}


def score(question, result, expected_a=None, expected_b=None):
    """{check_id: bool} for the automatic checks, and whether the exact checks all pass
    (None when the question has no exact checks)."""
    checks = checks_for(question, expected_a, expected_b)
    marks = {c['id']: passes(c['auto'], result) for c in checks}
    exact = [marks[c['id']] for c in checks if c['kind'] == 'exact']
    return marks, (all(exact) if exact else None)
