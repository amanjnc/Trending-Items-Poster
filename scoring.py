"""Decide which events are worth posting: hot, on-topic, talkable, fresh, not repetitive.

score = heat + interest + talkability + freshness + media + source - fatigue

All parts are small, explainable numbers. They are saved in each prepared post's
`reasons` so the job summary shows why something was (not) chosen. The engagement
loop (engagement.learn) can scale category weights within fixed bounds.
"""
import math
import re

# Topic lexicons. Politics/geopolitics is the account's focus; others are crossover.
TOPICS = {
    'geopolitics': r'\b(?:war|ceasefire|truce|missiles?|drones?|airstrikes?|strikes? on|troops|military|army|navy|nato|un security council|'
                   r'invasion|offensive|nuclear|sanctions?|embassy|diplomat\w*|foreign minister|summit|border|hostages?|'
                   r'israel\w*|gaza|hamas|hezbollah|iran\w*|russia\w*|ukrain\w*|kremlin|putin|zelensk\w*|netanyahu|china|chinese|'
                   r'taiwan|north korea|kim jong|syria\w*|lebanon|yemen|houthis?|venezuela\w*|maduro|pakistan\w*|india\b|modi|'
                   r'erdogan|turkey|saudi|mbs|qatar|brics|xi jinping|pentagon|idf|irgc|coup|annex\w*)\b',
    'politics': r'\b(?:trump|biden|harris|vance|white house|congress|senate|house speaker|republicans?|democrats?|gop|'
                r'election|vote[sd]?|voters|ballot|polls?|governor|supreme court|scotus|impeach\w*|indict\w*|'
                r'executive order|shutdown|filibuster|campaign|president|prime minister|parliament|minister|'
                r'starmer|macron|merz|carney|milei|lula|bukele|musk|doge|fbi|doj|attorney general|ice\b|deport\w*|'
                r'immigration|tariffs?|epstein|newsom|desantis|aoc|ocasio-cortez|mamdani|pelosi|schumer|mcconnell|'
                r'hegseth|rubio|bondi|patel|kennedy|rfk)\b',
    'business': r'\b(?:stocks?|shares|market|wall street|dow|nasdaq|s&p|fed\b|federal reserve|interest rates?|inflation|'
                r'recession|gdp|earnings|revenue|ipo|merger|acquisition|layoffs?|bankrupt\w*|ceo|billion|trillion|'
                r'oil prices?|opec|jobs report|unemployment)\b',
    'tech': r'\b(?:ai\b|artificial intelligence|openai|chatgpt|gpt-\d|anthropic|claude|gemini|google|apple|iphone|microsoft|'
            r'meta\b|nvidia|tesla|spacex|starlink|x\.com|twitter|tiktok|chips?|semiconductors?|tsmc|robot\w*|data center)\b',
    'crypto': r'\b(?:bitcoin|btc|ethereum|eth\b|crypto\w*|stablecoins?|solana|xrp|binance|coinbase|blockchain)\b',
}
BASE_INTEREST = {'politics': 22, 'geopolitics': 22, 'business': 8, 'tech': 8, 'crypto': 6, 'other': 0}
# A crossover story becomes "political" when power, law or states are involved.
POLITICAL_ANGLE = (r'\b(?:ban\w*|sanction\w*|tariffs?|export controls?|regulat\w*|antitrust|lawsuit|sues?|sued|congress|senate|'
                   r'white house|trump|government|lawmakers?|national security|china|beijing|eu\b|european commission|'
                   r'election|pentagon|military|investigation|probe|subpoena|testif\w*)\b')
# Household names whose news reliably gets reactions.
BIG_NAMES = (r'\b(?:trump|putin|zelensk\w*|netanyahu|xi jinping|musk|elon|modi|macron|starmer|khamenei|kim jong[- ]un|'
             r'erdogan|biden|obama|vance|harris|pope|mbs|mohammed bin salman|jensen huang|sam altman|zuckerberg|bezos|'
             r'tim cook|bill gates|warren buffett|tucker carlson|joe rogan|taylor swift|epstein|mamdani|rfk|hegseth|rubio)\b')
# Words that make people argue, react or share.
TALK = (r'\b(?:breaking|first[- ]ever|historic|record|shock\w*|stun\w*|slams?|blasts?|fires?|fired|resign\w*|ousted|arrest\w*|'
        r'indict\w*|charged|convicted|sentenced|banned?|bans|blocks?|threat\w*|warns?|ultimatum|declares?|'
        r'refuses?|rejects?|vetoe?s?|leak\w*|secret\w*|scandal|accus\w*|admits?|reverses?|u-turn|backfire\w*|'
        r'collapse\w*|crash\w*|surge\w*|plunge\w*|soar\w*|kill\w*|dead|assassinat\w*|explosion|invade\w*|'
        r'shut(?:s|ting)? down|pardon\w*|deport\w*|tariffs?|nuclear|war\b|ceasefire|coup|emergency|unprecedented|'
        r'biggest|largest|worst|never before|for the first time)\b')
