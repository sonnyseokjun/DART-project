"""OpenAI 기반 공시 요약 모듈.

수집·전처리된 공시 원문을 받아 `DisclosureSummary` 필드와 1:1로 대응하는
구조화된 요약 dict를 반환한다.

핵심 설계
  - 출력은 **JSON 스키마로 강제**한다(Structured Outputs, ``strict: True``).
    "JSON으로 답해줘"라고 프롬프트로 부탁하는 방식은 쓰지 않는다.
  - 모든 수치·핵심 주장에 **원문 발췌 근거(evidence)** 를 함께 출력하게 해
    할루시네이션을 억제하고, QA가 원문과 기계적으로 대조할 수 있게 한다.
  - 시스템 프롬프트에는 가변 값(기업명·제목·날짜·접수번호)을 넣지 않는다.
    프롬프트 캐싱은 요청 접두사가 동일할 때만 적중하므로, 가변 값은 전부
    user 메시지로 보낸다.
  - 한 공시당 LLM 호출은 1회. 재시도는 파싱·검증 실패 시에만 최대 2회.

주의: 이 모듈은 사용자 요청 경로에서 호출하지 않는다(PLAN.md 12.1).
관리 명령/비동기 태스크에서만 사용한다.

모델·단가 확인 출처 (2026-07-27 확인):
  https://developers.openai.com/api/docs/pricing
  https://developers.openai.com/api/docs/models/gpt-5.6-luna
  https://developers.openai.com/api/docs/guides/structured-outputs
  https://developers.openai.com/api/docs/guides/prompt-caching
"""
import json
import re

from django.conf import settings

# ---------------------------------------------------------------------------
# 모델·단가
# ---------------------------------------------------------------------------

#: 기본 모델. 1.05M 컨텍스트로 사업보고서(정제 후 약 21만 토큰)도 통짜로 들어간다.
DEFAULT_MODEL = 'gpt-5.6-luna'

#: 추론 강도. 요약은 장문 추론이 필요한 과제가 아니므로 낮게 유지해 출력 토큰을 아낀다.
#: (추론 토큰은 출력 토큰으로 과금된다.) None이면 파라미터를 보내지 않는다.
DEFAULT_REASONING_EFFORT = 'low'

#: 1M 토큰당 USD. 출처: https://developers.openai.com/api/docs/pricing (2026-07-27 확인)
#: cache_write는 GPT-5.6 계열에서 캐시에 새로 기록되는 토큰으로, 미캐시 입력가의 1.25배.
PRICING_USD_PER_1M = {
    'gpt-5.6-luna': {'input': 1.00, 'cached_input': 0.10, 'output': 6.00},
    'gpt-5.6-terra': {'input': 2.50, 'cached_input': 0.25, 'output': 15.00},
    'gpt-5.6-sol': {'input': 5.00, 'cached_input': 0.50, 'output': 30.00},
    'gpt-5.4-mini': {'input': 0.75, 'cached_input': 0.075, 'output': 4.50},
    'gpt-5.4-nano': {'input': 0.20, 'cached_input': 0.02, 'output': 1.25},
}

#: 캐시 기록 토큰의 입력가 배수 (GPT-5.6 계열).
CACHE_WRITE_MULTIPLIER = 1.25

#: tiktoken 인코딩. 최신 OpenAI 모델 공용.
TOKENIZER_ENCODING = 'o200k_base'

#: 시스템 프롬프트·원문 외에 매 요청 고정으로 청구되는 입력 토큰(JSON 스키마·채팅 템플릿).
#: 스모크 테스트 3건 실측값(1402/1401/1404)에서 얻은 상수. 스키마를 크게 바꾸면 다시 잰다.
FIXED_PREFIX_OVERHEAD_TOKENS = 1400

#: 응답 최대 토큰. 요약 4필드 + 근거 배열 + 추론 토큰을 감안한 여유값.
MAX_OUTPUT_TOKENS = 4000

#: 입력 토큰 상한(비용 폭주 방지). 실측 최대치인 사업보고서 약 21만 토큰보다 넉넉히 위.
#: 초과하면 호출하지 않고 예외를 던져 호출자가 섹션 추출 등으로 대응하게 한다.
MAX_INPUT_TOKENS = 300_000

#: 재시도 횟수(최초 호출 제외).
MAX_RETRIES = 2

