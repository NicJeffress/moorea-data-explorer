"""The analyst's model allowlist: provider, model ID, endpoint, efforts, limits, prices.

Only models listed here can be served by the analyst, and the server, never the
browser, picks the contestant: a (model, effort) pair, ``model@effort``
(``GLEN_ANALYST_MODEL`` and ``GLEN_ANALYST_EFFORT``). Every hold, charge and
per-request record is priced from these entries, by the model the provider
reports it served. Prices are US dollars per million tokens, which is the same
number as microdollars per token. Update a price only from the provider's
official pricing page, and record where and when in ``checked``. A model whose
price is not confirmed (``price=None``) cannot be selected or priced.
"""
from dataclasses import dataclass, field, replace
from fractions import Fraction
import math
import os
import re

# One request body is at most 256,000 bytes (server.LocalBoundary, in local and
# preview mode alike). A text token covers at least one byte, so a request cannot
# reach this many input tokens even after message and tool framing. It is also
# GPT-5.4 mini's whole context window, so its hold is unchanged.
MAX_INPUT = 400000
# Output cap for a reasoning effort above 'none' on a model whose default cap
# is sized for no reasoning (GPT-5.4 mini's 2048): reasoning tokens count too.
REASONING_MAX_OUTPUT = 16000
CLAUDE_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens (= microdollars per token)."""
    input: str
    output: str
    cache_write_5m: str | None = None  # None: no separate write price (charged as input)
    cache_write_1h: str | None = None
    cache_read: str | None = None      # OpenAI "cached input"; Anthropic cache read

    def rate(self, name):
        value = getattr(self, name)
        return Fraction(value if value is not None else self.input)

    def worst_input(self):
        return max(self.rate(n) for n in ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read'))

    def openai_input(self):
        """OpenAI usage does not say which input tokens were cache writes, so every
        input token is charged at the dearer of the input and cache-write rates."""
        return max(self.rate('input'), self.rate('cache_write_5m'))


@dataclass(frozen=True)
class Model:
    id: str
    provider: str            # 'openai' or 'anthropic'
    role: str                # 'analyst': may be served; 'judge': evals only; 'fallback': pricing only
    price: Price | None      # None: unconfirmed, so the model is refused
    max_output: int
    effort: str | None       # the effort sent when none is configured
    efforts: tuple           # every effort this model documents; nothing else is sent
    checked: str             # where and when the prices were confirmed
    api: str = 'messages'    # 'chat' (Chat Completions), 'responses' (Responses API) or 'messages'
    pinned: bool = True      # False: an alias; dated snapshots of it are priced the same
    max_input: int = MAX_INPUT
    long_price: Price | None = None   # a second rate card for long prompts
    long_above: int | None = None     # prompt tokens above which long_price applies
    fallbacks: tuple = field(default=())  # server-side refusal fallbacks, in order

    @property
    def contestant(self):
        return f'{self.id}@{self.effort}'

    def cards(self):
        return [self.price] + ([self.long_price] if self.long_price else [])

    def worst_price(self):
        return (max(c.worst_input() for c in self.cards()), max(Fraction(c.output) for c in self.cards()))

    def serves(self, served):
        """True when `served` (the reply's model) is this model."""
        if served == self.id:
            return True
        return not self.pinned and re.fullmatch(re.escape(self.id) + r'-\d{4}-\d{2}-\d{2}', served or '') is not None


SKILL_PRICES = 'Anthropic first-party rates in the claude-api skill model table (cached 2026-11-03); confirm on the official pricing page before a paid run'
OPENAI_PRICES = 'developers.openai.com/api/docs/pricing and the model page, standard tier, read 2026-11-09'
OPENAI_NEW = ('none', 'low', 'medium', 'high', 'xhigh', 'max')

MODELS = {m.id: m for m in [
    # The default. Chat Completions, as before PR 50's review; cached input is
    # charged at the uncached rate (PR 50), so cache_read is for the record only.
    Model('gpt-5.4-mini-2026-03-17', 'openai', 'analyst', Price('0.75', '4.50', cache_read='0.075'),
          2048, 'none', ('none', 'low', 'medium', 'high', 'xhigh'),
          'OpenAI model page, verified 2026-11-06 (docs/hosting.md); efforts read 2026-11-09', api='chat'),

    # The comparison models Nic chose (newest of each line). They have no dated
    # snapshot IDs, so each is an unpinned alias. Tool calling needs the Responses
    # API on Sol and Terra, and on Luna above effort 'none', so all four use it.
    Model('gpt-6-astra', 'openai', 'analyst', Price('10.00', '50.00', '12.50', cache_read='1.00'),
          16000, 'medium', ('low', 'medium', 'high', 'xhigh', 'max'), OPENAI_PRICES, api='responses',
          pinned=False, long_price=Price('20.00', '75.00', cache_read='2.00'), long_above=272000),
    Model('gpt-6-luna', 'openai', 'analyst', Price('0.10', '0.50', '0.125', cache_read='0.01'),
          16000, 'medium', OPENAI_NEW, OPENAI_PRICES, api='responses', pinned=False),
    Model('gpt-6.1-sol', 'openai', 'analyst', Price('2.00', '10.00', '2.50', cache_read='0.10'),
          16000, 'medium', ('low', 'medium', 'high', 'xhigh', 'max'), OPENAI_PRICES, api='responses', pinned=False),
    Model('gpt-6.1-terra', 'openai', 'analyst', Price('1.00', '5.00', '1.25', cache_read='0.05'),
          16000, 'medium', OPENAI_NEW, OPENAI_PRICES, api='responses', pinned=False),

    # Claude: prompt caching on (5-minute writes), adaptive thinking (the default
    # and the only mode on these models), effort always sent.
    Model('claude-opus-5-5', 'anthropic', 'analyst', Price('4', '20', '5', '8', '0.20'),
          16000, 'medium', CLAUDE_EFFORTS, SKILL_PRICES, fallbacks=('claude-opus-5',)),
    Model('claude-sonnet-5-5', 'anthropic', 'analyst', Price('2', '10', '2.50', '4', '0.10'),
          16000, 'medium', CLAUDE_EFFORTS, SKILL_PRICES, fallbacks=('claude-sonnet-5',)),
    # Two rate cards chosen by prompt length; no server-side fallback exists.
    Model('claude-haiku-5-5', 'anthropic', 'analyst', Price('0.10', '0.50', '0.125', '0.20', '0.01'),
          16000, 'medium', CLAUDE_EFFORTS, SKILL_PRICES,
          long_price=Price('0.50', '2.50', '0.625', '1.00', '0.05'), long_above=100000),

    # Reached only as a server-side fallback after a refusal; never selectable.
    # Cache rates are the standard 1.25x / 2x / 0.1x multiples of the input rate.
    Model('claude-opus-5', 'anthropic', 'fallback', Price('5', '25', '6.25', '10', '0.50'),
          16000, None, CLAUDE_EFFORTS, SKILL_PRICES),
    Model('claude-sonnet-5', 'anthropic', 'fallback', Price('2', '10', '2.50', '4', '0.20'),
          16000, None, CLAUDE_EFFORTS, SKILL_PRICES),

    # The eval judge. Never served by the analyst routes.
    Model('claude-fable-5-2', 'anthropic', 'judge', Price('8', '40', '10', '16', '0.20'),
          16000, 'high', CLAUDE_EFFORTS, SKILL_PRICES),
]}

DEFAULT_MODEL = 'gpt-5.4-mini-2026-03-17'
KEY_NAMES = {'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY'}


def chain(entry):
    """The requested model followed by its fallbacks: every model a request may bill."""
    return [entry] + [MODELS[m] for m in entry.fallbacks]


def reservation(entry):
    """Hold for one call of this contestant: every model in the chain at its full
    input and output limits, at the dearest rate on any of its cards. A valid
    settlement can never exceed it. Refuses an unconfirmed price."""
    total = Fraction(0)
    for m in chain(entry):
        if m.price is None:
            raise ValueError(f'{m.id} has no confirmed price.')
        worst_in, worst_out = m.worst_price()
        total += m.max_input * worst_in + entry.max_output * worst_out
    return math.ceil(total)


def _model_for(entry, served):
    return next((m for m in chain(entry) if m.serves(served)), None)


def attempt_cost(entry, attempt, *, list_price=False):
    """Microdollars (as a Fraction) for one billed attempt, or None if uncertain.

    `attempt` holds the served model ID and integer token counts. Anthropic:
    input, cache_write_5m, cache_write_1h, cache_read (disjoint) and output.
    OpenAI: input (all prompt tokens), cached (a part of input) and output.
    `list_price` prices OpenAI cached tokens at the cached rate and the rest at
    the input rate, as a bill without cache writes would; it is for the record,
    never for the ledger.
    """
    m = _model_for(entry, attempt.get('model'))
    if m is None or m.price is None:
        return None  # a model outside the request's own chain cannot be priced
    names = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'cached', 'output')
    counts = {n: attempt.get(n, 0) for n in names}
    if any(type(v) is not int or v < 0 for v in counts.values()):
        return None
    if m.provider == 'openai':
        prompt = counts['input']
        if counts['cache_write_5m'] or counts['cache_write_1h'] or counts['cache_read'] or counts['cached'] > prompt:
            return None
    else:
        prompt = sum(counts[n] for n in names[:4])
        if counts['cached']:
            return None
    if prompt > m.max_input or counts['output'] > entry.max_output:
        return None
    card = m.long_price if m.long_price and prompt > m.long_above else m.price
    out = counts['output'] * card.rate('output')
    if m.provider == 'openai':
        if list_price:
            return (prompt - counts['cached']) * card.rate('input') + counts['cached'] * card.rate('cache_read') + out
        return prompt * card.openai_input() + out
    return sum(counts[n] * card.rate(n) for n in names[:4]) + out


