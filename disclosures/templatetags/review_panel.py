"""검수 화면(admin 원문 대조 패널) 전용 템플릿 태그.

## 왜 템플릿 태그인가

원문 안에서 "경고가 지목한 수치"를 찾아 하이라이트하려면 텍스트를 잘라 마크업을
끼워 넣어야 하는데, 순수 템플릿 필터로는 할 수 없다. 검수자가 수만 자짜리 원문에서
문제의 숫자를 눈으로 찾는 것이 지금 검수 큐가 소진되지 않는 이유 중 하나다.

## XSS

`raw_content`는 DART에서 받아온 **외부 데이터**다. 반드시 원문(이스케이프 전)에서
위치를 찾은 뒤, 잘라낸 조각을 각각 `escape()` 하고 마크업만 안전한 문자열로 붙인다.
이스케이프된 문자열에서 검색하면 `&`·`<`가 엔티티로 바뀌어 위치가 어긋나므로
순서를 뒤집으면 안 된다.
"""
import re

from django import template
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

register = template.Library()

#: 수치 표기 사이에서 무시할 구분자. `9,891`과 `9891`, `3조 9,891억`과 `3조9,891억`을
#: 같은 값으로 보기 위한 것이다. 요약 본문과 원문(표에서 추출)은 자릿수 쉼표와 공백
#: 처리가 자주 다르므로, 이 정도의 관용이 없으면 하이라이트가 거의 걸리지 않는다.
_SEPARATOR = r'[\s,]*'

#: 구분자만으로 이루어진 항목은 빈 문자열에 매칭되어 무한 분할을 일으키므로 걸러낸다.
_HAS_CONTENT = re.compile(r'[^\s,]')

#: evidence 항목의 `field` 값 → 화면 라벨. 모델 필드명을 그대로 보여주면
#: 검수자가 어느 문단의 근거인지 즉시 알기 어렵다.
_FIELD_LABELS = {
    'one_line': '한 줄 요약',
    'easy_explanation': '쉬운 설명',
    'why_important': '왜 중요한가',
}


def _term_pattern(term):
    """수치 표기 하나를 관용적 정규식으로 바꾼다.

    쉼표·공백은 있어도 없어도 같은 것으로 보고(숫자 사이도 마찬가지), 나머지 문자는
    그대로 요구한다. 단위(`조`·`억`·`%`)까지 느슨하게 풀면 엉뚱한 곳이 걸린다.
    """
    chars = list(term.strip())
    parts = []
    for index, char in enumerate(chars):
        if char.isspace() or char == ',':
            if not parts or parts[-1] != _SEPARATOR:
                parts.append(_SEPARATOR)
            continue
        if (
            parts and parts[-1] != _SEPARATOR
            and char.isdigit() and chars[index - 1].isdigit()
        ):
            parts.append(_SEPARATOR)
        parts.append(re.escape(char))
    return ''.join(parts)


def _clean_terms(terms):
    """중복·빈 항목을 제거한 검색어 목록."""
    cleaned = []
    for term in (terms or []):
        text = str(term).strip()
        if text and _HAS_CONTENT.search(text) and text not in cleaned:
            cleaned.append(text)
    return cleaned


@register.simple_tag
def highlight_terms(text, terms, prefix='hl'):
    """`terms`를 `text`에서 찾아 `<mark>`로 감싼 HTML과 적중 정보를 돌려준다.

    반환: `{'html': 안전한 HTML, 'hits': [{term, count, anchor, found, index}],
             'total': 적중 총 횟수, 'missing': 원문에서 못 찾은 표기 목록}`

    `missing`이 검수의 핵심 신호다 — 요약에 있는 수치가 원문에 문자열로 없다는 뜻이며,
    실제로 39조 8,905억을 3조 9,891억으로 잘못 적은 사례가 이렇게 드러난다.
    (표기 방식이 달라 못 찾는 경우도 있으므로 '오류 확정'이 아니라 '먼저 볼 곳'이다.)
    """
    source = text or ''
    cleaned = _clean_terms(terms)
    hits = [
        {'term': term, 'count': 0, 'anchor': '', 'found': False, 'index': index}
        for index, term in enumerate(cleaned)
    ]
    if not source or not cleaned:
        return {
            'html': escape(source), 'hits': hits, 'total': 0,
            'missing': [hit['term'] for hit in hits],
        }

    # 긴 표기를 먼저 시도해야 `3조 9,891억`이 `891`에 먼저 먹히지 않는다.
    order = sorted(range(len(cleaned)), key=lambda i: len(cleaned[i]), reverse=True)
    pattern = re.compile('|'.join(
        f'(?P<t{position}>{_term_pattern(cleaned[i])})'
        for position, i in enumerate(order)
    ))

    pieces = []
    cursor = 0
    total = 0
    for match in pattern.finditer(source):
        if match.start() == match.end():  # 방어: 빈 매칭은 무시(무한 분할 방지)
            continue
        hit = hits[order[int(match.lastgroup[1:])]]
        hit['count'] += 1
        anchor = f"{prefix}-{hit['index']}-{hit['count']}"
        if not hit['anchor']:
            hit['anchor'] = anchor
            hit['found'] = True
        pieces.append(escape(source[cursor:match.start()]))
        pieces.append(format_html(
            '<mark class="rp-hit" id="{}" data-term="{}">{}</mark>',
            anchor, hit['term'], match.group(),
        ))
        cursor = match.end()
        total += 1
    pieces.append(escape(source[cursor:]))

    return {
        'html': mark_safe(''.join(pieces)),
        'hits': hits,
        'total': total,
        'missing': [hit['term'] for hit in hits if not hit['found']],
    }


@register.filter
def evidence_field_label(value):
    """evidence 항목의 `field` 값을 사람이 읽는 라벨로 바꾼다."""
    return _FIELD_LABELS.get(value, value or '요약')


@register.filter
def has_key(value, key):
    """dict에 키가 실제로 있는지. 값의 참/거짓과 '키 자체가 없음'을 구분하기 위한 것.

    `quote_found`가 없는 옛 근거를 "인용 미발견"으로 잘못 표시하지 않으려면 필요하다.

    dict가 아니면 무조건 False다. `in` 을 그냥 쓰면 문자열이 들어왔을 때 부분 문자열
    검사가 되어 `'quote_found 를 담은 문자열'|has_key:'quote_found'` 가 True가 된다 —
    "키가 있는가"라는 물음에 답할 수 없는 입력이므로 False가 맞다.
    """
    return isinstance(value, dict) and key in value