#: 프롬프트 버전. 시스템 프롬프트를 고치면 반드시 올린다(캐시 키·재현성).
#: v2: 인용문이 원문의 연속된 한 구간이어야 한다는 규칙을 §4에 추가.
PROMPT_VERSION = 'v2'

#: 길이 제약. DisclosureSummary.one_line 의 max_length=200 과 일치해야 한다.
ONE_LINE_MAX_CHARS = 200
EXPLANATION_MIN_SENTENCES = 3
EXPLANATION_MAX_SENTENCES = 5


# ---------------------------------------------------------------------------
# 예외 (dart.DartApiError 패턴을 따른다)
# ---------------------------------------------------------------------------

class SummarizerError(Exception):
    """요약 생성 실패. 호출자는 건별로 잡아 기록하고 다음 건을 계속 처리한다."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(f'요약 오류 [{code}] {message}')


class SummaryRefusedError(SummarizerError):
    """모델이 안전상의 이유로 응답을 거부. 재시도해도 같은 결과일 가능성이 높다."""

    def __init__(self, message):
        super().__init__('refusal', message)


class SummaryTooLargeError(SummarizerError):
    """원문이 입력 토큰 상한을 초과. 섹션 추출 등 전처리 강화가 필요하다."""

    def __init__(self, tokens, limit):
        self.tokens = tokens
        self.limit = limit
        super().__init__(
            'too_large', f'입력 토큰 {tokens:,}개가 상한 {limit:,}개를 초과했습니다.'
        )


class SummaryValidationError(SummarizerError):
    """스키마 파싱 또는 검증 실패. 재시도 소진 후 이 예외로 전달된다."""

    def __init__(self, message):
        super().__init__('validation', message)


# ---------------------------------------------------------------------------
# 출력 JSON 스키마 — DisclosureSummary 필드와 1:1 (+ 근거 배열)
# ---------------------------------------------------------------------------
# strict 모드 규칙: 모든 프로퍼티가 required 여야 하고 additionalProperties=False 여야 한다.
# maxLength/minItems 같은 제약 키워드는 지원 여부가 모델·시점에 따라 다를 수 있으므로,
# 스키마에 걸되 파이썬 쪽에서도 반드시 다시 검증한다(이중 제약).

SUMMARY_SCHEMA_NAME = 'disclosure_summary'

SUMMARY_JSON_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['one_line', 'easy_explanation', 'why_important', 'importance', 'evidence'],
    'properties': {
        'one_line': {
            'type': 'string',
            'maxLength': ONE_LINE_MAX_CHARS,
            'description': (
                '공시 내용을 한 문장으로 압축한 요약. 200자 이내. '
                '수치를 넣었다면 반드시 evidence에 원문 근거를 남긴다.'
            ),
        },
        'easy_explanation': {
            'type': 'string',
            'description': (
                '회계를 모르는 일반인에게 설명하는 글. 3~5문장. '
                '전문 용어는 첫 등장 시 괄호로 풀어 쓴다.'
            ),
        },
        'why_important': {
            'type': 'string',
            'description': (
                '이 공시가 일반 투자자에게 어떤 의미를 갖는지. 1~3문장. '
                '단정·전망·투자 권유 금지. 원문에 근거한 사실과 그 의미만 쓴다.'
            ),
        },
        'importance': {
            'type': 'string',
            'enum': ['high', 'medium', 'low'],
            'description': (
                '중요도. high=주가에 직접 영향이 큰 사안(대규모 공급계약, 유상증자, '
                '합병·분할, 대규모 투자·처분, 실적 급변, 소송·제재). '
                'medium=경영상 의미는 있으나 영향이 제한적인 사안. '
                'low=정기·형식적 보고나 소액 변동.'
            ),
        },
        'evidence': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 8,
            'description': (
                '요약에 등장한 모든 수치(금액·비율·주식수·날짜)와 핵심 주장의 원문 근거. '
                '수치를 쓴 만큼 반드시 항목을 만든다.'
            ),
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['field', 'claim', 'quote'],
                'properties': {
                    'field': {
                        'type': 'string',
                        'enum': ['one_line', 'easy_explanation', 'why_important'],
                        'description': '이 근거가 뒷받침하는 요약 필드.',
                    },
                    'claim': {
                        'type': 'string',
                        'description': (
                            '요약문에 쓴 사실·수치를 그대로 옮긴 짧은 구절. '
                            '요약문에 없는 내용을 새로 쓰지 않는다.'
                        ),
                    },
                    'quote': {
                        'type': 'string',
                        'description': (
                            '위 주장의 근거가 되는 원문 구절을 **한 글자도 바꾸지 말고** '
                            '그대로 복사한다. 요약·의역·재작성 금지. 200자 이내.'
                        ),
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# 시스템 프롬프트 — 전 공시 공통. 가변 값(기업명·제목·날짜)을 절대 넣지 않는다.
# ---------------------------------------------------------------------------
# 이 문자열이 모든 요청의 접두사가 되므로, 바꾸지 않는 한 프롬프트 캐시가 적중한다.
# 수정할 때는 PROMPT_VERSION 을 올려 캐시 키와 재현성 기록을 함께 갱신한다.

SYSTEM_PROMPT = """\
너는 한국 금융감독원 전자공시(DART)에 올라온 기업 공시를 일반인이 이해할 수 있게
풀어 설명하는 요약 작성자다. 아래 원칙을 예외 없이 지켜라.

