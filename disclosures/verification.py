"""요약 검증 모듈 — 원문 대조·경고 생성.

`summarizer.py`에 섞여 있던 검증 로직을 그대로 옮겨 온 것이다(동작 변경 없음).
분리 이유는 소유권이다. 프롬프트·스키마·LLM 클라이언트와 검증 규칙은 고치는 사람도
고치는 이유도 다른데 한 파일에 있으면 서로의 변경을 밟는다.

이 모듈은 **LLM을 호출하지 않는다.** 순수하게 문자열 대조만 하므로 비용이 0이고,
`revalidate_summaries` 로 기존 요약 전체에 몇 번이든 소급 적용할 수 있다.

`summarizer.py`가 이 모듈의 공개 이름을 전부 재수출하므로
`from .summarizer import validate_summary` 같은 기존 import 경로도 그대로 동작한다.
"""
import re

# ---------------------------------------------------------------------------
# 길이·문장 수 제약 (요약 본문의 형식 규칙)
# ---------------------------------------------------------------------------

#: 길이 제약. DisclosureSummary.one_line 의 max_length=200 과 일치해야 한다.
ONE_LINE_MAX_CHARS = 200
EXPLANATION_MIN_SENTENCES = 3
EXPLANATION_MAX_SENTENCES = 5


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r'[.!?]+(?=\s|$)')
_WHITESPACE = re.compile(r'\s+')

#: 금액·비율·주식수·날짜 등 대조 대상 수치. 뒤에 붙은 한국어 단위(조·억·만·천)까지 잡는다.
#: 앞뒤가 영문자면 식별자(예: 'P&T7', 'Fab2')의 일부이므로 대조 대상에서 뺀다.
_NUMBER = re.compile(r'(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)\s*([조억만천])?(?![A-Za-z])')

#: 한국어 수 단위 배수.
_UNIT_MULTIPLIER = {'조': 10 ** 12, '억': 10 ** 8, '만': 10 ** 4, '천': 10 ** 3}

#: 근사 표현. 있으면 단위 환산 오차를 허용한다.
_APPROX = re.compile(r'약|가량|여|안팎|상당')

#: 근사 표현이 있을 때 허용하는 상대 오차.
APPROX_TOLERANCE = 0.05

#: 연도 표기(1900~2099). 금액이 아니므로 대조 대상에서 뺀다.
_YEAR = re.compile(r'^(19|20)\d{2}$')

#: 이 값 이하의 맨 정수는 항목번호·순번·일자로 본다(`1. 거래상대방`, `제3자배정`, `5월`).
#: 실측: 경고를 유발한 수치 상위 3개가 `2026`(78회)·`1`(60회)·`2025`(38회)였다.
ORDINAL_MAX = 31

#: 표 머리글의 단위 표기(`(단위 : 천원)`, `(단위 : 백만 원)`)로 생기는 자릿수 차이.
#: 머리글을 파싱하는 대신 배수 일치를 허용한다 — 머리글은 인용문 밖에 있는 경우가 많아
#: 파싱해도 인용문만으로는 복원되지 않는다.
#:
#: **10^12(조)는 여기서 뺐다.** 무조건 허용하면 인용문의 **한 자리 숫자 하나가 조 단위
#: 금액 아무거나를 정당화한다** — `4` 가 `4조` 의 근거로 인정된다. 실제로 그 경로로
#: 10배 오류가 검증을 통과해 웹에 게시됐다(2026-08-19, rcept_no 20260710000008:
#: 40,023,070,290,000원을 `약 4조 원`으로 적었는데 경고가 붙지 않았다).
#: 배수가 클수록 이 구멍이 커지므로 가장 큰 배수부터 닫는다.
#:
#: 조 단위 표는 여전히 통과한다 — 문서가 `(단위 : 조원)` 을 선언했을 때만
#: `document_scales` 가 10^12 을 후보에 다시 넣는다(아래 `_value_supported` 의 가산 경로).
#: 실측(140건): 이 게이트로 새로 생긴 오탐 0건, 놓치던 10배 오류 1건 검출.
#:
#: **2026-08-23: 나머지도 전부 닫았다. 무조건 허용하는 배수는 이제 없다.**
#: 이 목록의 존재 이유는 "LLM이 단위를 환산해 써서 자릿수가 어긋난 것을 용서한다"였다.
#: v3 프롬프트가 환산을 금지하고 코드가 대신 하면서(`annotate_amounts`) 그 전제가 사라졌다.
#: 남은 것은 구멍뿐이었다 — 10^3~10^8 을 무조건 허용하면 **천 배 틀려도 통과**한다.
#:
#: 전량 v3 재요약(2026-08-23) 후 140건 전수 재측정:
#:   요약 수치 650개 중 인용문과 직접 일치 629 / 배수 경로 의존 21
#:   전면 좁히기 결과 — 차단 0건(변화 없음), 인용 누락 수치 16 → 17 (**+1**)
#: PR #11 시점에는 같은 좁히기가 새 오탐 25건을 냈다. 그 25건이 전부 v2 잔재였다는 뜻이다.
#:
#: 선언된 배수는 그대로 살아 있다 — `declared_scales`(인용문)와 `document_scales`(문서)가
#: `_value_supported` 의 가산 경로로 넣는다. 즉 `(단위 : 백만원)` 을 실제로 선언한 표는
#: 통과하고, 아무 근거 없이 자릿수만 맞는 값은 통과하지 못한다.
_SCALE_MULTIPLIERS = ()

