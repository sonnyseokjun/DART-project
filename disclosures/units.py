"""한국식 4자리 수 단위(만·억·조·경) 환산 — 순수 함수 모음.

## 왜 이 모듈이 필요한가

LLM이 반복해서 틀린 지점이 정확히 하나였다. **쉼표는 3자리로 끊고 만·억·조는 4자리로
끊는다.** 모델은 쉼표 위치를 보고 잘라 읽었다.

    45,453,450,000,000  →  (틀림) 4조 5,453억     ← 앞 두 덩어리 `45`,`453`을 조/억으로 봄
                           (맞음) 45조 4,534억 5,000만
                                  4자리로 끊으면  45 / 4534 / 5000 / 0000

숫자 자체는 한 자리도 틀리지 않았고 자릿수 끊기에서만 무너졌다. 이건 규칙이 완전히
정해진 계산이므로 코드가 하면 영원히 틀리지 않는다. LLM에게는 원문 숫자를 그대로
인용하게 하고, 읽기 쉬운 표기는 이 모듈이 만든다.

## 설계 제약

- **Django에 의존하지 않는다.** 표준 라이브러리(`re`)만 쓴다. `manage.py` 없이 단독으로
  import 해 테스트할 수 있어야 한다.
- **부동소수점을 쓰지 않는다.** 39조를 float로 다루면 유효자리가 날아간다(`float`의 정확
  정수 표현 한계는 2**53 ≈ 9,007조로 조 단위 금액이 곧 그 경계에 닿는다). 전부 정수 연산이다.
- **전역 상태·부작용이 없다.** 같은 입력에 항상 같은 출력.
"""
import re

#: 4자리 단위. (10의 지수, 단위 문자) 를 큰 것부터 나열한다.
#: 지수 0(단위 없음)은 만 미만의 나머지를 담는 자리다.
#: 경(10^16)까지 둔 이유는 표기를 위해서가 아니라 `12,345조` 같은 어색한 출력을 막기 위해서다.
UNIT_EXPONENTS = ((16, '경'), (12, '조'), (8, '억'), (4, '만'), (0, ''))

#: 단위 사이의 자릿수 간격. 한국식 수 체계의 근본 상수이자 이 버그의 원인.
UNIT_STEP = 4

_UNIT_BY_EXPONENT = dict(UNIT_EXPONENTS)
_EXPONENT_BY_UNIT = {unit: exponent for exponent, unit in UNIT_EXPONENTS if unit}

#: 기본 표기 단위 수. 아래 `format_korean_amount` 의 docstring에 근거를 적었다.
DEFAULT_MAX_UNITS = 2

#: 절사가 일어났을 때 앞에 붙이는 말.
APPROX_MARK = '약 '

#: 통화 단위. 한글 맞춤법상 단위 명사는 띄어 쓴다(기존 프롬프트도 `1,500억 원` 표기).
WON_SUFFIX = '원'

#: 한국어 금액 표기 전체를 받아들이는 패턴. `parse_korean_amount` 가 통짜로 검사한다.
_AMOUNT_RE = re.compile(r'\A(?:\d[\d,]*\s*[경조억만]?\s*)+\Z')

#: 위 표기를 (숫자, 단위) 항으로 쪼개는 패턴.
_TERM_RE = re.compile(r'(\d[\d,]*)\s*([경조억만])?')