FILLER = (r'\b(?:podcast|newsletter|quiz|crossword|horoscope|recipe|review:|hands[- ]on|deal of the day|best \w+ to buy|'
          r'how to watch|livestream|opinion|op-ed|analysis:|explainer|sponsored|giveaway|airdrop|presale)\b')


def topic_hits(text):
    text = text.casefold()
    return {name: len(re.findall(pattern, text)) for name, pattern in TOPICS.items()}


def categorise(text, hint=None):
    hits = topic_hits(text)
    best = max(hits, key=lambda k: (hits[k], k == hint, BASE_INTEREST[k]))
    if hits[best] == 0:
        return hint or 'other'
    # Politics and geopolitics outrank incidental tech/business words.
    for focus in ('geopolitics', 'politics'):
        if hits[focus] and hits[focus] >= hits[best]-1:
            return focus
    return best


def heat(m, trend=0):
    """How much the story is spreading right now."""
    outlets = min(36.0, 14*math.log2(1+max(0, m['outlets']-1))) if m['outlets'] > 1 else 0.0
    velocity = min(18.0, 6.0*max(0, m['outlets_last_hour']-1))
    views = min(10.0, 2.5*math.log10(1+m['views']))
    related = min(12.0, 3.0*math.log2(1+m['related']))
    return round(outlets+velocity+views+related+trend, 2)


def freshness(age_hours):
    """Steep decay: half-life two hours, nothing older than a day."""
    return round(30*0.5**(age_hours/2), 2)


def interest(text, category, weights=None):
    weights = weights or {}
    base = BASE_INTEREST.get(category, 0)*weights.get(category, 1.0)
    if not any(topic_hits(text).values()):
        base *= .4  # Only the channel's usual topic, nothing in the story itself.
    lower = text.casefold()
    if category in ('business', 'tech', 'crypto') and re.search(POLITICAL_ANGLE, lower):
        base += 10
    names = len(set(re.findall(BIG_NAMES, lower)))
    return round(base+min(14, 7*names), 2)


def talkability(text):
    lower = text.casefold()
    hits = len(set(re.findall(TALK, lower)))
    score = min(25, 5*hits)
    if re.search(r'[$€£]\s?\d|\d+(?:\.\d+)?\s?(?:%|percent|billion|million|trillion|bn\b)', lower):
        score += 3
    if '"' in text or '“' in text or re.search(r'^[^\n]{2,60}:\s', text):
        score += 4  # a direct quote or "Speaker: statement" invites reactions
    if re.search(FILLER, lower):
        score -= 25
    return score


def trend_boost(text, terms):
    """Matches against Google Trends searches; traffic-weighted and capped."""
    if not terms:
        return 0.0
    lower = text.casefold()
    best = 0
    for term, traffic in terms.items():
        words = [w for w in re.findall(r'\w+', term) if len(w) > 2]
        if words and all(re.search(r'\b'+re.escape(w)+r'\b', lower) for w in words):
            best = max(best, traffic)
    if not best:
        return 0.0
    # 1K searches -> ~7, 100K -> ~12, 1M+ -> 15.
    return round(min(15.0, 6+3*math.log10(best/1000+1)), 2)


def fatigue(names, recent_posts):
    """Penalty when the same people/places were posted in the last few hours."""
    penalty = 0
    for post_names, hours_ago in recent_posts:
        overlap = len(set(names) & set(post_names))
        if overlap >= 2 and hours_ago < 3:
            penalty = max(penalty, 18 if hours_ago < 1 else 10)
        elif overlap >= 1 and hours_ago < 1:
            penalty = max(penalty, 5)
    return penalty


def score(text, m, *, category_hint=None, priority=1.0, media=False, trend_terms=None,
          recent_posts=(), names=(), weights=None, state_media_only=False):
    category = categorise(text, category_hint)
    parts = dict(
        heat=heat(m, trend_boost(text, trend_terms)),
        interest=interest(text, category, weights),
        talkability=talkability(text),
        freshness=freshness(m['age_hours']),
        media=4 if media else 0,
        fatigue=-fatigue(names, recent_posts),
    )
    # Ranking is about engagement potential only: the outlet's reputation or
    # priority does not add or subtract points (`priority`/`state_media_only`
    # are accepted for compatibility but deliberately unused).
    total = sum(parts.values())
    # Crossover topics must be genuinely big to get through.
    if category in ('business', 'tech', 'crypto', 'other') and parts['heat'] < 20 and parts['interest'] < 18:
        total -= 15
    return round(total, 2), category, parts


def breaking(total, m, category):
    """Fast lane: big, fast-moving, on-topic stories skip the normal posting gap."""
    if m['age_hours'] >= 2:
        return False
    if category in ('politics', 'geopolitics'):
        return total >= 85 and (m['outlets'] >= 4 or m['outlets_last_hour'] >= 3)
    # Crossover stories only when everyone is covering them at once.
    return total >= 100 and m['outlets'] >= 4 and m['outlets_last_hour'] >= 3