#: **원문 전체 대조에서만** 쓰는 배수. 인용문 대조와 일부러 다르게 둔다.
#:
#: 두 대조는 결정하는 것이 다르다.
#:   - 인용문 대조 → **경고를 붙일지**. 틀렸을 때의 대가가 "검수 목록에 한 줄 더"라
#:     좁게 잡아도 손해가 작다. 그래서 선언된 배수만 인정한다.
#:   - 원문 대조 → **게시를 막을지**. 틀렸을 때의 대가가 "정확한 요약이 웹에서 사라짐"이라
#:     크다. 그래서 넓게 잡는다.
#:
#: 이 비대칭이 없으면 좁히기가 과잉 차단으로 돌아온다. 예: `736`(백만원 표)을 `약 7억`으로
#: 쓴 **정확한** 요약(id 38)은 좁힌 인용문 대조를 통과하지 못한다. 원문 대조까지 좁히면
#: 그 요약이 통째로 내려간다 — 값이 맞는데도. 넓은 원문 대조가 그것을 게시 쪽으로 돌린다.
#:
#: 넓혀도 무력하지 않다: 10배·100배는 여기 없으므로 실제 오류는 그대로 막힌다.
_DOCUMENT_SCALE_MULTIPLIERS = (10 ** 3, 10 ** 4, 10 ** 6, 10 ** 8)

#: 배수 일치로 인정할 때의 상대 오차. 반올림 표기(3,746억 ↔ 374,629백만) 흡수용.
SCALE_TOLERANCE = 0.01

#: 인용문을 조각으로 나누는 구분자: 줄바꿈·표 구분자·생략부호.
_FRAGMENT_SPLIT = re.compile(r'\n|\||\.{3}|…')

#: 조각 검증에 쓸 최소 길이(서식 문자 제거 후). 이보다 짧으면 라벨·구분자라 판정에 무의미하다.
MIN_FRAGMENT_CHARS = 4

#: 서식 문자를 모두 제거해 '내용'만 남기는 정규화. 표 구분자·공백·괄호 유무 차이를 흡수한다.
_CONTENT_ONLY = re.compile(r'[^0-9A-Za-z가-힣.%\-]')


# ---------------------------------------------------------------------------
# 표 머리글의 단위 선언 (`(단위 : 백만원)`)
# ---------------------------------------------------------------------------
# analyst 조사(_workspace/10_analyst_number_triage.md 2장)에서 raw_content 141건 전수로
# 검증한 패턴이다. 금액 선언 414건이 모두 정상 분류되고 오검출 0건이었다.

#: 단위 선언. **콜론(반각/전각)을 필수**로 두는 것이 핵심이다 — 이것이 `단위당 원가`,
#: `3년 단위 주주환원정책` 같은 산문을 걸러내는 유일한 신호다.
#: 앞에 한글이 붙은 `통화단위`는 lookbehind로 제외한다.
_UNIT_DECL = re.compile(r'(?<![가-힣])단위\s*[:：]\s*([^)\]\n|]{1,40})')

#: 선언 본문을 쪼갠 뒤 금액 토큰만 고른다. 공백을 지운 뒤 매칭해야 `백만 원`을 잡는다.
#: `원`으로 끝나야 하므로 `조 달러`·`백만달러`·`USD`는 자동으로 배제된다(W5).
_MONEY_TOKEN = re.compile(r'^(조|십억|억|백만|천만|만|천|)원$')

#: 금액 단위 토큰 → 배수. `십억`(10^9)이 실제로 등장한다(id 37 유형자산 취득 표).
_DECL_SCALE = {
    '': 1, '천': 10 ** 3, '만': 10 ** 4, '백만': 10 ** 6,
    '천만': 10 ** 7, '억': 10 ** 8, '십억': 10 ** 9, '조': 10 ** 12,
}

#: 선언 본문 안에서 여러 단위를 나열할 때 쓰는 구분자(`주, 원` · `백만원, %`).
_DECL_SPLIT = re.compile(r'[,·ㆍ/]')


