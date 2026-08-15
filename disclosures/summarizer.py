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

from .units import format_korean_amount, format_korean_won

# 검증 로직은 disclosures/verification.py 로 분리했다(소유권 분리, 동작은 동일).
# 아래 이름들은 **하위 호환을 위한 재수출**이다 — `from .summarizer import validate_summary`
# 같은 기존 import 경로가 그대로 동작해야 한다. 새 코드는 verification 에서 직접 가져오라.
from .verification import (  # noqa: F401
    ACCURACY_WARNING_PREFIXES,
    APPROX_TOLERANCE,
    EXPLANATION_MAX_SENTENCES,
    EXPLANATION_MIN_SENTENCES,
    MIN_FRAGMENT_CHARS,
    ONE_LINE_MAX_CHARS,
    ORDINAL_MAX,
    SCALE_TOLERANCE,
    SENTENCE_COUNT_PREFIX,
    UNSUPPORTED_NUMBER_PREFIX,
    UNVERIFIED_QUOTE_PREFIX,
    WARNING_LIST_SEPARATOR,
    _value_supported,
    build_review_warnings,
    count_sentences,
    extract_comparable_numbers,
    extract_numbers,
    is_reference_number,
    quote_fragments,
    validate_summary,
    verify_evidence,
    verify_quote,
)

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
#: v2: 인용문이 원문의 연속된 한 구간이어야 한다는 규칙을 추가.
#: v3: **단위 환산·파생 계산 금지**(§2 신설). v2의 "금액은 읽기 쉬운 단위로 환산해
#:     병기한다"가 10배 오류 3건의 직접 원인이었다. 읽기 쉬운 표기는 코드가 만든다
#:     (`annotate_amounts`). 이 변경으로 §2 이하 절 번호가 하나씩 밀렸다.
PROMPT_VERSION = 'v3'

#: 재생성 횟수 상한은 여기 두지 않는다. `review_policy.MAX_REGENERATION_ATTEMPTS` 가
#: 유일한 출처이고 `summarize_disclosures` 의 루프가 그것만 본다. 같은 상한을 두 곳에
#: 적으면 한쪽만 고쳤을 때 조용히 어긋나는데, 어긋나는 방향이 비용 초과다.
#: 이 모듈은 "한 번의 교정 요청을 어떻게 만들 것인가"만 안다.

#: 금액 표기를 코드가 병기하기 시작하는 하한. 1억(10^8) 미만은 쉼표만으로 읽힌다.
#: `254,500원`은 그대로 읽히지만 `322,755,945,000원`은 자릿수를 세지 않으면 못 읽는다.
#: 실측 기준으로 사람이 쉼표 표기를 한눈에 읽는 한계가 백만 단위(7자리)여서 그 위로 잡았다.
AMOUNT_NOTATION_MIN = 10 ** 8

