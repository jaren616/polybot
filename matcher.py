"""Cheap first-pass filter: which markets could this headline possibly be about?

Uses weighted keyword overlap. Capitalised words (names, places, organisations)
count more than ordinary words, and rare words count more than common ones.
Only the best few candidates go on to the (slower, paid) LLM.
"""
import math
import re
from collections import Counter

import config

STOP = set("""a an the and or of to in on for at by with from as is are was were be been
will would could should may might can do does did not no yes this that these those it its
he she they them his her their we our you your i me my who what when where which why how
than then there here over under after before about into out up down off more most less
new says said say per vs via amid just also only all any some each other such own same
s t re ve ll d m 2025 2026 2027 2028 by end before after
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday week month year today
win wins won next first last top people officials state party""".split())

WORD = re.compile(r"[A-Za-z][A-Za-z0-9'\-]+")


def _tokens(text: str) -> tuple[set[str], set[str]]:
    words, caps = set(), set()
    for w in WORD.findall(text):
        lw = re.sub(r"'s$", "", w.lower()).strip("'-")
        if lw in STOP or len(lw) < 3:
            continue
        words.add(lw)
        if w[0].isupper():
            caps.add(lw)
    return words, caps


class Matcher:
    def __init__(self, markets):
        self.markets = markets
        self.market_tokens = [_tokens(m.question) for m in markets]
        df = Counter()
        for words, _ in self.market_tokens:
            df.update(words)
        n = max(len(markets), 1)
        self.idf = {w: math.log(1 + n / c) for w, c in df.items()}

    def candidates(self, text: str):
        n_words, n_caps = _tokens(text)
        scored = []
        for mk, (m_words, m_caps) in zip(self.markets, self.market_tokens):
            shared = n_words & m_words
            # need 2+ shared words, at least one a name (capitalised in both)
            if len(shared) < config.MIN_SHARED_WORDS or not (shared & m_caps & n_caps):
                continue
            score = sum(self.idf.get(w, 1.0) * (2.0 if w in m_caps and w in n_caps else 1.0)
                        for w in shared)
            if score >= config.MIN_MATCH_SCORE:
                scored.append((score, mk))
        scored.sort(key=lambda x: -x[0])
        return scored[: config.MAX_CANDIDATES_PER_ITEM]