def unit_declarations(text):
    """원문에서 단위 선언을 찾아 [(위치, 배수)] 로 반환한다. 위치 오름차순.

    배수는 금액 선언이면 10의 거듭제곱, **비금액 선언이면 1**이다.
    비금액(`시간`·`%`·`주`·`명`·`USD`)을 버리지 않고 배수 1로 담는 것이 중요하다 —
    버리면 앞 표의 배수가 뒤 표로 새기 때문이다(analyst W1).
    실제로 id 37은 `(단위: 백만원)` 다음에 `(단위: 시간)`이 오고 그 뒤 값들은
    백만원이 아니다.

    한 선언이 여러 단위를 나열하면(`(단위 : 주, 원)`) **금액 토큰 중 가장 큰 배수**를 쓴다.
    나열은 열마다 단위가 다르다는 뜻인데 전처리된 텍스트에서 열 경계를 복원할 수 없으므로
    (analyst W3), 여기서 고른 배수는 확정이 아니라 대조 후보로만 쓰여야 한다.
    """
    declarations = []
    for match in _UNIT_DECL.finditer(text or ''):
        scales = []
        for token in _DECL_SPLIT.split(match.group(1)):
            token = _WHITESPACE.sub('', token)
            money = _MONEY_TOKEN.match(token)
            if money:
                scales.append(_DECL_SCALE[money.group(1)])
        declarations.append((match.start(), max(scales) if scales else 1))
    return declarations


def scale_at(declarations, offset):
    """주어진 위치에 적용되는 배수. 앞선 선언이 없으면 None.

    **그 위치 직전의 선언 하나**만 본다. 문서 전체에 한 배수를 적용하면 안 된다 —
    141건 중 30건(21%)이 한 문서에서 금액 단위를 2종 이상 쓰고, id 37은 11개 선언이
    백만원과 십억원을 오간다(1,000배 차이). 선언보다 **앞**에 있는 수치에는 적용하지
    않는다(소급 금지, analyst 규칙 4).

    None(선언 없음)과 1(비금액 선언으로 리셋됨)은 다른 뜻이므로 구분해서 돌려준다.
    """
    found = None
    for position, scale in declarations:
        if position > offset:
            break
        found = scale
    return found


def _normalize(text):
    """원문 대조용 정규화. 공백과 표 셀 구분자를 흡수한다.

    전처리된 원문은 표를 `합 계 | 80,948 | | 17,236,792,300` 형태로 담는다(backend 전처리).
    모델은 이를 `합 계 80,948` 처럼 구분자 없이 인용하는 경우가 많아, 구분자를 그대로 두면
    실제로는 정확한 인용인데도 quote_found=False 가 된다.
    구분자는 지우지 않고 **공백으로 치환**한다. 통째로 지우면 빈 셀을 사이에 둔 숫자들이
    `80,94817,236,792,300` 으로 붙어 버려 수치 대조가 더 나빠진다.
    """
    return _WHITESPACE.sub(' ', (text or '').replace('|', ' ')).strip()


def extract_numbers(text):
    """텍스트에서 대조 대상 수치를 뽑아 [(표기, 값)] 로 반환한다.

    '7조 931억' 처럼 단위가 이어지는 복합 표기는 하나의 값으로 합산한다.
    요약에서 '7,093,100,000,000원'을 '약 7조 931억 원'으로 바꿔 쓰는 것은 지침이 요구하는
    정상 동작이므로, 자릿수 문자열이 아니라 **값**으로 대조해야 오탐이 나지 않는다.
    """
    matches = list(_NUMBER.finditer(text or ''))
    results = []
    idx = 0
    while idx < len(matches):
        match = matches[idx]
        raw = match.group(1).replace(',', '')
        unit = match.group(2)
        try:
            value = float(raw)
        except ValueError:
            idx += 1
            continue
        if unit is None:
            results.append((match.group(0).strip(), value))
            idx += 1
            continue

        # 단위가 붙었으면 뒤따르는 하위 단위를 합산한다. 예: '7조 931억' → 7.0931e12
        total = value * _UNIT_MULTIPLIER[unit]
        last_multiplier = _UNIT_MULTIPLIER[unit]
        end = idx
        for nxt in range(idx + 1, len(matches)):
            gap = (text[matches[nxt - 1].end():matches[nxt].start()] or '').strip()
            nxt_unit = matches[nxt].group(2)
            if gap:
                break
            try:
                nxt_value = float(matches[nxt].group(1).replace(',', ''))
            except ValueError:
                break
            if nxt_unit is None:
                # 맨 끝의 무단위 조각은 **일의 자리 나머지**다.
                # `341억 8,587만 4,800`의 `4,800`이 그것이고, 이걸 흡수하지 않으면
                # 별개의 수치 `4,800`으로 튀어나와 "인용 근거 없는 수치" 오탐이 된다
                # (실측: 이 한 가지 원인으로 요약 4건에 경고가 붙어 있었다 — id 13·56·73·131).
                #
                # 직전 단위보다 **작을 때만** 흡수한다. 크거나 같으면 나머지가 아니라
                # 뒤따르는 다른 수치이므로 별개로 남겨야 한다.
                if last_multiplier > 1 and nxt_value < last_multiplier:
                    total += nxt_value
                    end = nxt
                break
            if _UNIT_MULTIPLIER[nxt_unit] >= last_multiplier:
                break
            total += nxt_value * _UNIT_MULTIPLIER[nxt_unit]
            last_multiplier = _UNIT_MULTIPLIER[nxt_unit]
            end = nxt
        results.append((text[match.start():matches[end].end()].strip(), total))
        idx = end + 1
    return results


