"""Offline Chinese phrase boundaries for display wrapping, never cue timing."""
from functools import lru_cache
import logging
import re

PRODUCT_NOUNS = ('洗脸巾', '棉柔巾', '湿厕纸', '卫生巾', '安睡裤', '萌睡裤',
                 '拉拉裤', '抽纸', '卷纸', '纸巾', '毛巾', '浴巾', '联名款')
_LEXICON = {**{word: 'n' for word in PRODUCT_NOUNS}, '心相印': 'nz', '七度空间': 'nz',
            '云感': 'n', '亲肤': 'a', '不掉絮': 'v', '不易掉絮': 'v', '不容易掉絮': 'v',
            '不连抽': 'v', '不反渗': 'v', '用起来': 'v', '擦完脸': 'v', '足足': 'd',
            **{word: 'c' for word in ('关键是', '重点是', '问题是', '重要的是', '更重要的是', '最好是')}}
_PREDICATES = ('真的', '简直', '确实', '特别', '非常', '用起来', '也太', '还能',
               '关键', '而且', '但是', '不过', '所以', '因为', '不仅', '还', '又', '也', '并且')
_PREFIXES = {'的', '得', '不', '没', '别', '很', '更', '最', '太', '也', '还',
             '又', '把', '被', '在', '向', '给', '对', '从', '和', '与', '跟', '及', '或', '用起来',
             '关键是', '重点是', '问题是', '重要的是', '更重要的是', '最好是'}
_SUFFIXES = {'的', '了', '着', '过', '啊', '呀', '呢', '吗', '吧', '哇', '啦'}
_CLOSING = '，。！？、；：,.!?;:）】》」』”’%％'
_OPENING = '（【《「『“‘'


@lru_cache(maxsize=1)
def _tagger():
    import jieba
    import jieba.posseg
    logging.getLogger('jieba').setLevel(logging.WARNING)
    tokenizer = jieba.Tokenizer()
    for word, tag in _LEXICON.items():
        tokenizer.add_word(word, freq=1_000_000, tag=tag)
    return jieba.posseg.POSTokenizer(tokenizer)


def _occurrences(text, terms):
    for term in terms:
        if not isinstance(term, str) or not term:
            continue
        start = 0
        while (start := text.find(term, start)) >= 0:
            yield start, start + len(term)
            start += 1


@lru_cache(maxsize=512)
def analyze_phrases(text, protected_terms=()):
    """Return word/phrase facts; brand names can be supplied from task metadata."""
    tokens, offset = [], 0
    for token in _tagger().cut(text):
        end = offset + len(token.word)
        tokens.append((offset, end, token.word, token.flag))
        offset = end
    if offset != len(text) or ''.join(t[2] for t in tokens) != text:
        raise ValueError('SUBTITLE_TOKENIZATION_CHANGED_TEXT')
    # Quoted names, capitalized foreign names, numeric specifications and
    # modifier+product names stay together whenever the unit fits a line.
    spans = list(_occurrences(text, (*_LEXICON, *protected_terms)))
    patterns = (
        r'[“「『《][^”」』》\n]+[”」』》]',
        r'[A-Z][A-Za-z0-9]*(?:[ -]+[A-Z][A-Za-z0-9]*)+',
        r'(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+)\s*'
        r'(?:倍速|倍|毫升|厘米|毫米|毫克|公斤|千克|抽|包|层|片|张|克|秒|分钟|ml|mL|cm|mm|kg|g|%|％)'
        r'(?:加厚|加大|加长|加宽)?',
        r'(?:新品|新款|升级款)([^，。！？、；：\s的]{2,16}?(?:' + '|'.join(PRODUCT_NOUNS) + r'))',
        r'(?:这种|那种|这款|那款)([^，。！？、；：\n]{1,18}?的[^，。！？、；：\n]{0,6}?(?:'
        + '|'.join(PRODUCT_NOUNS) + r'))',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            spans.append(match.span(1) if match.lastindex else match.span())
    return tuple(tokens), tuple(sorted(set(spans)))


def boundary_preferences(text, measure, limit, protected_terms=()):
    tokens, spans = analyze_phrases(text, protected_terms)
    words = {(lo, hi) for lo, hi, _, _ in tokens if hi - lo > 1}
    # Hard word/name protection applies only if that whole unit fits. An
    # oversized URL/word still has an explicit grapheme fallback.
    protected = [span for span in {*words, *spans} if measure(text[slice(*span)]) <= limit]
    ends = {hi: (word, tag) for _, hi, word, tag in tokens}
    starts = {lo: (word, tag) for lo, _, word, tag in tokens}
    # Adjacent complete statements may have had their punctuation stripped by
    # the caption writer: "没有刺鼻香味 / 宝宝也能放心用". Keep the new
    # subject with its modal predicate instead of appending it to the old one.
    clause_subjects = set()
    for i, (lo, hi, word, tag) in enumerate(tokens[:-2]):
        if (tag.startswith(('n', 'r', 'v')) and tokens[i + 1][2] in {'也', '都', '还'}
                and tokens[i + 2][3].startswith(('v', 'a', 'd'))):
            # Keep an action with its result: "按压也不反渗" / "擦完脸也不掉絮".
            start = i
            if tag.startswith('v'):
                while start and tokens[start - 1][3].startswith(('d', 'z', 'm')):
                    start -= 1
            clause_subjects.add(tokens[start][0])
    clause_subjects.update(lo for i, (lo, hi, word, tag) in enumerate(tokens[:-1])
        if tag.startswith('m') and word.endswith(('箱', '包', '提', '份', '组'))
        and tokens[i + 1][2] in {'足足', '有', '能', '可以'})
    result = {len(text): (0, 0)}
    for at in range(1, len(text)):
        left, right = text[at - 1], text[at]
        hard = int(any(lo < at < hi for lo, hi in protected)
                   or left in _OPENING or right in _CLOSING)
        lw, lt = ends.get(at, ('', ''))
        rw, rt = starts.get(at, ('', ''))
        score = 30
        if left in '，。！？；：,.!?;:':
            score = 0
        elif at in clause_subjects:
            score = 1
        elif text[at:].startswith(_PREDICATES):
            score = 2
        elif lt.startswith(('n', 'r')) and rt.startswith(('v', 'a', 'd')):
            score = 6  # subject -> predicate / effect phrase
        elif lw.isspace() or rw.isspace():
            score = 10
        elif lt.startswith('n') and rt.startswith('n'):
            score = 60  # stay inside a noun phrase only when necessary
        if (lw in _PREFIXES or rw in _SUFFIXES or lt in {'p', 'c', 'u', 'uj', 'ud'}
                or lt == 'd' or lt.startswith('v') and rt.startswith(('n', 'm', 'q'))):
            score += 250
        if right.isspace():
            score += 100
        result[at] = (hard, score)
    return result
