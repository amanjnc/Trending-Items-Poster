"""AI editor: decide if an event is worth posting and write a hook-first post.

Providers: Google Gemini (GEMINI_API_KEY, default) with optional Groq fallback
(GROQ_API_KEY). The model sees ONLY the source reports for one event. A strict
validator rejects outputs with links, invented numbers or invented quotations;
callers then fall back to the rule-based draft. Nothing here posts anything.
"""
import json
import os
import re
import unicodedata
import urllib.error
import urllib.request

GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile')
MAX_CHARS = 270

SYSTEM = """You are the editor of a fast X (Twitter) news account focused on politics and geopolitics, with big tech/business/crypto stories only when they are huge or political. Your goal: posts people stop for, reply to, quote and share, while staying strictly accurate.

You receive reports from several outlets about ONE news event. Decide whether it deserves a post and write it.

ACCURACY RULES (never break these):
- Use only facts stated in the reports. Never add numbers, names, dates, motives, predictions or context that the reports do not contain.
- Quotes must be copied exactly from a report. Do not invent or paraphrase inside quotation marks.
- If only state media (RT, BRICS News) or a single channel reports a claim, attribute it ("Russian state media says...", "per Reuters", "Iran says...").
- Keep uncertainty: "reportedly", "claims", "says" stay when the source uses them.

WRITING RULES:
- Line 1 is the hook: the single most surprising or consequential fact, in plain punchy words (max ~100 characters). Lead with the news, not the outlet.
- Then 1-3 short lines with the key specifics (who, what, the number, the sharpest direct quote). Blank line between lines.
- Optionally end with ONE short, neutral question that invites people to reply, only if it arises naturally from the story (no "What do you think?", no "Thoughts?").
- Emoji: usually none. At most one, at the very start: a country flag, or 🚨 ONLY for breaking, high-stakes news (war, deaths, major arrests/resignations). No hashtags. No links. No @handles (they are added later).
- Neutral and factual tone: no insults, no partisan opinion, no fake urgency, no clickbait that the facts don't support.
- English only. Hard limit: 260 characters.

SET post=false when: it is an ad, promo, giveaway, opinion column, explainer, review, minor/local or routine news, too vague to state clearly, graphic gore, or it repeats something listed under ALREADY POSTED without a genuinely new development.

Return JSON only:
{"post": bool, "newsworthiness": 1-10, "talkability": 1-10, "text": "...", "reply": "optional 1-2 sentence extra context from the reports for a follow-up reply, or empty", "reason": "short reason"}
talkability = how likely people are to reply, argue, quote or share it."""

SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'post': {'type': 'BOOLEAN'},
        'newsworthiness': {'type': 'INTEGER'},
        'talkability': {'type': 'INTEGER'},
        'text': {'type': 'STRING'},
        'reply': {'type': 'STRING'},
        'reason': {'type': 'STRING'},
    },
    'required': ['post', 'newsworthiness', 'talkability', 'text'],
}


class EditorUnavailable(RuntimeError):
    pass


def available():
    return bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GROQ_API_KEY'))


def _error_detail(error):
    """Short provider error message (e.g. 'model not found'); never headers or keys."""
    try:
        body = json.loads(error.read(20_000))
        message = body.get('error', {}).get('message', '') if isinstance(body.get('error'), dict) else str(body.get('error', ''))
    except Exception:
        message = ''
    message = re.sub(r'(?i)(key|token)[=:]\s*\S+', r'\1=***', message)
    return ('HTTP %s %s' % (error.code, message[:160])).strip()


class HTTPFailure(EditorUnavailable):
    def __init__(self, status, detail):
        self.status = status
        super().__init__(detail)


def _request(url, headers, body=None, timeout=75):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     method='POST' if body is not None else 'GET',
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'TrendingItemsPoster/2.0', **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(4_000_000))
    except urllib.error.HTTPError as error:
        raise HTTPFailure(error.code, _error_detail(error)) from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        raise EditorUnavailable(type(error).__name__) from None


def _post_json(url, body, headers, timeout=75):
    return _request(url, headers, body, timeout)