def is_reference_number(text, value):
    """대조에서 제외할 '수치 아닌 숫자'(연도·항목번호·순번)인지 판정한다.

    금액·비율·주식수는 검증의 핵심이므로 절대 제외하지 않는다. 판정 기준은 보수적이다.
      - 한국어 단위(조·억·만·천)가 붙었으면 금액이다 → 제외하지 않는다
      - 쉼표나 소수점이 있으면 자릿수 구분·비율이다 → 제외하지 않는다
      - 그 외 맨 정수 중 연도(19xx·20xx)와 0~ORDINAL_MAX 만 제외한다
    """
    bare = (text or '').strip()
    if re.search(r'[조억만천]', bare):
        return False
    if ',' in bare or '.' in bare:
        return False
    if _YEAR.match(bare):
        return True
    return value == int(value) and 0 <= value <= ORDINAL_MAX


def extract_comparable_numbers(text):
    """extract_numbers 에서 연도·항목번호를 걸러낸 대조용 수치 목록."""
    return [
        (raw, value) for raw, value in extract_numbers(text)
        if not is_reference_number(raw, value)
    ]


def declared_scales(quotes):
    """인용문들이 **스스로 담고 있는** 단위 선언의 배수 집합.

    프롬프트 v3 §2가 "머리글에 단위가 선언돼 있으면 그 머리글 구절을 quote 에 함께 담아라"를
    요구하면서 열린 경로다. 그 전까지는 머리글이 인용문 **밖**에 있어 인용문만으로는 배수를
    복원할 수 없었고(`_SCALE_MULTIPLIERS` 주석의 전제), 원문 좌표로 찾으려 하면 원문과
    `_content_only` 의 좌표계가 어긋나는 문제가 있었다. 인용문이 선언을 품고 있으면
    **좌표 계산 자체가 필요 없다.**

    v2로 만든 기존 요약에는 선언이 담겨 있지 않으므로 빈 집합이 나온다(소급 없음).
    """
    scales = set()
    for quote in quotes:
        for _position, scale in unit_declarations(_normalize(quote or '')):
            if scale > 1:
                scales.add(scale)
    return scales


def document_scales(raw_text):
    """원문 **전체**가 선언한 단위 배수의 집합.

    `declared_scales` 는 인용문이 스스로 품은 선언만 본다. 그 규칙은 v3 프롬프트가
    "머리글을 quote 에 함께 담아라"를 요구하면서 생겼고, v2로 만든 기존 요약에는
    선언이 담겨 있지 않아 빈 집합이 나온다.

    조 단위 표(`(단위 : 조원)`)는 이 프로젝트에서 실제로 쓰인다 — 삼성전자 잠정실적
    공시가 매출액을 `171.00` 으로만 적는다. 인용문 기준으로만 판정하면 그런 정상 요약이
    v2 잔재라는 이유만으로 오탐이 된다.

    그래서 **문서 단위**로 한 번 더 본다. 표 단위로 좁히는 것보다 느슨하지만,
    "아무 문서에나 10^12 을 허용"하던 이전 상태보다는 훨씬 좁다. 조를 선언한 문서는
    140건 중 6건뿐이고, 나머지 134건에서는 조 배수가 아예 후보에 오르지 않는다.
    """
    return {scale for _position, scale in unit_declarations(raw_text) if scale > 1}


def document_numbers(raw_text):
    """원문 **전체**에 등장하는 수치 값의 목록.

    인용문 대조가 실패했을 때 한 번 더 물어보기 위한 것이다 — "이 숫자가 원문 어딘가에는
    있는가". 인용문 대조보다 훨씬 느슨하므로 **게시를 막는 판정에는 쓰지 않는다.**
    쓰임새는 그 반대다: 인용만 빠졌을 뿐 값은 맞는 수치를 차단 대상에서 빼는 데 쓴다
    (`validate_summary` 의 uncited_numbers 참고).

    문서 전체를 훑지만 140건 전수에 0.2초로 비용이 문제되지 않는다.
    """
    return [value for _text, value in extract_numbers(_normalize(raw_text))]