def usage_cost(entry, attempts, *, list_price=False):
    """Total microdollars for a response's billed attempts, rounded up, or None.

    Each model in the chain may bill at most once per request; anything else is
    unexpected and keeps the full hold.
    """
    if not isinstance(attempts, list) or not attempts or len(attempts) > len(chain(entry)):
        return None
    if any(not isinstance(a, dict) for a in attempts):
        return None
    models = [_model_for(entry, a.get('model')) for a in attempts]
    if None in models or len({m.id for m in models}) != len(attempts):
        return None
    costs = [attempt_cost(entry, a, list_price=list_price) for a in attempts]
    if any(c is None for c in costs):
        return None
    total = math.ceil(sum(costs))
    return total if total <= reservation(entry) else None


def contestant(model_id, effort=None):
    """(entry for the pair, problem). Only an effort the model documents is accepted."""
    entry = MODELS.get(model_id)
    if entry is None or entry.role != 'analyst':
        return None, f'"{model_id}" is not an allowlisted analyst model.'
    if entry.price is None:
        return None, f'{model_id} has no confirmed price, so it cannot be run.'
    effort = effort or entry.effort
    if effort not in entry.efforts:
        return None, (f'Effort "{effort}" is not supported by {model_id}; '
                      f'use one of: {", ".join(entry.efforts)}.')
    max_output = entry.max_output
    if effort != 'none' and max_output < REASONING_MAX_OUTPUT:
        max_output = REASONING_MAX_OUTPUT
    return replace(entry, effort=effort, max_output=max_output), None


