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
- At most one emoji, only a country flag or 🚨 for genuinely breaking, high-stakes news. No hashtags. No links. No @handles (they are added later).
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


def _post_json(url, body, headers, timeout=40):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'TrendingItemsPoster/2.0', **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as error:
        # Never log response bodies or headers; they can echo request data.
        raise EditorUnavailable('HTTP %s' % error.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise EditorUnavailable(type(error).__name__) from None


def gemini(prompt):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise EditorUnavailable('no Gemini key')
    url = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent' % GEMINI_MODEL
    data = _post_json(url, {
        'systemInstruction': {'parts': [{'text': SYSTEM}]},
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.6, 'maxOutputTokens': 4096,
                             'responseMimeType': 'application/json', 'responseSchema': SCHEMA},
    }, {'x-goog-api-key': key})
    try:
        parts = data['candidates'][0]['content']['parts']
        return ''.join(p.get('text', '') for p in parts if not p.get('thought'))
    except (KeyError, IndexError, TypeError):
        raise EditorUnavailable('empty Gemini response') from None


def groq(prompt):
    key = os.environ.get('GROQ_API_KEY')
    if not key:
        raise EditorUnavailable('no Groq key')
    data = _post_json('https://api.groq.com/openai/v1/chat/completions', {
        'model': GROQ_MODEL, 'temperature': 0.6, 'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}],
    }, {'Authorization': 'Bearer '+key})
    try:
        return data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        raise EditorUnavailable('empty Groq response') from None


def build_prompt(reports, already_posted=(), update_of=None):
    lines = ['REPORTS ABOUT ONE EVENT:']
    for i, (outlet, text) in enumerate(reports[:8], 1):
        lines.append('[%d] %s:\n%s' % (i, outlet, text.strip()[:1200]))
    if update_of:
        lines.append('\nWE ALREADY POSTED THIS EVENT AS:\n'+update_of+
                     '\nOnly post if the reports contain a genuinely NEW development; write it as an update ("Update: ...").')
    if already_posted:
        lines.append('\nALREADY POSTED RECENTLY (do not repeat):')
        lines.extend('- '+t.replace('\n', ' ')[:200] for t in already_posted[:8])
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
    for name, provider in (('gemini', gemini), ('groq', groq)):
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
            ok, why = validate(result, sources)
            if ok:
                if result['reply'] and not validate(dict(post=True, text=result['reply']), sources)[0]:
                    result['reply'] = ''
                return result
            errors.append(name+': '+why)
            # One corrective retry with the exact rejection reason.
            prompt = base_prompt+'\n\nYOUR PREVIOUS DRAFT WAS REJECTED ('+why+'):\n'+result['text']+'\nRewrite it following every rule.'
    raise EditorUnavailable('; '.join(errors) or 'no provider')


def adjustment(result):
    """How the AI verdict changes the rule score: news + talk around a neutral 5."""
    return 3*(result['newsworthiness']-5)+4*(result['talkability']-5)