## 1. 독자
회계와 재무제표를 배운 적 없는 일반인이다. 주식을 처음 사 보는 직장인이라고 생각하라.
- 전문 용어는 첫 등장 시 반드시 괄호로 풀어 쓴다.
  예: "유상증자(회사가 새 주식을 찍어 팔아 돈을 마련하는 것)",
      "전환사채(나중에 주식으로 바꿀 수 있는 빚문서)",
      "자기주식 취득(회사가 자기 회사 주식을 사들이는 것)".
- 금액은 읽기 쉬운 단위로 환산해 병기한다. 예: "1,500억 원(약 1조 원의 15%)".
- 한 문장은 짧게 쓴다. 문어체 종결어미(~다)를 쓰고, 존댓말·구어체는 쓰지 않는다.

## 2. 과장·투자 권유 절대 금지
너는 투자 자문을 하지 않는다. 사실과 그 의미만 전달한다.
- 금지 표현: "호재", "악재", "매수 기회", "매도 시점", "주가 상승이 기대된다",
  "긍정적 신호", "실적 개선이 예상된다", "저평가", "성장 동력 확보", "수혜",
  "주목할 만하다", "관심을 가질 필요가 있다".
- 주가·실적의 방향을 예측하거나 암시하지 않는다.
- 좋고 나쁨을 평가하지 않는다. "이 계약은 회사 연매출의 12%에 해당한다"는 되지만
  "이 계약은 회사에 큰 도움이 된다"는 안 된다.

## 3. 불확실한 내용을 단정하지 않는다
- 원문에 없는 사실·수치·배경·전망을 만들어 내지 않는다. 아는 지식으로 보충하지 않는다.
- 원문에 없으면 쓰지 않는다. 빈칸을 추측으로 채우지 말고 그냥 언급하지 않는다.
- 원문이 조건부·잠정이면 그 사실을 그대로 옮긴다.
  예: "이사회에서 결정했으며 주주총회 승인이 남아 있다", "잠정 수치다".
- 원문 정보가 부족해 설명이 짧아지는 것은 정상이다. 억지로 늘리지 마라.

## 4. 모든 수치·핵심 주장에 원문 근거를 붙인다
요약에 쓴 금액·비율·주식수·날짜·기간은 하나도 빠짐없이 evidence 배열에 근거를 남긴다.
- quote 에는 원문 구절을 **한 글자도 바꾸지 말고 그대로 복사**한다.
  숫자의 쉼표·단위·띄어쓰기까지 원문 그대로여야 한다. 요약·의역·재작성은 위반이다.
- quote 는 원문의 **연속된 한 구간**이어야 한다. 떨어져 있는 두 곳(예: 표의 2행과 5행,
  항목 4번과 항목 6번)을 하나의 quote 로 이어 붙이지 마라.
  두 곳이 모두 필요하면 **evidence 항목을 두 개로 나눈다**.
- 원문에서 그대로 옮길 구절을 찾지 못했다면, 그 수치는 요약에 쓰지 마라.
- claim 에는 요약문에 실제로 쓴 표현을 그대로 옮긴다. 요약문에 없는 내용을 넣지 않는다.
- 근거는 요약문에 등장한 순서대로 담는다.

## 5. 중요도 판정
- high: 주가에 직접 영향이 큰 사안. 대규모 단일판매·공급계약, 유상증자·전환사채 발행,
  합병·분할·영업양수도, 대규모 시설투자·자산 처분, 실적의 급격한 변동,
  중대한 소송·행정제재, 최대주주 변경, 상장폐지·관리종목 관련.