def _value_supported(value, quote_values, approximate, quote_scales=()):
    """요약의 수치 value가 인용문의 수치들로 뒷받침되는지 판정한다.

    quote_scales: 인용문이 스스로 선언한 배수(`declared_scales`). **가산으로만 쓴다** —
    후보 배수를 늘리기만 하고 기존 후보를 줄이지 않는다. 그래서 이 인자로는 경고가
    사라질 수는 있어도 새로 생길 수는 없다.

    좁히기는 **가장 위험한 배수부터 부분적으로** 했다. 10^12 을 무조건 후보에 넣던 것을
    빼고, 문서가 조 단위를 선언했을 때만 `document_scales` 로 되돌려 넣는다(위 상수 주석).
    v3 요약 27건이 쌓인 뒤 140건 전체로 측정한 결과다 — 새 오탐 0건, 놓치던 10배 오류 1건 검출.

    **선언된 배수 하나만 허용하는 전면 좁히기는 아직 하지 않는다.** 같은 측정에서 경고가
    6건 → 31건으로 늘었고(새 오탐 25건), 그 25건은 대부분 v2로 만든 요약이 머리글을
    quote 에 담지 않아 생긴다. v2 잔재가 사라진 뒤 다시 측정할 것.
    """
    for qvalue in quote_values:
        if value == qvalue:
            return True
        # 단위 환산으로 자릿수 표기만 달라진 경우(7,093,100,000,000 ↔ 7조 931억)
        if qvalue and abs(value - qvalue) / abs(qvalue) <= (
            APPROX_TOLERANCE if approximate else 0.0
        ):
            return True
    # 표 머리글 단위(`(단위 : 천원)`)로 자릿수만 어긋난 경우.
    # 원문 표가 374,629(백만원)이고 요약이 '3조 7,462억'이면 값은 10^6 배 차이지만 옳은 환산이다.
    #
    # 근사 표현('약 7억')이 붙었으면 직접 대조와 **같은** 허용오차를 준다.
    # 직접 경로는 5%를 허용하는데 배수 경로만 1%를 요구하던 것이 비대칭이었고, 그 탓에
    # `736`(백만원)=7.36억을 '약 7억'으로 쓴 정확한 요약이 오차 4.89%로 경고를 맞았다(id 38).
    # 배수를 곱하는 것과 근사로 반올림하는 것은 서로 독립인 오차이므로 둘 다 인정해야 한다.
    # 진짜 오류는 10배·100배 어긋나므로 5%로 넓혀도 통과하지 못한다.
    tolerance = max(SCALE_TOLERANCE, APPROX_TOLERANCE) if approximate else SCALE_TOLERANCE
    multipliers = _SCALE_MULTIPLIERS + tuple(
        scale for scale in sorted(quote_scales) if scale not in _SCALE_MULTIPLIERS
    )
    for qvalue in quote_values:
        if not qvalue:
            continue
        for multiplier in multipliers:
            for left, right in ((value, qvalue * multiplier), (value * multiplier, qvalue)):
                if right and abs(left - right) / abs(right) <= tolerance:
                    return True
    return False


def _content_only(text):
    """서식 문자를 지우고 내용 문자만 남긴다. 표 구분자·공백 차이를 흡수하기 위한 정규화."""
    return _CONTENT_ONLY.sub('', text or '')


def quote_fragments(quote):
    """인용문을 대조 단위 조각으로 나눈다(서식 문자 제거 후, 짧은 조각은 버린다)."""
    return [
        fragment for fragment in (
            _content_only(part) for part in _FRAGMENT_SPLIT.split(quote or '')
        )
        if len(fragment) >= MIN_FRAGMENT_CHARS
    ]


def verify_quote(quote, raw_text=None, *, raw_content=None):
    """인용문이 원문에 근거하는지 판정한다. 판정 불가면 None.

    ## 왜 '통짜 일치'가 아니라 '조각 순서 일치'인가

    모델은 표에서 **떨어진 두 행을 하나의 인용문으로 이어 붙이는** 경우가 많다.
    예: `4. 개최방법 | 대면미팅` + `6. 주요 설명회내용(요약) | ...` (5번 항목을 건너뜀).
    각 조각은 원문에 정확히 존재하고 이어 붙인 문자열만 존재하지 않는다. 즉 할루시네이션이
    아니라 인용 형식 문제다. 통짜 일치로 보면 이런 정상 인용이 전부 경고로 뜬다
    (실측: 요약 140건 중 44건).

    그래서 인용문을 조각으로 나눠 **원문에 순서대로 등장하는지**만 본다.
    조각 하나라도 원문에 없으면 여전히 실패하므로 지어낸 인용은 그대로 걸린다.
    조각 순서를 요구하는 것도 의미가 있다 — 원문 순서를 뒤집어 인과를 왜곡한 인용은 잡힌다.

    프롬프트에는 "인용문은 연속된 한 구간이어야 한다"고 명시해 두었다(v3 §5).
    이 완화는 그 지시가 없던 시점에 생성된 요약을 구제하기 위한 것이기도 하다.
    """
    if raw_content is None:
        raw_content = _content_only(_normalize(raw_text))
    fragments = quote_fragments(quote)
    if not fragments:
        return None  # 판정할 만한 내용이 없는 짧은 인용
    position = 0
    for fragment in fragments:
        found = raw_content.find(fragment, position)
        if found < 0:
            return False
        position = found + len(fragment)
    return True


def count_sentences(text):
    """문장 수를 센다. '3.5%' 처럼 소수점 뒤에 공백이 없으면 분할되지 않는다."""
    stripped = (text or '').strip()
    if not stripped:
        return 0
    parts = [p for p in _SENTENCE_END.split(stripped) if p.strip()]
    return len(parts)