def select(environ=None):
    """(contestant entry, problem). The analyst is disabled with `problem` set
    when the configured contestant is not allowlisted.

    GLEN_ANALYST_MODEL is a model ID or `model@effort`; GLEN_ANALYST_EFFORT
    sets the effort separately. Without either, OPENAI_MODEL (older .env files)
    names the default when it is an allowlisted OpenAI model.
    """
    environ = os.environ if environ is None else environ
    wanted = (environ.get('GLEN_ANALYST_MODEL') or '').strip()
    effort = (environ.get('GLEN_ANALYST_EFFORT') or '').strip()
    if '@' in wanted:
        wanted, _, paired = wanted.partition('@')
        if effort and effort != paired:
            return None, f'GLEN_ANALYST_MODEL says effort "{paired}" but GLEN_ANALYST_EFFORT says "{effort}".'
        effort = paired
    if not wanted:
        legacy = (environ.get('OPENAI_MODEL') or '').strip()
        known = MODELS.get(legacy)
        wanted = legacy if known and known.role == 'analyst' and known.provider == 'openai' else DEFAULT_MODEL
    entry, problem = contestant(wanted, effort or None)
    return entry, (f'GLEN_ANALYST_MODEL: {problem}' if problem else None)


def api_key(entry, environ=None):
    environ = os.environ if environ is None else environ
    return environ.get(KEY_NAMES[entry.provider], '') if entry else ''