#: 길이·문장 수 제약(ONE_LINE_MAX_CHARS 등)은 verification.py 로 옮겼다.
#: 스키마가 참조하는 ONE_LINE_MAX_CHARS 는 위 재수출로 이 모듈에서도 그대로 쓸 수 있다.


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
                '수치를 넣었다면 반드시 evidence에 원문 근거를 남긴다. '
                '숫자는 원문에 인쇄된 표기 그대로 쓴다(만·억·조로 바꾸지 않는다).'
            ),
        },
        'easy_explanation': {
            'type': 'string',
            'description': (
                '회계를 모르는 일반인에게 설명하는 글. 3~5문장. '
                '전문 용어는 첫 등장 시 괄호로 풀어 쓴다. '
                '숫자는 원문 표기 그대로 쓰고, 계산으로 만든 값은 넣지 않는다.'
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
                '수치를 쓴 만큼 반드시 항목을 만든다. '
                '요약 본문의 숫자는 어느 quote엔가 문자 그대로 들어 있어야 한다.'
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
                            '그대로 복사한다. 요약·의역·재작성 금지. 200자 이내. '
                            '원문에 없는 기호(%, 원, 주)를 덧붙이지 않는다.'
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
- 한 문장은 짧게 쓴다. 문어체 종결어미(~다)를 쓰고, 존댓말·구어체는 쓰지 않는다.

## 2. 숫자는 원문에 인쇄된 그대로 옮긴다 — 환산도 계산도 하지 않는다
이 문서에서 가장 강한 규칙이다. 예외가 없다.

금지하는 것은 **긴 숫자를 만·억·조로 쪼개 다시 쓰는 일**이다.
금지하지 않는 것은 **원문이 이미 말해 준 단위를 그대로 반영하는 일**이다.
이 둘을 헷갈리지 마라. 아래 두 항목이 그 경계다.

- **(금지) 자릿수를 다시 끊지 마라.** 원문에 `45,453,450,000,000` 이라고 적혀 있으면
  요약에도 `45,453,450,000,000` 이라고 쓴다. 쉼표 위치까지 원문 그대로다.
  이것을 `45조 4,534억` 이나 `4조 5,453억` 으로 묶어 쓰는 일이 없어야 한다.
  쉼표는 세 자리로 끊고 만·억·조는 네 자리로 끊는다. 이 둘을 섞으면 열 배가 틀린다.
  읽기 쉬운 단위 표기는 이 요약을 받는 프로그램이 따로 만들어 붙인다. 네 몫이 아니다.
- **(허용, 오히려 필수) 표 머리글의 단위는 값에 붙여 쓴다.**
  값이 `4,000` 이고 표 머리에 `(단위 : 억원)` 이 있으면 그 값은 `4,000억원` 이다.
  머리글을 무시하고 `4,000` 이라고만 쓰면 4천 원인지 4천억 원인지 알 수 없어 **틀린 값이 된다.**
  이건 자릿수를 다시 끊는 일이 아니라 원문을 읽는 일이다. 숫자 `4,000` 은 그대로 남는다.
  **이때 머리글 구절을 quote 에 함께 담아라.**
  같은 규칙이 `(단위 : 천원)` · `(단위 : 백만원)` 에도 적용된다.
- **(금지) 사칙연산을 하지 마라.** 증감액·증감률·지분 희석률·비중·환율·평균을 직접 계산하지 마라.
  두 숫자를 빼거나 더하거나 나눈 결과는 원문에 없는 숫자다.
  원문이 그 값을 따로 인쇄해 두었으면 그 숫자를 옮기고, 인쇄해 두지 않았으면 쓰지 않는다.
  예: 원문에 `이번 보고서 2,709,958` 과 `증 감 1,312,415` 만 있고 직전 보유량은 없다면,
  빼서 `1,397,543` 을 구하지 말고 직전 보유량을 아예 언급하지 않는다.
- 금액에는 숫자 뒤에 `원` 을 붙인다(`45,453,450,000,000원`). 주식 수에는 `주`,
  비율에는 `%` 를 붙인다. **원문에 그 단위가 명시돼 있을 때만** 붙인다.
  이 단위 표시는 프로그램이 금액과 주식 수를 구별하는 근거이므로 빠뜨리면 안 된다.
- 외화는 원문에 적힌 통화로 그대로 쓴다. 원화로 바꾸지 마라. 원문이 원화 환산액을
  따로 적어 두었으면 그 숫자를 옮긴다.
- 숫자가 사라져 설명이 밋밋해지는 것보다 틀린 숫자가 하나 들어가는 것이 훨씬 나쁘다.
  확신이 서지 않는 숫자는 쓰지 않는다.

## 3. 과장·투자 권유 절대 금지
너는 투자 자문을 하지 않는다. 사실과 그 의미만 전달한다.
- 금지 표현: "호재", "악재", "매수 기회", "매도 시점", "주가 상승이 기대된다",
  "긍정적 신호", "실적 개선이 예상된다", "저평가", "성장 동력 확보", "수혜",
  "주목할 만하다", "관심을 가질 필요가 있다".
- 주가·실적의 방향을 예측하거나 암시하지 않는다.
- 좋고 나쁨을 평가하지 않는다. "이 계약은 회사 연매출의 12%에 해당한다"는 되지만
  "이 계약은 회사에 큰 도움이 된다"는 안 된다.

## 4. 불확실한 내용을 단정하지 않는다
- 원문에 없는 사실·수치·배경·전망을 만들어 내지 않는다. 아는 지식으로 보충하지 않는다.
- 원문에 없으면 쓰지 않는다. 빈칸을 추측으로 채우지 말고 그냥 언급하지 않는다.
- 원문이 조건부·잠정이면 그 사실을 그대로 옮긴다.
  예: "이사회에서 결정했으며 주주총회 승인이 남아 있다", "잠정 수치다".
- 원문 정보가 부족해 설명이 짧아지는 것은 정상이다. 억지로 늘리지 마라.

## 5. 모든 수치·핵심 주장에 원문 근거를 붙인다
요약에 쓴 금액·비율·주식수·날짜·기간은 하나도 빠짐없이 evidence 배열에 근거를 남긴다.
- **요약 본문에 쓴 숫자는 evidence 의 quote 중 적어도 하나에 문자 그대로 들어 있어야 한다.**
  quote 에 없는 숫자를 본문에 쓰면 검증에서 걸린다. 이 규칙이 §2의 실질적 강제 장치다.
- quote 에는 원문 구절을 **한 글자도 바꾸지 말고 그대로 복사**한다.
  숫자의 쉼표·단위·띄어쓰기까지 원문 그대로여야 한다. 요약·의역·재작성은 위반이다.
- 원문에 없는 기호를 quote 에 덧붙이지 마라. 원문이 `17.33` 이면 `17.33%` 로 쓰지 않는다.
  단위 기호는 요약 본문(claim)에 붙이고, quote 는 원문 그대로 둔다.
- quote 는 원문의 **연속된 한 구간**이어야 한다. 떨어져 있는 두 곳(예: 표의 2행과 5행,
  항목 4번과 항목 6번)을 하나의 quote 로 이어 붙이지 마라.
  두 곳이 모두 필요하면 **evidence 항목을 두 개로 나눈다**.
- 원문에서 그대로 옮길 구절을 찾지 못했다면, 그 수치는 요약에 쓰지 마라.
- claim 에는 요약문에 실제로 쓴 표현을 그대로 옮긴다. 요약문에 없는 내용을 넣지 않는다.
- 근거는 요약문에 등장한 순서대로 담는다.

## 6. 중요도 판정
- high: 주가에 직접 영향이 큰 사안. 대규모 단일판매·공급계약, 유상증자·전환사채 발행,
  합병·분할·영업양수도, 대규모 시설투자·자산 처분, 실적의 급격한 변동,
  중대한 소송·행정제재, 최대주주 변경, 상장폐지·관리종목 관련.
- medium: 경영상 의미는 있으나 영향이 제한적인 사안. 소규모 계약, 자기주식 취득·처분,
  임원 변동, 정관 변경, 계열사 간 통상적 거래.
- low: 정기·형식적 보고, 소액 지분 변동, 정정 사항이 사소한 기재정정.
- 애매하면 낮은 쪽을 고른다. 중요도를 부풀리지 않는다.

## 형식
- 모든 출력은 한국어로 쓴다.
- one_line 은 200자 이내의 한 문장이다. 긴 금액은 프로그램이 읽기 쉬운 표기를 덧붙여
  더 길어지므로, one_line 에는 금액을 하나만 넣고 나머지는 easy_explanation 으로 넘긴다.
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
# 읽기 쉬운 금액 표기 병기 — LLM이 하던 환산을 코드가 대신한다
# ---------------------------------------------------------------------------
# 프롬프트 §2가 LLM에게 환산을 금지시키므로, 요약 본문에는 `45,453,450,000,000원` 처럼
# 원문 표기가 그대로 남는다. 회계를 모르는 일반인은 이 자릿수를 읽지 못한다(PLAN.md 1.4).
# 그 간극을 여기서 메운다.
#
# ## 왜 자유로운 정규식이 아니라 '단위 명사 앵커'인가
#
# 본문의 긴 숫자를 모두 환산하면 날짜(`20260713`)·사업자등록번호(`104-81-26688`)·
# 접수번호까지 금액으로 바꿔 버린다. 그래서 **`원`·`주` 로 끝나는 숫자만** 건드린다.
# 프롬프트 §2가 "금액에는 `원`, 주식 수에는 `주` 를 붙인다"를 요구하는 것과 한 쌍이다.
# 프롬프트가 앵커를 만들고 코드가 그 앵커만 신뢰한다 — 둘 중 하나만 바꾸면 안 된다.
#
# ## 왜 검증 이후에 붙이는가
#
# `validate_summary` 는 요약 본문의 수치를 evidence 인용문과 대조한다. 두 쪽 모두
# 원문 표기 그대로일 때 대조가 가장 정확하다. 병기는 판정이 끝난 뒤의 표시 단계다.
# 병기된 표기(`약 45조 4,534억`)를 나중에 `revalidate_summaries` 가 다시 읽어도,
# 절사 오차가 APPROX_TOLERANCE(5%) 안이라 판정이 뒤집히지 않는다.

#: 본문에서 금액·주식 수를 찾는 앵커. 4자리 이상의 수 또는 쉼표 세 자리 묶음 표기가
#: 곧바로 `원`/`주` 로 이어질 때만 잡는다.
#:
#: **이미 한국 단위가 붙은 표기는 건드리지 않는다.** 두 경로로 막힌다.
#:   1. `4,000억원` — 숫자 바로 뒤가 `억` 이라 앵커(`원`/`주`) 자체가 안 맞는다.
#:      표 머리글을 반영한 이 표기는 프롬프트 §2가 허용·요구하는 정상 출력이고,
#:      잡으면 `4,000억원(약 4,000억 원)` 이라는 동어반복이 된다.
#:   2. `2,882억 1,539만 6,500원` — 꼬리 `6,500원` 은 숫자 뒤가 `원` 이라 앵커에 걸린다.
#:      이건 금액 전체가 아니라 복합 표기의 **마지막 항**이므로 병기하면 안 된다.
#:      앞에 오는 단위를 lookbehind 로 막는다.
#:
#: 방어 장치별 이유. 하나라도 빼면 오염이 생기므로 각각 남긴다.
#:   `(?<![\d,.])`      숫자 중간에서 잘라 읽지 않는다.
#:   `(?<![경조억만천])`   위 2번 — `1,539만6,500원` 처럼 붙여 쓴 복합 표기의 꼬리를 막는다.
#:   `(?<![경조억만천] )`  위 2번 — `1,539만 6,500원` 처럼 띄어 쓴 경우.
#:                      파이썬 lookbehind 는 고정 폭이라 폭 1과 폭 2를 둘로 나눠 적는다.
#:   `(?!식)`           `1,132,477주식수` 를 주식 수로 오인하지 않는다.
#:   `(?!\s*\(약)`      멱등성 — 이미 병기된 표기를 다시 병기하지 않는다.
_AMOUNT_IN_TEXT = re.compile(
    r'(?<![\d,.])(?<![경조억만천])(?<![경조억만천] )'
    r'(\d{1,3}(?:,\d{3})+|\d{4,})\s*(원|주(?!식))(?!\s*\(약)'
)


def _amount_replacement(match):
    """`45,453,450,000,000원` → `45,453,450,000,000원(약 45조 4,534억 원)`."""
    value = int(match.group(1).replace(',', ''))
    if value < AMOUNT_NOTATION_MIN:
        return match.group(0)  # 쉼표만으로 읽히는 크기다. 괄호를 붙이면 오히려 지저분하다.
    unit = match.group(2)
    notation = (
        format_korean_won(value) if unit == '원'
        else f'{format_korean_amount(value)} {unit}'
    )
    return f'{match.group(0)}({notation})'


def annotate_amounts(result):
    """검증을 마친 요약 dict의 본문 3필드에 읽기 쉬운 금액 표기를 병기한 새 dict를 만든다.

    원본 dict는 건드리지 않는다. 호출자는 반환값을 저장한다.

    one_line 만 예외를 둔다. 병기로 200자(ONE_LINE_MAX_CHARS)를 넘기면 모델 필드
    max_length 를 초과해 저장이 깨지므로, 그 경우 one_line 은 병기하지 않고 원문 표기로
    남긴다. **읽기 편함보다 저장 성공이 우선**이다 — 그 문장의 금액은 easy_explanation
    에서 다시 병기되어 나온다.
    """
    annotated = dict(result)
    for field in ('one_line', 'easy_explanation', 'why_important'):
        text = annotated.get(field) or ''
        replaced = _AMOUNT_IN_TEXT.sub(_amount_replacement, text)
        if field == 'one_line' and len(replaced) > ONE_LINE_MAX_CHARS:
            continue
        annotated[field] = replaced
    return annotated


# ---------------------------------------------------------------------------
# 재생성(자동 교정) 프롬프트 — 검증 경고를 되먹인다
# ---------------------------------------------------------------------------
# ## 왜 별도 프롬프트가 아니라 같은 대화의 연장인가
#
# 비용이 결정했다. 교정 요청은 (시스템 프롬프트 + 원문을 담은 user 메시지)를 **바이트
# 단위로 동일하게** 유지한 뒤 assistant·user 턴만 덧붙인다. 그래야 프롬프트 캐시가
# 접두사 전체에 적중한다(캐시 입력 단가는 미캐시의 1/10). 시스템 프롬프트를 갈아 끼운
# 별도 교정 프롬프트를 쓰면 접두사가 깨져 원문 토큰 값을 통째로 다시 낸다
# — 사업보고서(약 21만 토큰) 기준 건당 $0.21 대 $0.021 의 차이다.
# 덧붙는 것은 직전 출력(약 700토큰)과 아래 지시문뿐이라 재생성의 실질 증분은 작다.
#
# ## 직전 출력을 보여 주는 것의 득실
#
# 보여 준다. 경고 문구(`인용 근거 없는 수치: 3조 9,891억`)만으로는 모델이 자기가 어디에
# 그 값을 썼는지 알 수 없다. 오답에 끌려갈 위험은 있지만, 여기서는 오히려 **최소 수정**이
# 목표다 — 맞게 쓴 문장까지 다시 쓰면 새 오류가 생긴다. 그래서 지시문이 "지적된 곳만
# 고치고 나머지는 그대로 두라"고 못 박고, 동시에 "지적된 부분의 표현은 반복하지 말라"고
# 따로 경계한다. 끌려가면 안 되는 범위를 좁혀서 지정하는 것이다.
#
# ## 경고를 그대로 넣지 않는 이유
#
# 저장된 경고는 **진단문**이지 **지시문**이 아니다. "인용 근거 없는 수치: 3조 9,891억"은
# 무엇이 틀렸는지는 말하지만 무엇을 하라는 말이 없다. 경고 종류(접두어)마다 조치문을
# 짝지어 붙인다. 접두어는 verification.py 가 이미 상수로 고정해 둔 단일 출처라
# 문자열 매칭이 아니라 그 상수로 잇는다.

#: 경고 접두어별 조치문. 키는 verification.py 의 접두어 상수를 그대로 쓴다.
CORRECTION_ACTIONS = {
    UNSUPPORTED_NUMBER_PREFIX: (
        '이 수치가 요약 본문에 있으나 evidence 의 quote 어디에도 같은 값이 없다. '
        '원인은 대개 둘 중 하나다. '
        '(가) 원문 숫자의 자릿수를 만·억·조로 다시 끊어 썼다 — 원문에 인쇄된 표기 그대로 '
        '되돌리고, 그 표기를 담은 quote 를 붙여라. '
        '(나) 원문에 없는 값을 계산해 만들었다(증감액·증감률·비중·환율 등) '
        '— 그 수치와, 그 수치에만 기대는 문장을 지워라.'
    ),
    UNVERIFIED_QUOTE_PREFIX: (
        '이 quote 를 원문에서 찾지 못했다. 원문의 **연속된 한 구간**을 다시 찾아 '
        '한 글자도 바꾸지 말고 복사하라. 떨어진 두 곳을 이어 붙였다면 evidence 항목을 '
        '둘로 나눠라. 원문에 없는 기호(%, 원, 주)를 덧붙였다면 지워라. '
        '그래도 못 찾으면 그 근거와 그 근거에만 기대는 문장을 삭제하라.'
    ),
    SENTENCE_COUNT_PREFIX: (
        'easy_explanation 의 문장 수만 맞춰라. 내용을 새로 지어내 늘리지 말고, '
        '길면 문장을 합치고 짧으면 원문에 이미 있는 사실로만 채워라.'
    ),
}

#: 접두어가 어느 것과도 안 맞는 경고에 붙일 기본 조치문.
DEFAULT_CORRECTION_ACTION = (
    '지적된 내용을 원문에 맞게 고쳐라. 원문으로 뒷받침할 수 없으면 해당 부분을 삭제하라.'
)

CORRECTION_HEADER = """\
직전 출력이 자동 검증을 통과하지 못했다. 아래 지적 사항만 고쳐 전체 JSON을 다시 출력하라.

- 지적되지 않은 문장과 근거는 한 글자도 바꾸지 마라. 다시 쓰지 말고 그대로 옮겨라.
- 지적된 부분은 직전 표현을 반복하지 말고 원문을 다시 읽어 고쳐라.
- 원문으로 고칠 방법이 없으면 그 수치와 문장을 **지워라**. 숫자가 빠진 요약은 통과하지만
  틀린 숫자가 든 요약은 통과하지 못한다.
- 출력 형식은 직전과 동일한 JSON 스키마다.

[지적 사항]
"""


def build_correction_message(warnings):
    """검증 경고 목록 → 재생성 요청 user 메시지.

    각 경고에 접두어로 찾은 조치문을 짝지어 붙인다. 경고가 비어 있으면 None 을 돌려
    호출자가 재생성 자체를 건너뛰게 한다(고칠 것이 없는데 호출하면 비용만 나간다).
    """
    items = [warning for warning in (warnings or []) if warning and warning.strip()]
    if not items:
        return None
    lines = [CORRECTION_HEADER]
    for idx, warning in enumerate(items, 1):
        action = next(
            (act for prefix, act in CORRECTION_ACTIONS.items() if warning.startswith(prefix)),
            DEFAULT_CORRECTION_ACTION,
        )
        lines.append(f'{idx}. {warning}\n   조치: {action}')
    return '\n'.join(lines)


def schema_only(data):
    """모델에게 되돌려 줄 직전 출력에서 스키마 밖 필드를 걷어낸다.

    `validate_summary` 결과에는 `quote_found` · `numbers_ok` · `warnings` 처럼 우리가
    덧붙인 진단 필드가 섞여 있다. 그대로 되먹이면 모델이 그 형태를 따라 출력하려 해
    strict 스키마와 어긋난다. 직전 턴은 **스키마에 정확히 맞는 모양**이어야 다음 출력도
    같은 모양으로 나온다.
    """
    return {
        'one_line': data.get('one_line', ''),
        'easy_explanation': data.get('easy_explanation', ''),
        'why_important': data.get('why_important', ''),
        'importance': data.get('importance', ''),
        'evidence': [
            {
                'field': item.get('field', ''),
                'claim': item.get('claim', ''),
                'quote': item.get('quote', ''),
            }
            for item in (data.get('evidence') or [])
        ],
    }


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
                         max_retries=MAX_RETRIES, max_input_tokens=MAX_INPUT_TOKENS,
                         correction_warnings=None, correction_previous=None):
    """공시 1건을 요약해 검증된 dict를 반환한다.

    raw_text 는 **전처리를 마친** 원문이어야 한다(XML 태그 제거·표 텍스트화·상용구 제거).
    이 함수는 전처리를 하지 않는다.

    `correction_warnings` 를 주면 **자동 교정 재생성**이 된다.

      correction_warnings: 검증 경고 문자열 목록(`build_review_warnings` 결과).
      correction_previous: 직전 요약 dict. **함께 넘기는 것을 강하게 권한다.**
        없어도 동작하지만, 없으면 모델이 "인용 근거 없는 수치: 3조 9,891억" 이라는 지적을
        받고도 자기가 그 값을 어디에 썼는지 알 수 없어 교정이 아니라 재작성이 된다.

    이때도 시스템 프롬프트와 user 메시지는 최초 호출과 바이트 단위로 같게 유지하고
    assistant·user 턴만 뒤에 덧붙인다. 캐시 접두사 동일성을 호출자가 아니라 이 함수가
    책임지기 위해서다 — 호출자가 user 메시지를 다시 조립하면 캐시가 조용히 깨지고
    입력 토큰을 전액 다시 낸다. 재생성 횟수 상한은 호출자가 관리한다
    (`review_policy.MAX_REGENERATION_ATTEMPTS`). 이 함수는 한 번의 교정 요청만 수행한다.
    경고가 비어 있으면 교정 턴을 붙이지 않고 최초 호출과 동일하게 동작한다.

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
          'corrected': bool,          # 교정 재생성으로 만들어진 요약인가
        }

    본문 3필드에는 `annotate_amounts` 로 읽기 쉬운 금액 표기가 병기돼 있다
    (`45,453,450,000,000원(약 45조 4,534억 원)`). 검증은 병기 **전** 텍스트로 끝낸 뒤라
    `warnings`·`unsupported_numbers` 는 모델이 실제로 쓴 표기를 기준으로 판정된 값이다.

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

    # 교정 턴은 캐시 접두사 **뒤**에 붙는다. 위 두 메시지를 손대면 캐시가 깨지므로
    # 여기서 append 만 한다.
    correction_message = build_correction_message(correction_warnings)
    if correction_message:
        # 직전 출력이 없으면 assistant 턴을 만들지 않는다. 빈 JSON을 넣으면 모델이
        # 그 빈 출력을 고쳐야 할 대상으로 오해한다.
        if correction_previous:
            messages.append({
                'role': 'assistant',
                'content': json.dumps(
                    schema_only(correction_previous), ensure_ascii=False
                ),
            })
        messages.append({'role': 'user', 'content': correction_message})

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

            # 검증은 모델이 쓴 표기 그대로 수행한다. 금액 병기는 판정이 끝난 뒤에 붙인다.
            result = annotate_amounts(validate_summary(data, raw_text))
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
            result['corrected'] = bool(correction_message)
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