def verify_evidence(evidence, raw_text):
    """근거 항목을 원문과 대조한다. QA 숫자 대조의 기계적 1차 관문.

    각 항목에 두 개의 판정을 붙인다.
      quote_found  — quote 의 각 조각이 원문에 순서대로 존재하는가(verify_quote 참고).
                     False면 인용에 원문에 없는 내용이 섞인 것이다.
      numbers_ok   — claim 에 등장하는 수치가 **근거 전체**로 뒷받침되는가.
                     진단용 필드이며 경고를 만들지 않는다(아래 참고).
    반환: (판정이 붙은 evidence 리스트, 경고 문자열 리스트)

    ## numbers_ok 로는 경고를 만들지 않는 이유

    근거 하나가 주장 여러 개를 걸치는 일이 흔해서, 항목별 대조는 오탐이 대부분이다.
    "인용 없이 등장한 수치"는 validate_summary 의 집계 검사가 정확히 걸러내므로
    검수 게이트는 그쪽에 맡기고, 여기서는 판정 결과만 기록해 원인 추적에 쓴다.
    대조 범위를 자기 인용문이 아니라 **전체 인용문**으로 둔 것도 같은 이유다
    (실측: 자기 인용문 대조는 55건 오탐, 전체 대조는 13건).
    """
    raw_content = _content_only(_normalize(raw_text))
    all_quote_values = []
    for item in evidence:
        all_quote_values.extend(
            value for _, value in extract_numbers(_normalize(item.get('quote', '')))
        )
    quote_scales = declared_scales(
        item.get('quote', '') for item in evidence
    ) | document_scales(raw_text)

    checked = []
    warnings = []
    for idx, item in enumerate(evidence):
        claim = item.get('claim', '')
        verdict = verify_quote(item.get('quote', ''), raw_content=raw_content)
        approximate = bool(_APPROX.search(claim))
        missing = sorted({
            text for text, value in extract_comparable_numbers(claim)
            if not _value_supported(value, all_quote_values, approximate, quote_scales)
        })
        result = dict(item)
        # 판정 불가(None)는 실패로 취급하지 않는다. 경고를 만들지 못하는 근거일 뿐이다.
        result['quote_found'] = verdict is not False
        result['numbers_ok'] = not missing
        result['missing_numbers'] = missing
        checked.append(result)
        if verdict is False:
            warnings.append(f'evidence[{idx}]: 인용문이 원문에서 발견되지 않음')
    return checked, warnings


def validate_summary(data, raw_text):
    """파싱된 dict를 검증한다.

    하드 검증(실패 시 재시도): 필수 필드 존재, importance 값, one_line 길이,
    evidence 최소 1건. one_line 길이는 모델 필드 max_length=200 과 직결되므로
    초과하면 저장이 깨진다 — 반드시 하드 검증이다.
    소프트 검증(경고만 기록): 문장 수, 근거 대조 결과. QA가 판단할 재료로 넘긴다.
    """
    # summarizer 는 이 모듈을 import 해 재수출한다. 모듈 최상단에서 되받아 import 하면
    # 순환이 되므로, 예외 클래스는 호출 시점에 지연 import 한다.
    from .summarizer import SummaryValidationError

    for field in ('one_line', 'easy_explanation', 'why_important', 'importance'):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SummaryValidationError(f'필수 필드 누락 또는 빈 값: {field}')

    if data['importance'] not in ('high', 'medium', 'low'):
        raise SummaryValidationError(f"중요도 값이 잘못됨: {data['importance']!r}")

    one_line = data['one_line'].strip()
    if len(one_line) > ONE_LINE_MAX_CHARS:
        raise SummaryValidationError(
            f'one_line이 {len(one_line)}자로 상한 {ONE_LINE_MAX_CHARS}자를 초과'
        )

    evidence = data.get('evidence') or []
    if not isinstance(evidence, list) or not evidence:
        raise SummaryValidationError('evidence가 비어 있음 (원문 근거 필수)')

    checked, warnings = verify_evidence(evidence, raw_text)

    # QA 숫자 대조의 본 관문: 요약 본문에 등장한 모든 수치가 근거 인용문 전체로 뒷받침되는가.
    # 항목별 대조는 근거 하나가 주장 여러 개를 걸칠 때 오탐이 나지만, 이 집계 검사는
    # "인용 없이 등장한 수치"만 정확히 걸러낸다. 단위 환산 오차는 허용한다.
    # 연도·항목번호는 금액이 아니므로 대조에서 뺀다(is_reference_number 참고).
    # 인용문은 verify_evidence 와 똑같이 정규화한 뒤 수치를 뽑는다.
    # 표 구분자를 지우지 않으면 `1,234|567` 같은 셀 경계에서 값이 다르게 읽힌다.
    quote_values = []
    for item in evidence:
        quote_values.extend(
            value for _, value in extract_numbers(_normalize(item.get('quote', '')))
        )
    summary_text = ' '.join(
        data[f] for f in ('one_line', 'easy_explanation', 'why_important')
    )
    quote_scales = declared_scales(
        item.get('quote', '') for item in evidence
    ) | document_scales(raw_text)
    #
    # 인용 대조에 실패한 수치는 **두 종류로 갈라야 한다.** 하나로 묶어 전부 게시를 막던 것이
    # 실제 손해를 냈다 — v3 재요약(2026-08-23) 직후 자동 미게시 10건 중 8건이
    # **값은 원문과 정확히 일치하는데** 모델이 인용을 안 붙였다는 이유로 내려가 있었다.
    # 경고 대상 수치 16개를 전수 확인한 결과 16개 모두 원문에 존재했다.
    #
    #   - 원문에도 없다      → 지어냈을 수 있다. **사실 오류**이므로 계속 막는다.
    #   - 원문에는 있다      → 값은 맞고 근거만 빠졌다. 검수는 요청하되 **게시는 한다.**
    #
    # 문서 전체 대조는 느슨하지만 무력하지 않다. 실제로 웹에 떠 있던 오류들을 그대로 막는다
    # (rcept_no 20260715800045 원문 기준 실측: 10배 오류 `3조 9,891억` 차단,
    #  100배 오류 차단, pk 117 유형 `4조` 차단, 정답 `39조 8,905억`만 통과).
    # 10배·100배는 _SCALE_MULTIPLIERS 에 없는 배수라 문서 어느 값으로도 정당화되지 않는다.
    uncited = []
    unsupported = []
    document_values = document_numbers(raw_text)
    for text in sorted({
        text for text, value in extract_comparable_numbers(summary_text)
        if not _value_supported(value, quote_values, True, quote_scales)
    }):
        value = next(
            v for t, v in extract_comparable_numbers(summary_text) if t == text
        )
        if _value_supported(
            value, document_values, True,
            set(quote_scales) | set(_DOCUMENT_SCALE_MULTIPLIERS),
        ):
            uncited.append(text)
        else:
            unsupported.append(text)
    if unsupported:
        warnings.append(
            f'요약 본문의 수치 {", ".join(unsupported)}에 대응하는 원문 근거가 없음'
        )
    if uncited:
        warnings.append(
            f'요약 본문의 수치 {", ".join(uncited)}가 인용 근거에 없음 (원문에는 있음)'
        )

    sentences = count_sentences(data['easy_explanation'])
    if not EXPLANATION_MIN_SENTENCES <= sentences <= EXPLANATION_MAX_SENTENCES:
        warnings.append(
            f'easy_explanation이 {sentences}문장 '
            f'(권장 {EXPLANATION_MIN_SENTENCES}~{EXPLANATION_MAX_SENTENCES}문장)'
        )

    return {
        'one_line': one_line,
        'easy_explanation': data['easy_explanation'].strip(),
        'why_important': data['why_important'].strip(),
        'importance': data['importance'],
        'evidence': checked,
        'unsupported_numbers': unsupported,
        'uncited_numbers': uncited,
        'sentence_count': sentences,
        'warnings': warnings,
    }


