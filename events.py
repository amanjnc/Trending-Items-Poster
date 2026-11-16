"""Group stories from different outlets into events so the bot can see what is spreading.

An event is one real-world story reported by several sources. Its size and how quickly
new outlets join it ("velocity") are the strongest signals that people are talking
about it. Clustering is rule-based: shared proper names plus overlapping key words,
within a bounded time distance.
"""
import hashlib
import re
import unicodedata

STOP = set('''the a an and or to of in on for from at by with is are was were has have had that this it its as be
been being says said say just breaking update new news after over amid into about than more most will would could
should can may might not no but if their they them he she his her we our you your who what when where which why how
also up out off all any some one two three first last year years day days week weeks today yesterday tonight report
reports reported according per via live latest watch video photo photos us`'''.split())
# Capitalised words that start sentences or headlines but identify nothing.
WEAK_CAPS = set('''the a an breaking update just live watch video photos new why how what who when where this that
these those it he she they we after amid as in on at for from by with is are was were will says said report
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august
september october november december his her their its our last yes no among also but more some'''.split())


DEMONYMS = {'israeli': 'israel', 'iranian': 'iran', 'russian': 'russia', 'ukrainian': 'ukraine', 'chinese': 'china',
            'american': 'us', 'u.s': 'us', 'usa': 'us', 'washington': 'us', 'british': 'uk', 'britain': 'uk',
            'french': 'france', 'german': 'germany', 'indian': 'india', 'pakistani': 'pakistan', 'syrian': 'syria',
            'lebanese': 'lebanon', 'palestinian': 'palestine', 'turkish': 'turkey', 'saudi': 'saudi', 'venezuelan': 'venezuela',
            'japanese': 'japan', 'korean': 'korea', 'taiwanese': 'taiwan', 'european': 'eu', 'canadian': 'canada',
            'mexican': 'mexico', 'kremlin': 'russia', 'pentagon': 'pentagon', 'beijing': 'china', 'moscow': 'russia',
            'kyiv': 'ukraine', 'tehran': 'iran'}


def _fold(text):
    return unicodedata.normalize('NFKC', text).casefold()


def stem(word):
    word = DEMONYMS.get(word, word)
    for suffix in ('ments', 'ment', 'ings', 'ing', 'ers', 'ed', 's'):
        if len(word) > len(suffix)+3 and word.endswith(suffix):
            word = word[:-len(suffix)]
            break
    return word[:-1] if len(word) > 4 and word.endswith('e') else word


def key_terms(text):
    """(proper_names, content_words, numbers) used to compare two stories."""
    text = re.sub(r'https?://\S+', ' ', text)
    names = set()
    for match in re.finditer(r"(?<![\w@#])([A-Z][\w'’.-]*[\w]|[A-Z]{2,})", text):
        word = match[1].rstrip('.').replace('’', "'")
        word = re.sub(r"'s$", '', word)
        if _fold(word) in WEAK_CAPS or len(word) < 2:
            continue
        names.add(stem(_fold(word)))
    words = {stem(w) for w in re.findall(r'[^\W\d_]{3,}', _fold(text)) if w not in STOP}
    numbers = set(re.findall(r'\d+(?:[.,]\d+)?', text))
    return names, words, numbers


def similar(a, b):
    """True when two stories describe the same event. Shared distinctive names must be
    backed by shared *non-name* words, so "Trump meets Putin" and "Trump sanctions
    Putin" stay separate while differently worded reports of one event merge."""
    an, aw, _ = a
    bn, bw, _ = b
    shared_names = len(an & bn)
    ac, bc = aw-an-bn, bw-an-bn
    common = len(ac & bc)
    content = common/max(1, min(len(ac), len(bc)))
    if shared_names >= 3 and content >= .2:
        return True
    if shared_names >= 2 and content >= .25:
        return True
    if shared_names >= 1 and content >= .5 and common >= 3:
        return True
    return content >= .6 and common >= 5


def cluster(stories, max_gap=8*3600):
    """Union-find clustering. `stories` need id, body, source, published.
    Returns a list of events (dicts) sorted by size, largest first."""
    terms = [key_terms(s['body']) for s in stories]
    parent = list(range(len(stories)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(stories)):
        for j in range(i+1, len(stories)):
            if abs(stories[i]['published']-stories[j]['published']) > max_gap:
                continue
            if find(i) != find(j) and similar(terms[i], terms[j]):
                parent[find(i)] = find(j)
    groups = {}
    for i, story in enumerate(stories):
        groups.setdefault(find(i), []).append(i)
    events = []
    for members in groups.values():
        items = [stories[i] for i in members]
        names = set().union(*(terms[i][0] for i in members))
        # The most common names across members describe the event best.
        counts = {}
        for i in members:
            for n in terms[i][0]:
                counts[n] = counts.get(n, 0)+1
        top = sorted(counts, key=lambda n: (-counts[n], n))[:6]
        events.append(dict(
            id=hashlib.sha256('|'.join(sorted(s['id'] for s in items)).encode()).hexdigest()[:16],
            items=sorted(items, key=lambda s: s['published']),
            names=names, top_names=top,
            numbers=set().union(*(terms[i][2] for i in members)),
        ))
    return sorted(events, key=lambda e: -len(e['items']))


def outlet(source):
    """Distinct outlet behind a source label (aggregators list the real publisher)."""
    if ' / ' in source:
        return source.split(' / ', 1)[1].strip().casefold()
    return source.casefold()


def metrics(event, now):
    items = event['items']
    outlets = {outlet(s['source']) for s in items}
    recent = {outlet(s['source']) for s in items if now-s['published'] <= 3600}
    first = min(s['published'] for s in items)
    latest = max(s['published'] for s in items)
    return dict(
        outlets=len(outlets),
        outlets_last_hour=len(recent),
        first_seen=first,
        latest=latest,
        age_hours=max(0.0, (now-first)/3600),
        views=max((s.get('views') or 0) for s in items),
        related=max((s.get('related') or 0) for s in items),
        posted=any(s.get('posted') for s in items),
    )