# Working settings discovered at runtime (model names change; thinking options differ by model).
_gemini_state = {'model': None, 'thinking': None, 'down': False, 'discovered': False}
_groq_state = {'model': None, 'listed': None}
GEMINI_TIMEOUT = int(os.environ.get('GEMINI_TIMEOUT', '30'))
# Groq first: Gemini generation requests currently hang from GitHub runners.
PROVIDER_ORDER = [p.strip() for p in os.environ.get('AI_PROVIDER_ORDER', 'groq,gemini').split(',') if p.strip()]
GEMINI_THINKING = ({'thinkingLevel': 'low'}, {'thinkingBudget': 0}, None)


def _version_key(name):
    return [int(n) for n in re.findall(r'\d+', name)] or [0]


def gemini_models(key):
    data = _request('https://generativelanguage.googleapis.com/v1beta/models?pageSize=200', {'x-goog-api-key': key}, timeout=20)
    names = [m['name'].split('/', 1)[-1] for m in data.get('models', [])
             if 'generateContent' in m.get('supportedGenerationMethods', [])]
    flash = [n for n in names if 'flash' in n and not re.search(r'image|tts|audio|live|exp|preview|thinking|8b', n)]
    return sorted(flash, key=lambda n: ('lite' in n, [-v for v in _version_key(n)]))


def gemini(prompt):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise EditorUnavailable('no Gemini key')
    if _gemini_state['down']:
        raise EditorUnavailable('Gemini skipped after an earlier timeout in this run')
    models = [_gemini_state['model'] or GEMINI_MODEL]
    thinking = [_gemini_state['thinking']] if _gemini_state['model'] else list(GEMINI_THINKING)
    tried = set()
    while models:
        model = models.pop(0)
        if model in tried:
            continue
        tried.add(model)
        for option in thinking:
            config = {'temperature': 0.6, 'maxOutputTokens': 4096,
                      'responseMimeType': 'application/json', 'responseSchema': SCHEMA}
            if option:
                config['thinkingConfig'] = option
            url = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent' % model
            try:
                data = _request(url, {'x-goog-api-key': key}, {
                    'systemInstruction': {'parts': [{'text': SYSTEM}]},
                    'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
                    'generationConfig': config}, timeout=GEMINI_TIMEOUT)
            except HTTPFailure as error:
                if error.status == 400 and option is not None:
                    continue  # this model does not accept that thinking option
                if error.status == 404 and not _gemini_state['discovered']:
                    _gemini_state['discovered'] = True
                    models.extend([m for m in gemini_models(key) if m not in tried][:3])  # retired name: pick a current flash model
                    break
                raise
            except EditorUnavailable:
                # This model hangs or is unreachable: try a couple of other current flash
                # models once, then stop spending the timeout on every event this run.
                if not _gemini_state.get('discovered'):
                    _gemini_state['discovered'] = True
                    try:
                        models.extend(m for m in gemini_models(key) if m not in tried)
                        del models[2:]
                    except EditorUnavailable:
                        pass
                if models:
                    break
                _gemini_state['down'] = True
                raise
            try:
                parts = data['candidates'][0]['content']['parts']
                text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
            except (KeyError, IndexError, TypeError):
                raise EditorUnavailable('empty Gemini response') from None
            _gemini_state.update(model=model, thinking=option)
            return text
        thinking = list(GEMINI_THINKING)
    _gemini_state['down'] = True
    raise EditorUnavailable('no usable Gemini model')


def remembered():
    """Working provider settings to persist between scheduled runs."""
    return {'gemini_model': _gemini_state['model'], 'gemini_thinking': _gemini_state['thinking'],
            'groq_model': _groq_state['model']}


def remember(value):
    if value.get('gemini_model'):
        _gemini_state.update(model=value['gemini_model'], thinking=value.get('gemini_thinking'))
    if value.get('groq_model'):
        _groq_state['model'] = value['groq_model']


GROQ_PREFERENCES = ('llama-3.3-70b-versatile', 'openai/gpt-oss-120b', 'moonshotai/kimi-k2-instruct',
                    'meta-llama/llama-4-maverick-17b-128e-instruct', 'qwen/qwen3-32b', 'openai/gpt-oss-20b')