#: 경고 문구의 접두어. 경고의 **종류**를 문자열에서 되읽기 위한 단일 출처다.
#: 화면(정확성 배너)과 admin이 종류별로 다르게 다뤄야 하므로 접두어를 고정한다.
#: 문구를 바꾸면 저장된 경고와 어긋나므로 revalidate_summaries 를 다시 돌려야 한다.
UNSUPPORTED_NUMBER_PREFIX = '인용 근거 없는 수치: '
UNCITED_NUMBER_PREFIX = '인용에 없는 수치(원문에는 있음): '
UNVERIFIED_QUOTE_PREFIX = '원문에서 찾지 못한 인용'
SENTENCE_COUNT_PREFIX = '설명 길이'

#: 요약의 **사실 정확성**과 직접 관련된 경고. 사용자에게 알려야 하는 종류다.
#: 문장 수(SENTENCE_COUNT_PREFIX)는 문체 문제라 여기 넣지 않는다 —
#: 3문장 권장을 6문장으로 쓴 것을 두고 "수치를 신뢰하지 말라"고 경고하면 오히려 해롭다.
#: 인용 누락(UNCITED_NUMBER_PREFIX)도 여기 넣는다. 값이 원문과 일치하는 것은 확인했지만
#: **인용문 대조보다 약한 확인**이기 때문이다 — 200쪽짜리 문서 어딘가에 같은 숫자가 무관한
#: 맥락으로 있었을 가능성이 남는다. 게시는 하되 화면에 표시는 하는 것이 그 잔여 위험에 맞는
#: 대응이다. 감추는 것과 알리는 것은 다른 판단이라는 원칙(아래 참고)의 연장이다.
ACCURACY_WARNING_PREFIXES = (
    UNSUPPORTED_NUMBER_PREFIX, UNCITED_NUMBER_PREFIX, UNVERIFIED_QUOTE_PREFIX,
)