- medium: 경영상 의미는 있으나 영향이 제한적인 사안. 소규모 계약, 자기주식 취득·처분,
  임원 변동, 정관 변경, 계열사 간 통상적 거래.
- low: 정기·형식적 보고, 소액 지분 변동, 정정 사항이 사소한 기재정정.
- 애매하면 낮은 쪽을 고른다. 중요도를 부풀리지 않는다.

## 형식
- 모든 출력은 한국어로 쓴다.
- one_line 은 200자 이내의 한 문장이다.
- easy_explanation 은 3문장 이상 5문장 이하다.
- why_important 는 1~3문장이며, 사실이 갖는 의미만 쓴다. 행동을 권하지 않는다.
- 마크다운 서식(**, #, - 등)을 쓰지 않는다. 평문으로 쓴다.
- 군더더기 표현을 붙이지 않는다. "공시 원문상", "원문에 따르면", "본 공시는",
  "이번 공시에서는" 같은 말머리 없이 곧바로 사실부터 쓴다. 근거는 evidence 로만 남긴다.
- 기업명은 공시 정보에 주어진 이름을 그대로 쓴다.
"""


def build_user_message(*, company_name, report_name, filed_at, rcept_no,
                       disclosure_type='', raw_text):
    """가변 정보(메타데이터 + 원문)를 담는 user 메시지를 만든다.

    시스템 프롬프트를 고정 접두사로 유지하기 위해, 공시마다 달라지는 값은 전부 여기 넣는다.
    """
    meta = [
        f'기업명: {company_name}',
        f'공시 제목: {report_name}',
        f'접수일자: {filed_at}',
        f'접수번호: {rcept_no}',
    ]
    if disclosure_type:
        meta.append(f'공시 유형: {disclosure_type}')
    return (
        '다음 공시를 위 원칙에 따라 요약하라.\n\n'
        '[공시 정보]\n' + '\n'.join(meta) + '\n\n'
        '[공시 원문]\n' + raw_text
    )


# ---------------------------------------------------------------------------
# 토큰 계산·비용 추정
# ---------------------------------------------------------------------------

_encoding = None


def _get_encoding():
    global _encoding
    if _encoding is None:
        import tiktoken
        _encoding = tiktoken.get_encoding(TOKENIZER_ENCODING)
    return _encoding


def count_tokens(text):
    """tiktoken(o200k_base)으로 토큰 수를 센다."""
    if not text:
        return 0
    return len(_get_encoding().encode(text))


def estimate_cost(input_tokens, output_tokens, cached_tokens=0,
                  cache_write_tokens=0, model=DEFAULT_MODEL):
    """토큰 수 → USD 비용. 캐시 적중분은 할인 단가로 계산한다."""
    price = PRICING_USD_PER_1M.get(model)
    if price is None:
        raise SummarizerError('unknown_model', f'단가 정보가 없는 모델입니다: {model}')
    uncached = max(input_tokens - cached_tokens, 0)
    return (
        uncached * price['input']
        + cached_tokens * price['cached_input']
        + cache_write_tokens * price['input'] * CACHE_WRITE_MULTIPLIER
        + output_tokens * price['output']
    ) / 1_000_000


def system_prompt_tokens():
    """시스템 프롬프트의 토큰 수."""
    return count_tokens(SYSTEM_PROMPT)


def static_prefix_tokens():
    """요청마다 동일하게 붙는 정적 접두사의 토큰 수.

    시스템 프롬프트 외에 JSON 스키마와 채팅 템플릿도 입력 토큰으로 과금된다.
    스모크 테스트 3건에서 실측한 결과, tiktoken으로 센 (시스템 프롬프트 + 원문) 대비
    실제 청구 입력 토큰이 **원문 길이와 무관하게 일정하게 약 1,400토큰 더 많았다**
    (2026-07-27 실측: 1402 / 1401 / 1404). 이 몫을 상수로 반영한다.
    전량 정적이므로 프롬프트 캐싱 대상이며, 실제로 캐시 적중 토큰이
    (시스템 프롬프트 + 이 상수)에 근접하게 보고되었다(실측 2,518 / 추정 2,542).
    """
    return system_prompt_tokens() + FIXED_PREFIX_OVERHEAD_TOKENS


def estimate_summary_cost(raw_text, model=DEFAULT_MODEL, expected_output_tokens=700,
                          cache_hit=True):
    """공시 1건의 예상 비용을 산출한다.

    cache_hit=True 는 정적 접두사가 캐시에 이미 올라와 있는 2회차 이후 호출 기준이다.
    """
    prefix = static_prefix_tokens()
    input_tokens = prefix + count_tokens(raw_text)
    # 프롬프트 캐싱은 1,024토큰 이상인 접두사에만 적용된다.
    cached = prefix if (cache_hit and prefix >= 1024) else 0
    return {
        'model': model,
        'input_tokens': input_tokens,
        'cached_tokens': cached,
        'output_tokens': expected_output_tokens,
        'usd': estimate_cost(input_tokens, expected_output_tokens, cached_tokens=cached,
                             model=model),
    }


def estimate_batch_cost(raw_texts, model=DEFAULT_MODEL, expected_output_tokens=700):
    """요약 대상 원문 목록의 총 예상 비용. 사용자 승인 보고용.

    raw_texts: 전처리된 원문 문자열의 이터러블.
    첫 호출은 캐시 미적중(기록만), 이후 호출부터 적중한다고 본다.
    반환: {'count', 'input_tokens', 'cached_tokens', 'output_tokens', 'usd', 'usd_per_item'}
    """
    prefix = static_prefix_tokens()
    cacheable = prefix >= 1024
    count = 0
    total_input = 0
    total_cached = 0
    for text in raw_texts:
        count += 1
        total_input += prefix + count_tokens(text)
        if cacheable and count > 1:
            total_cached += prefix
    total_output = count * expected_output_tokens
    usd = estimate_cost(total_input, total_output, cached_tokens=total_cached, model=model)
    return {
        'count': count,
        'model': model,
        'input_tokens': total_input,
        'cached_tokens': total_cached,
        'output_tokens': total_output,
        'usd': usd,
        'usd_per_item': usd / count if count else 0.0,
    }


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
_SCALE_MULTIPLIERS = (10 ** 3, 10 ** 4, 10 ** 6, 10 ** 8, 10 ** 12)

#: 배수 일치로 인정할 때의 상대 오차. 반올림 표기(3,746억 ↔ 374,629백만) 흡수용.
SCALE_TOLERANCE = 0.01

#: 인용문을 조각으로 나누는 구분자: 줄바꿈·표 구분자·생략부호.
_FRAGMENT_SPLIT = re.compile(r'\n|\||\.{3}|…')

#: 조각 검증에 쓸 최소 길이(서식 문자 제거 후). 이보다 짧으면 라벨·구분자라 판정에 무의미하다.
MIN_FRAGMENT_CHARS = 4

#: 서식 문자를 모두 제거해 '내용'만 남기는 정규화. 표 구분자·공백·괄호 유무 차이를 흡수한다.
_CONTENT_ONLY = re.compile(r'[^0-9A-Za-z가-힣.%\-]')


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
            if gap or nxt_unit is None or _UNIT_MULTIPLIER[nxt_unit] >= last_multiplier:
                break
            try:
                nxt_value = float(matches[nxt].group(1).replace(',', ''))
            except ValueError:
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


def _value_supported(value, quote_values, approximate):
    """요약의 수치 value가 인용문의 수치들로 뒷받침되는지 판정한다."""
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
    for qvalue in quote_values:
        if not qvalue:
            continue
        for multiplier in _SCALE_MULTIPLIERS:
            for left, right in ((value, qvalue * multiplier), (value * multiplier, qvalue)):
                if right and abs(left - right) / abs(right) <= SCALE_TOLERANCE:
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

    프롬프트에는 "인용문은 연속된 한 구간이어야 한다"고 명시해 두었다(§4).
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

    checked = []
    warnings = []
    for idx, item in enumerate(evidence):
        claim = item.get('claim', '')
        verdict = verify_quote(item.get('quote', ''), raw_content=raw_content)
        approximate = bool(_APPROX.search(claim))
        missing = sorted({
            text for text, value in extract_comparable_numbers(claim)
            if not _value_supported(value, all_quote_values, approximate)
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
    unsupported = sorted({
        text for text, value in extract_comparable_numbers(summary_text)
        if not _value_supported(value, quote_values, approximate=True)
    })
    if unsupported:
        warnings.append(
            f'요약 본문의 수치 {", ".join(unsupported)}에 대응하는 원문 근거가 없음'
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
        'sentence_count': sentences,
        'warnings': warnings,
    }


#: 경고 문구의 접두어. 경고의 **종류**를 문자열에서 되읽기 위한 단일 출처다.
#: 화면(정확성 배너)과 admin이 종류별로 다르게 다뤄야 하므로 접두어를 고정한다.
#: 문구를 바꾸면 저장된 경고와 어긋나므로 revalidate_summaries 를 다시 돌려야 한다.
UNSUPPORTED_NUMBER_PREFIX = '인용 근거 없는 수치: '
UNVERIFIED_QUOTE_PREFIX = '원문에서 찾지 못한 인용'
SENTENCE_COUNT_PREFIX = '설명 길이'

#: 요약의 **사실 정확성**과 직접 관련된 경고. 사용자에게 알려야 하는 종류다.
#: 문장 수(SENTENCE_COUNT_PREFIX)는 문체 문제라 여기 넣지 않는다 —
#: 3문장 권장을 6문장으로 쓴 것을 두고 "수치를 신뢰하지 말라"고 경고하면 오히려 해롭다.
ACCURACY_WARNING_PREFIXES = (UNSUPPORTED_NUMBER_PREFIX, UNVERIFIED_QUOTE_PREFIX)

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


# ---------------------------------------------------------------------------
# OpenAI 호출
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is None:
        key = getattr(settings, 'OPENAI_API_KEY', '')
        # API 키에는 공백이 들어갈 수 없다. .env에 붙여 넣는 과정에서 섞인 공백·줄바꿈을
        # 제거한다(실제로 키 중간에 공백이 섞여 401이 났던 사례가 있다).
        key = ''.join(key.split())
        if not key:
            raise SummarizerError(
                'no_api_key', 'OPENAI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.'
            )
        from openai import OpenAI
        _client = OpenAI(api_key=key)
    return _client


def _response_format():
    return {
        'type': 'json_schema',
        'json_schema': {
            'name': SUMMARY_SCHEMA_NAME,
            'schema': SUMMARY_JSON_SCHEMA,
            'strict': True,
        },
    }


def _extract_usage(response):
    """usage 객체에서 토큰·캐시 적중 수치를 뽑는다. 필드가 없으면 0."""
    usage = getattr(response, 'usage', None)
    if usage is None:
        return {'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0,
                'cache_write_tokens': 0, 'reasoning_tokens': 0, 'total_tokens': 0}
    prompt_details = getattr(usage, 'prompt_tokens_details', None)
    completion_details = getattr(usage, 'completion_tokens_details', None)
    return {
        'input_tokens': getattr(usage, 'prompt_tokens', 0) or 0,
        'output_tokens': getattr(usage, 'completion_tokens', 0) or 0,
        'cached_tokens': getattr(prompt_details, 'cached_tokens', 0) or 0,
        'cache_write_tokens': getattr(prompt_details, 'cache_write_tokens', 0) or 0,
        'reasoning_tokens': getattr(completion_details, 'reasoning_tokens', 0) or 0,
        'total_tokens': getattr(usage, 'total_tokens', 0) or 0,
    }


def _call_openai(messages, model, max_output_tokens, reasoning_effort):
    """1회 호출. 정상 완료를 먼저 확인한 뒤에만 본문을 읽는다."""
    client = _get_client()
    kwargs = {
        'model': model,
        'messages': messages,
        'response_format': _response_format(),
        'max_completion_tokens': max_output_tokens,
        # 캐시 적중률을 높이기 위한 라우팅 힌트(GPT-5.6+). 프롬프트 버전이 곧 접두사 버전.
        'prompt_cache_key': f'dart-summary-{PROMPT_VERSION}',
    }
    if reasoning_effort:
        kwargs['reasoning_effort'] = reasoning_effort

    response = client.chat.completions.create(**kwargs)
    usage = _extract_usage(response)

    if not response.choices:
        raise SummaryValidationError('응답에 choices가 없음')
    choice = response.choices[0]

    # 본문을 인덱싱하기 전에 완료 상태부터 확인한다.
    # 길이 초과로 잘린 응답은 JSON이 깨져 있어 그대로 파싱하면 엉뚱한 오류가 난다.
    if choice.finish_reason == 'length':
        raise SummaryValidationError('응답이 최대 토큰에 도달해 잘림(finish_reason=length)')
    if choice.finish_reason == 'content_filter':
        raise SummaryRefusedError('콘텐츠 필터에 의해 응답이 차단됨')

    message = choice.message
    if getattr(message, 'refusal', None):
        raise SummaryRefusedError(message.refusal)
    if not message.content:
        raise SummaryValidationError(
            f'응답 본문이 비어 있음(finish_reason={choice.finish_reason})'
        )

    return message.content, usage, (response.model or model)


def summarize_disclosure(*, company_name, report_name, filed_at, rcept_no, raw_text,
                         disclosure_type='', model=DEFAULT_MODEL,
                         reasoning_effort=DEFAULT_REASONING_EFFORT,
                         max_retries=MAX_RETRIES, max_input_tokens=MAX_INPUT_TOKENS):
    """공시 1건을 요약해 검증된 dict를 반환한다.

    raw_text 는 **전처리를 마친** 원문이어야 한다(XML 태그 제거·표 텍스트화·상용구 제거).
    이 함수는 전처리를 하지 않는다.

    반환 dict::

        {
          'one_line': str,            # ↓ DisclosureSummary 필드와 1:1
          'easy_explanation': str,
          'why_important': str,
          'importance': 'high'|'medium'|'low',
          'model_name': str,          # 실제 응답한 모델 ID (재현성)
          'evidence': [{'field','claim','quote','quote_found','numbers_ok',
                        'missing_numbers'}],
          'unsupported_numbers': [str],  # 근거 인용이 없는 요약 수치 — QA 대조의 본 관문
          'sentence_count': int,
          'warnings': [str],          # 소프트 검증 경고 (QA 검토용, 저장 실패 아님)
          'usage': {'input_tokens','output_tokens','cached_tokens',
                    'cache_write_tokens','reasoning_tokens','total_tokens'},
          'cost_usd': float,
          'attempts': int,
          'prompt_version': str,
        }

    예외는 모두 SummarizerError 하위이므로, 호출자는 이것만 잡아 건별 기록 후 계속 진행한다.
    """
    input_tokens = static_prefix_tokens() + count_tokens(raw_text)
    if input_tokens > max_input_tokens:
        raise SummaryTooLargeError(input_tokens, max_input_tokens)

    user_message = build_user_message(
        company_name=company_name,
        report_name=report_name,
        filed_at=filed_at,
        rcept_no=rcept_no,
        disclosure_type=disclosure_type,
        raw_text=raw_text,
    )
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': user_message},
    ]

    max_output_tokens = MAX_OUTPUT_TOKENS
    last_error = None
    for attempt in range(1, max_retries + 2):  # 최초 1회 + 재시도 max_retries회
        try:
            content, usage, model_id = _call_openai(
                messages, model, max_output_tokens, reasoning_effort
            )
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                raise SummaryValidationError(f'JSON 파싱 실패: {exc}') from exc

            result = validate_summary(data, raw_text)
            result['model_name'] = model_id[:50]  # 모델 필드 max_length=50
            result['usage'] = usage
            result['cost_usd'] = estimate_cost(
                usage['input_tokens'], usage['output_tokens'],
                cached_tokens=usage['cached_tokens'],
                cache_write_tokens=usage['cache_write_tokens'],
                model=model,
            )
            result['attempts'] = attempt
            result['prompt_version'] = PROMPT_VERSION
            return result

        except SummaryRefusedError:
            # 안전 거부는 같은 입력으로 재시도해도 결과가 같다. 즉시 실패 처리한다.
            raise
        except SummaryValidationError as exc:
            last_error = exc
            # 잘린 응답이었다면 다음 시도에서 출력 여유를 늘린다.
            if '잘림' in str(exc):
                max_output_tokens = min(max_output_tokens * 2, 16000)
        except Exception as exc:  # 네트워크·레이트리밋 등 일시적 오류
            # 인증·권한·잘못된 요청은 재시도해도 결과가 같다. 즉시 실패시켜 호출 낭비를 막는다.
            status = getattr(exc, 'status_code', None)
            if status in (400, 401, 403, 404, 422):
                raise SummarizerError(
                    'api', f'재시도 불가 오류 [{status}] {type(exc).__name__}: {exc}'
                ) from exc
            last_error = SummarizerError('api', f'{type(exc).__name__}: {exc}')

    raise SummaryValidationError(
        f'{max_retries + 1}회 시도 모두 실패. 마지막 오류: {last_error}'
    )