def groq_models(key):
    data = _request('https://api.groq.com/openai/v1/models', {'Authorization': 'Bearer '+key}, timeout=20)
    names = [m['id'] for m in data.get('data', []) if m.get('active', True)]
    names = [n for n in names if not re.search(r'whisper|guard|tts|playai|distil|compound|orpheus', n)]
    preferred = [n for n in GROQ_PREFERENCES if n in names]
    return preferred+[n for n in names if n not in preferred]


def groq(prompt):
    key = os.environ.get('GROQ_API_KEY')
    if not key:
        raise EditorUnavailable('no Groq key')
    models = [_groq_state['model'] or GROQ_MODEL]
    tried = set()
    while models:
        model = models.pop(0)
        if model in tried:
            continue
        tried.add(model)
        try:
            data = _request('https://api.groq.com/openai/v1/chat/completions', {'Authorization': 'Bearer '+key}, {
                'model': model, 'temperature': 0.6, 'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}]})
        except HTTPFailure as error:
            if error.status in (400, 404, 429) and len(tried) <= 3:
                # Retired/unsupported model, or this model's per-minute token limit is used
                # up: each Groq model has its own limit, so try another current model.
                if not _groq_state.get('listed'):
                    _groq_state['listed'] = groq_models(key)
                models.extend(m for m in _groq_state['listed'] if m not in tried and m not in models)
                del models[3:]
                if models:
                    continue
            raise
        try:
            text = data['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            raise EditorUnavailable('empty Groq response') from None
        _groq_state['model'] = model
        return text
    raise EditorUnavailable('no usable Groq model')


def probe_gemini(key, models):
    """Time a minimal request (no schema, no system prompt) on each model."""
    import time
    results = {}
    candidates = [m for m in models if 'omni' not in m][:4]+[m for m in models if 'lite' in m][:1]
    for model in dict.fromkeys(candidates):
        for label, config in (('plain', {'maxOutputTokens': 64}),
                              ('json+schema', {'maxOutputTokens': 512, 'responseMimeType': 'application/json', 'responseSchema': SCHEMA})):
            start = time.monotonic()
            try:
                _request('https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent' % model, {'x-goog-api-key': key},
                         {'contents': [{'role': 'user', 'parts': [{'text': 'Reply with the word ok as JSON {"post": false, "newsworthiness": 1, "talkability": 1, "text": "ok"}.'}]}],
                          'generationConfig': config}, timeout=60)
                outcome = 'ok'
            except EditorUnavailable as error:
                outcome = str(error)[:120]
            results['%s %s' % (model, label)] = '%s in %.1fs' % (outcome, time.monotonic()-start)
    return results


def check():
    """Diagnostic: one tiny request per configured provider; reports model or error."""
    sample = [('example', 'The Senate voted 52-48 to pass the budget bill on Monday, sending it to the House.')]
    report = {}
    for name, provider in (('gemini', gemini), ('groq', groq)):
        if not os.environ.get(name.upper()+'_API_KEY'):
            report[name] = 'no key'
            continue
        try:
            result = _json(provider(build_prompt(sample)))
            state = _gemini_state if name == 'gemini' else _groq_state
            report[name] = dict(ok=True, model=state['model'], thinking=state.get('thinking'),
                                sample=str(result.get('text', ''))[:200])
        except EditorUnavailable as error:
            report[name] = dict(ok=False, error=str(error))
            if name == 'gemini':
                # Distinguish "Google unreachable" from "this model is slow/hanging".
                try:
                    models = gemini_models(os.environ['GEMINI_API_KEY'])
                    report[name]['available_flash_models'] = models[:6]
                    report[name]['probe'] = probe_gemini(os.environ['GEMINI_API_KEY'], models)
                except EditorUnavailable as listing:
                    report[name]['model_listing_error'] = str(listing)
    return report


def build_prompt(reports, already_posted=(), update_of=None):
    lines = ['REPORTS ABOUT ONE EVENT:']
    # Kept compact: free-tier providers limit tokens per minute.
    for i, (outlet, text) in enumerate(reports[:5], 1):
        lines.append('[%d] %s:\n%s' % (i, outlet, text.strip()[:700]))
    if update_of:
        lines.append('\nWE ALREADY POSTED THIS EVENT AS:\n'+update_of+
                     '\nOnly post if the reports contain a genuinely NEW development; write it as an update ("Update: ...").')
    if already_posted:
        lines.append('\nALREADY POSTED RECENTLY (do not repeat):')
        lines.extend('- '+t.replace('\n', ' ')[:140] for t in already_posted[:6])
    return '\n'.join(lines)


def _json(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise EditorUnavailable('no JSON object')
    try:
        return json.loads(text[start:end+1])
    except ValueError:
        raise EditorUnavailable('invalid JSON') from None


def weight(text):
    return sum(1 if ord(ch) < 0x1100 else 2 for ch in text)


def _norm(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    text = text.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'\s+', ' ', text)


def _numbers(text):
    return {n.replace(',', '') for n in re.findall(r'\d[\d,]*(?:\.\d+)?', text)}


def validate(result, sources):
    """Return (ok, reason). Conservative: reject anything not grounded in sources."""
    text = (result.get('text') or '').strip()
    if not result.get('post'):
        return True, 'declined'
    if not text:
        return False, 'empty text'
    if re.search(r'https?://|www\.|\b\w+\.(?:com|org|net)/', text, re.I):
        return False, 'contains link'
    if '@' in text:
        return False, 'contains handle'
    if len(re.findall(r'#\w', text)) > 1:
        return False, 'hashtags'
    if weight(text) > MAX_CHARS:
        return False, 'too long'
    source_text = _norm(' '.join(sources))
    source_numbers = _numbers(source_text)
    invented = [n for n in _numbers(text) if n not in source_numbers]
    if invented:
        return False, 'number not in sources'
    for quote in re.findall(r'["“]([^"”]{12,})["”]', text):
        if _norm(quote).strip(' .,!?') not in source_text:
            return False, 'quote not in sources'
    return True, 'ok'


def edit(reports, already_posted=(), update_of=None):
    """Return a validated editorial decision dict, or raise EditorUnavailable."""
    if not available():
        raise EditorUnavailable('no AI key configured')
    base_prompt = build_prompt(reports, already_posted, update_of)
    sources = [t for _, t in reports]
    errors = []
    providers = {'gemini': gemini, 'groq': groq}
    for name in [n for n in PROVIDER_ORDER if n in providers]:
        provider = providers[name]
        prompt = base_prompt
        for attempt in range(2):
            try:
                result = _json(provider(prompt))
            except EditorUnavailable as error:
                errors.append(name+': '+str(error))
                break
            try:
                result = dict(post=bool(result.get('post')),
                              newsworthiness=max(1, min(10, int(result.get('newsworthiness') or 1))),
                              talkability=max(1, min(10, int(result.get('talkability') or 1))),
                              text=str(result.get('text') or '').strip(),
                              reply=str(result.get('reply') or '').strip(),
                              reason=str(result.get('reason') or '')[:200],
                              provider=name)
            except (TypeError, ValueError):
                errors.append(name+': malformed fields')
                break
            result['text'] = tidy(result['text'])
            ok, why = validate(result, sources)
            if ok:
                if result['reply'] and not validate(dict(post=True, text=result['reply']), sources)[0]:
                    result['reply'] = ''
                return result
            errors.append(name+': '+why)
            # One corrective retry with the exact rejection reason.
            prompt = base_prompt+'\n\nYOUR PREVIOUS DRAFT WAS REJECTED ('+why+'):\n'+result['text']+'\nRewrite it following every rule.'
    raise EditorUnavailable('; '.join(errors) or 'no provider')


def tidy(text):
    """Keep at most one leading emoji (flag or 🚨); drop decorative 🚨 elsewhere."""
    text = text.strip()
    if text[:1] == '🚨':
        head, rest = '🚨', text[1:]
    elif len(text) > 1 and all(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in text[:2]):
        head, rest = text[:2], text[2:]
    else:
        head, rest = '', text
    rest = re.sub(r'[ \t]*🚨[ \t]*', ' ', rest)
    rest = '\n'.join(line.strip() for line in rest.split('\n')).strip()
    return (head+' '+rest) if head else rest


def adjustment(result):
    """How the AI verdict changes the rule score: news + talk around a neutral 5."""
    return 3*(result['newsworthiness']-5)+4*(result['talkability']-5)