#: **게시를 막는** 경고. 이 경고가 붙은 요약은 자동으로 미게시 상태로 만든다.
#:
#: 수치 경고만 넣는다. 세 종류를 하나씩 따져 보면 이 결론밖에 없다.
#:   - 수치(UNSUPPORTED_NUMBER_PREFIX): 요약 본문의 금액이 **원문 어디에도** 없다.
#:     실제로 39조를 3조로 적은 요약이 경고를 단 채 웹에 그대로 떠 있었다. **사실 오류**이고
#:     독자가 그대로 믿으면 판단이 통째로 어긋난다 → 막는다.
#:   - 인용 누락(UNCITED_NUMBER_PREFIX): 값은 원문과 일치하고 인용만 빠졌다.
#:     **이걸 막던 것이 실제 손해였다.** v3 재요약 직후 자동 미게시 10건 중 8건이 여기였고,
#:     경고 대상 수치 16개가 전부 원문에 존재했다. 정확한 요약을 형식 때문에 감춘 셈이다
#:     → 게시하되 검수를 요청하고 화면에 표시한다.
#:   - 인용(UNVERIFIED_QUOTE_PREFIX): 3단계 조사에서 대부분 "표의 떨어진 두 행을 이어 붙인"
#:     **인용 형식** 문제로 밝혀졌다. 요약 본문의 사실이 틀린 것이 아니다. 막으면 멀쩡한
#:     요약이 대량으로 내려간다 → 게시하되 화면에 표시만 한다(accuracy_warnings).
#:   - 문체(SENTENCE_COUNT_PREFIX): 3문장 권장을 6문장으로 쓴 것뿐이다. 이걸로 게시를 막으면
#:     읽을 수 있는 글을 형식 때문에 감추는 셈이다 → 막지 않는다.
#:
#: ACCURACY_WARNING_PREFIXES(사용자에게 알릴 경고)와 **일부러 다르게** 둔다.
#: "알려야 하는 것"과 "감춰야 하는 것"은 다른 판단이다 — 인용 경고가 정확히 그 차이에 있다.
PUBLICATION_BLOCKING_PREFIXES = (UNSUPPORTED_NUMBER_PREFIX,)

#: 자동 미게시 사유 문구. 요약 생성(summarize_disclosures)과 재검증(revalidate_summaries)이
#: 같은 문구를 써야 검수 화면에서 두 경로를 구분할 이유가 없어진다.
AUTO_HIDDEN_REASON = '자동 검증 실패(수치 근거 없음) — 사람 확인 전까지 비공개'


def blocking_warnings(warnings):
    """경고 목록에서 게시를 막아야 하는 것만 추린다.

    요약 생성·재검증·검수 화면이 모두 이 함수 하나를 봐야 "왜 내려갔는가"가 경로에 따라
    달라지지 않는다.
    """
    return [
        warning for warning in (warnings or [])
        if warning.startswith(PUBLICATION_BLOCKING_PREFIXES)
    ]


#: 경고 문구에서 수치 여러 개를 잇는 구분자.
#: 쉼표만으로 나누면 `3조 9,891억`이 `3조 9` + `891억`으로 쪼개진다 — 자릿수 쉼표와
#: 구분자를 구별해야 하므로 **쉼표+공백**을 함께 쓰고, 되읽을 때도 같은 문자열로 나눈다.
WARNING_LIST_SEPARATOR = ', '


def build_review_warnings(result):
    """DisclosureSummary.review_warnings 에 저장할 경고 목록을 만든다.

    요약 생성(summarize_disclosures)과 재검증(revalidate_summaries) 두 경로가 이 함수를
    공유해야 같은 요약이 경로에 따라 다르게 판정되지 않는다. 문구를 고칠 일이 있으면
    여기만 고친다.

    validate_summary 가 반환하는 `warnings` 를 그대로 쓰지 않는 이유는, 검수자가
    admin에서 바로 판단할 수 있도록 문제가 된 인용문·수치를 문구에 담기 위해서다.

    각 문구는 위 접두어 상수로 시작한다. `DisclosureSummary.accuracy_warnings` 가
    이 접두어로 정확성 경고만 골라내므로, 접두어보다 앞에 다른 말을 붙이면 안 된다.
    """
    warnings = []
    if result.get('unsupported_numbers'):
        warnings.append(
            UNSUPPORTED_NUMBER_PREFIX
            + WARNING_LIST_SEPARATOR.join(result['unsupported_numbers'])
        )
    if result.get('uncited_numbers'):
        warnings.append(
            UNCITED_NUMBER_PREFIX
            + WARNING_LIST_SEPARATOR.join(result['uncited_numbers'])
        )
    for idx, item in enumerate(result.get('evidence', [])):
        if not item.get('quote_found', True):
            quote = (item.get('quote') or '').strip()
            warnings.append(
                f'{UNVERIFIED_QUOTE_PREFIX} (근거 {idx + 1}번): {quote[:60]}'
            )
    sentences = result.get('sentence_count')
    if sentences is not None and not (
        EXPLANATION_MIN_SENTENCES <= sentences <= EXPLANATION_MAX_SENTENCES
    ):
        warnings.append(
            f'{SENTENCE_COUNT_PREFIX}: 쉬운 설명이 {sentences}문장 '
            f'(권장 {EXPLANATION_MIN_SENTENCES}~{EXPLANATION_MAX_SENTENCES}문장)'
        )
    return warnings