def split_units(value):
    """음이 아닌 정수를 [(10의 지수, 계수)] 로 분해한다. 계수가 0인 자리는 뺀다.

    큰 단위부터 내림차순으로 돌려준다. 0을 넣으면 빈 리스트다.

        >>> split_units(45453450000000)
        [(12, 45), (8, 4534), (4, 5000)]
        >>> split_units(10000)
        [(4, 1)]

    계수가 0인 자리를 빼는 것이 표기의 자연스러움을 좌우한다. `45조 4,534억 5,000만`에서
    일의 자리가 0인데 `0`을 붙이면 사람이 쓰지 않는 말이 된다.
    최상위 계수는 9,999를 넘을 수 있다(10^20 이상). 경 위의 단위는 실무에서 쓰지 않으므로
    `12,345경` 처럼 그대로 둔다.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f'정수만 받는다: {value!r}')
    if value < 0:
        raise ValueError(f'음수는 호출자가 부호를 분리해 넘긴다: {value!r}')

    terms = []
    remainder = value
    for exponent, _unit in UNIT_EXPONENTS:
        coefficient, remainder = divmod(remainder, 10 ** exponent)
        if coefficient:
            terms.append((exponent, coefficient))
    return terms


def format_korean_amount(value, *, max_units=DEFAULT_MAX_UNITS, approx_mark=APPROX_MARK):
    """정수 → 한국어 자릿수 표기 문자열. 통화 단위(`원`)는 붙이지 않는다.

        >>> format_korean_amount(45453450000000)
        '약 45조 4,534억'
        >>> format_korean_amount(45453450000000, max_units=None)
        '45조 4,534억 5,000만'
        >>> format_korean_amount(10000)
        '1만'

    ## max_units — 어디까지 쓸 것인가

    `max_units=n` 은 **최상위 단위에서 아래로 n개 단위(4n자리)까지만 쓰고 나머지는 버린다**는
    뜻이다. 항의 개수가 아니라 자릿수 폭 기준이다. 0인 자리를 생략한 결과 `10조 1` 처럼
    동떨어진 항만 남는 경우에도 폭 기준이면 `약 10조` 로 정리된다.

    기본값을 2(≈8자리 유효숫자)로 둔 근거:
      - 독자는 회계를 배운 적 없는 일반인이다(PLAN.md 1.4). `45조 4,534억 5,000만 원`은
        한 호흡에 읽히지 않는다. 규모를 전하는 데는 상위 두 단위면 충분하다.
      - 그렇다고 `약 45조`(1단위)로 끊으면 4,534억이 통째로 사라진다. 45조와 45.5조는
        일반인에게도 다른 크기다.
      - 정확한 값이 필요한 곳(검수 화면·원문 대조)은 `max_units=None` 으로 전체를 쓴다.
        표기를 줄인 자리에는 항상 원문 링크가 함께 노출된다(CLAUDE.md 컨벤션).

    ## 절사(버림)이지 반올림이 아닌 이유

    반올림하면 원문에 없는 숫자가 생긴다. `39조 8,905억 3,479만` 을 억 단위에서 반올림하면
    `8,905`가 되지만, 5,000만을 넘겼다면 `8,906`이 되어 **원문 어디에도 없는 자릿수**가
    나온다. 검증기는 요약의 수치를 원문 수치와 대조하는데, 반올림으로 만들어진 숫자는
    원문 접두와 어긋나 오탐을 만든다. 절사는 항상 원문의 앞자리를 그대로 보존한다.
    또 절사는 금액을 부풀리지 않는다 — 과장 금지 원칙(프롬프트 v3 §3)과 방향이 같다.
    버린 자리가 있으면 `약` 을 붙여 근사임을 명시한다.

    음수는 부호를 분리해 계산하고 `약 -39조 8,905억` 처럼 `약` 다음에 부호를 붙인다
    (`약`은 수 전체를 수식하고, 부호는 수의 일부다).
    """
    if max_units is not None and max_units < 1:
        raise ValueError(f'max_units는 1 이상이거나 None이어야 한다: {max_units!r}')

    negative = value < 0
    terms = split_units(-value if negative else value)
    if not terms:
        return '0'

    truncated = False
    if max_units is not None:
        floor_exponent = terms[0][0] - UNIT_STEP * (max_units - 1)
        kept = [term for term in terms if term[0] >= floor_exponent]
        truncated = len(kept) < len(terms)
    else:
        kept = terms

    body = ' '.join(
        '{:,}{}'.format(coefficient, _UNIT_BY_EXPONENT[exponent])
        for exponent, coefficient in kept
    )
    if negative:
        body = '-' + body
    if truncated and approx_mark:
        body = approx_mark + body
    return body


def format_korean_won(value, *, max_units=DEFAULT_MAX_UNITS, approx_mark=APPROX_MARK):
    """`format_korean_amount` 에 통화 단위를 붙인다.

        >>> format_korean_won(45453450000000)
        '약 45조 4,534억 원'
    """
    body = format_korean_amount(value, max_units=max_units, approx_mark=approx_mark)
    return f'{body} {WON_SUFFIX}'


def parse_korean_amount(text):
    """한국어 금액 표기 → 정수. 표기 전체가 금액이 아니면 None.

        >>> parse_korean_amount('약 45조 4,534억 5,000만 원')
        45453450000000
        >>> parse_korean_amount('39,890,534,790,000')
        39890534790000
        >>> parse_korean_amount('계약금액은 3조 원이다') is None
        True

    **금액 표기 하나를 통짜로** 받는 함수다. 자유 문장에서 수치를 긁어내는 일은
    `verification.extract_numbers` 가 맡는다(그쪽은 오탐을 감수하고 넓게 잡는다).
    여기서 통짜 일치를 요구하는 이유는 용도가 다르기 때문이다 — 이 함수는 코드가 만든
    표기를 되읽어 값이 보존됐는지 확인하는 데 쓴다. 넓게 잡으면 그 확인이 무의미해진다.

    쉼표는 자릿수 구분자로만 보고 무시한다. 그래서 원문의 `39,890,534,790,000` 과
    표기의 `4,534억` 을 같은 규칙으로 읽는다. 단위가 내림차순이 아니어도(`1억 2조`)
    합산해서 돌려준다 — 형식 검사기가 아니라 값 계산기다.
    """
    cleaned = (text or '').strip()

    mark = APPROX_MARK.strip()
    if mark and cleaned.startswith(mark):
        cleaned = cleaned[len(mark):].strip()
    negative = cleaned.startswith('-')
    if negative:
        cleaned = cleaned[1:].strip()
    if cleaned.endswith(WON_SUFFIX):
        cleaned = cleaned[:-len(WON_SUFFIX)].strip()

    if not _AMOUNT_RE.match(cleaned):
        return None

    total = 0
    for digits, unit in _TERM_RE.findall(cleaned):
        total += int(digits.replace(',', '')) * 10 ** _EXPONENT_BY_UNIT.get(unit, 0)
    return -total if negative else total
