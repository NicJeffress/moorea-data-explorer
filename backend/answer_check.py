"""A last check on the analyst's final answer for a few known-wrong phrasings.

On a hit, GLEN appends a short correction to the answer instead of asking the
model again. Why append, not retry:
  * no second provider call, so no second hold, no extra spend against the
    tester allowance and no risk of the 80-second request limit;
  * the browser runs the tool loop and the ledger admits one call at a time, so
    a server-side retry would need its own reservation that can be refused;
  * a cheap model asked again often repeats the phrase, while the note is fixed,
    reviewed text that always lands;
  * the analyst's numbers stay as computed; the note only corrects the reading.
Only final answers are checked (a reply with tool calls is a step, not an answer).
The record for evals gets the rule names that fired, never the text.
"""
import re

NEGATION = re.compile(r"\b(not|never|no|cannot|can.t|isn.t|aren.t|unverified|without|until|unless|whether|if|before|once|pending)\b[^.\n]{0,30}$", re.I)

RULES = (
    ('slot_share',
     re.compile(r'(\d+(?:\.\d+)?\s*(?:[–-]\s*\d+(?:\.\d+)?\s*)?%[^.\n]{0,90}?\b(?:slots?|nominal|20[- ]?min(?:ute)?s?\b[^.\n]{0,20}\b(?:grid|interval|records?|rows|time\s*steps?)|possible\s+(?:records|samples|readings|rows)|expected\s+(?:records|samples|readings|rows)))'
                r'|((?:slots?|nominal\s+(?:20[- ]?min(?:ute)?\s+)?(?:grid|interval|records|rows|samples))\b[^.\n]{0,60}?\d+(?:\.\d+)?\s*%)', re.I),
     'The grid a table is stored on is not the sampling schedule, so record counts are not a share of '
     '20-minute (or other nominal) slots. Each instrument reports at its own interval; for the forereef '
     'waves, the ADCP reports hourly until 2008–2009 and every 3 hours from 2010, the SBE26 every 2 hours. '
     'Read those figures as record counts; describe_sampling gives the observed interval and the gaps.'),
    ('coverage_percent',
     re.compile(r'(\bcoverage\b[^.\n]{0,40}?\d+(?:\.\d+)?\s*%)|(\d+(?:\.\d+)?\s*%\s*(?:data\s+)?(?:coverage|complete(?:ness)?)\b)', re.I),
     'GLEN does not grade data by coverage percentages. What matters is when values exist: the first and '
     'last time, the months with values, and the gaps. Groups that cover different months are not '
     'like-for-like; compare the months they share.'),
)

# "Coverage" for when data exist (PR 84 eval: every model used it). Cover itself is fine:
# coral cover, percent cover, "couverture corallienne". Code (SQL, `ids`) is not checked.
COVERAGE_NOTE = ('GLEN does not describe data as coverage. Say when values exist: the first and last time, '
                 'the months with values and the gaps, or "no data". On every grid, grey is no data.')
COUVERTURE_NOTE = ("GLEN ne parle pas de couverture des données. Dites quand il y a des valeurs : premier et dernier "
                   "relevé, mois avec valeurs, lacunes, ou « pas de données ». Sur toutes les grilles, le gris signifie "
                   "« pas de données ».")
WORD_RULES = (
    ('coverage_word', re.compile(r'\bcoverage\b', re.I), COVERAGE_NOTE),
    ('couverture_word', re.compile(r'\bcouvertures?\b', re.I), COUVERTURE_NOTE),
)
# The word before "coverage" that makes it cover of something, or an EML place/taxon term.
COVER_KIND = re.compile(r'\b(?:coral|percent|percentage|algal|macroalgal|algae|benthic|live|substrate|cloud|canopy|'
                        r'seagrass|turf|vegetation|ground|geographic|taxonomic)[\s-]+$', re.I)
# French "couverture" is mostly coral cover ("la couverture corallienne", "une couverture nulle"). It means
# data availability in a bare label ("**Couverture 2019 :**", "- Couverture par site :") or in these phrases.
COUVERTURE_DATA = re.compile(r'couvertures?\s+(?:des\s+donn[ée]es|de\s+donn[ée]es|temporelle|d.[ée]chantillonnage)'
                             r'|taux\s+de\s+couverture(?!\s+(?:corall|algal|benthi|en\s+cora|de\s+cora))', re.I)
LABEL_START = re.compile(r'^\s*(?:[-*+]\s+|#+\s*|\d+[.)]\s+)?(?:\*\*|__)?\s*$')
COUVERTURE_COVER = re.compile(r'corail|coraux|corallien|algu|algal|benthi|substrat|quadrat|taxon|v[ée]g[ée]tal|nuageuse'
                              r'|vivante|nulle|%|pourcentage|percent', re.I)
WORD_NEGATION = re.compile(r'\b(?:not|never|rather than|instead of|pas|jamais|plutôt que)\b[^.\n]{0,20}$', re.I)
CODE = re.compile(r'```.*?```|`[^`\n]*`', re.S)


def _word_hit(name, text, m):
    before = text[max(0, m.start() - 60):m.start()]
    if WORD_NEGATION.search(before):  # "record counts, not coverage"; "no coverage" is still the word
        return False
    if name == 'coverage_word':
        return not COVER_KIND.search(before) and not re.match(r'\s+area', text[m.end():], re.I)
    if COUVERTURE_DATA.search(text[max(0, m.start() - 9):m.end() + 30]):
        return True
    line_start = text.rfind('\n', 0, m.start()) + 1
    label = re.match(r'[^:*\n]{0,60}', text[m.start():]).group(0)
    return bool(LABEL_START.match(text[line_start:m.start()])) and not COUVERTURE_COVER.search(label)


# A stated rule ("bins with fewer than 75% of their slots are grey") is not a claim.
THRESHOLD = re.compile(r"\b(fewer than|less than|at least|more than|minimum( of)?|threshold( of)?|below|above|under)\s*$", re.I)

HEADER = '\n\n---\n**GLEN check:** '


def findings(text):
    """Names of the rules the text breaks."""
    hits = []
    if not isinstance(text, str):
        return hits
    for name, pattern, _ in RULES:
        for m in pattern.finditer(text):
            before = text[max(0, m.start() - 60):m.start()]
            if NEGATION.search(before) or NEGATION.search(m.group(0)) or THRESHOLD.search(before):
                continue
            if name == 'coverage_percent' and m.group(1) and COVER_KIND.search(before):  # "coral coverage 80%"
                continue
            hits.append(name)
            break
    prose = CODE.sub(' ', text)
    for name, pattern, _ in WORD_RULES:
        if 'coverage_percent' in hits:  # that note already says it
            break
        if any(_word_hit(name, prose, m) for m in pattern.finditer(prose)):
            hits.append(name)
    return hits


def correction(names):
    notes = [note for name, _, note in RULES + WORD_RULES if name in names]
    return HEADER + ' '.join(notes) if notes else ''


def review(data):
    """Append corrections to a final chat-completions answer. Returns (data, hits)."""
    try:
        choice = data['choices'][0]
        message = choice['message']
    except (KeyError, IndexError, TypeError):
        return data, []
    if not isinstance(message, dict) or message.get('tool_calls') or choice.get('finish_reason') == 'tool_calls':
        return data, []
    content = message.get('content')
    if not isinstance(content, str) or HEADER in content:
        return data, []
    hits = findings(content)
    if not hits:
        return data, []
    message = dict(message, content=content + correction(hits))
    choices = [dict(choice, message=message)] + list(data['choices'][1:])
    return dict(data, choices=choices), hits
