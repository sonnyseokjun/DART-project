# 21 · backend Phase B — 검증 로직 분리 · 한국 단위 환산

브랜치: `feature/stage4-review-workflow` (작업 시점 기준)
범위: Phase B 세 항목 중 **1·2 완료, 3은 analyst 대기**. Phase C는 손대지 않았다.

## 0. 한눈에

| 항목 | 상태 | 판정 근거 |
|---|---|---|
| 1. `verification.py` 분리 | 완료 | `test disclosures` **209건 전원 통과**, `tests.py` 무수정 |
| 2. `units.py` 신규 | 완료 | 실제 오류 3건의 정답값 재현, 경계값 18종 왕복 일치 |
| 3. 표 헤더 단위 인식 | **미착수 (analyst 대기)** | `_workspace/10_analyst_number_triage.md` 미생성 |

동작 변경이 없음을 증명하는 가장 강한 근거는 테스트가 아니라 실데이터다.
`revalidate_summaries --dry-run` 을 실데이터 140건에 돌린 결과 **판정이 바뀐 요약 0건**이다.

---

## 1. `summarizer.py` → `verification.py` 분리

### 1.1 옮긴 것

`summarizer.py` L400~L775 (검증 섹션 전체)를 **한 글자도 고치지 않고** 옮겼다.
로직 개선·이름 변경·주석 정리를 일절 하지 않았다. 리팩터와 기능 변경을 섞으면
나중에 무엇이 깨졌는지 못 찾는다.

| 종류 | 이름 |
|---|---|
| 상수 | `ONE_LINE_MAX_CHARS` · `EXPLANATION_MIN_SENTENCES` · `EXPLANATION_MAX_SENTENCES` · `APPROX_TOLERANCE` · `ORDINAL_MAX` · `SCALE_TOLERANCE` · `MIN_FRAGMENT_CHARS` |
| 경고 접두어 | `UNSUPPORTED_NUMBER_PREFIX` · `UNVERIFIED_QUOTE_PREFIX` · `SENTENCE_COUNT_PREFIX` · `ACCURACY_WARNING_PREFIXES` · `WARNING_LIST_SEPARATOR` |
| 비공개 정규식·헬퍼 | `_SENTENCE_END` `_WHITESPACE` `_NUMBER` `_UNIT_MULTIPLIER` `_APPROX` `_YEAR` `_SCALE_MULTIPLIERS` `_FRAGMENT_SPLIT` `_CONTENT_ONLY` `_normalize` `_value_supported` `_content_only` |
| 공개 함수 | `extract_numbers` · `is_reference_number` · `extract_comparable_numbers` · `quote_fragments` · `verify_quote` · `count_sentences` · `verify_evidence` · `validate_summary` · `build_review_warnings` |

`summarizer.py`에는 프롬프트·JSON 스키마·예외·토큰/비용 추정·OpenAI 클라이언트만 남았다.
955행 → 604행. `import re` 는 쓰이지 않게 되어 제거했다(검증 정규식이 전부 이동).

### 1.2 재수출 — 하위 호환

`summarizer.py` 상단에서 위 이름을 전부 `from .verification import ...  # noqa: F401` 로
되받는다. **비공개 `_value_supported` 도 포함**시켰다 — `tests.py:1070` 이
`summarizer._value_supported(...)` 를 직접 호출하고 있어서, 빼면 테스트를 고쳐야 했다.
테스트를 고쳐야 통과하는 분리는 순수 이동이 아니다.

`from .summarizer import ...` 를 쓰는 곳 전수 확인 결과(`grep`):

| 위치 | 처리 |
|---|---|
| `models.py:165` `ACCURACY_WARNING_PREFIXES` | **`.verification` 로 변경** |
| `models.py:182` `UNSUPPORTED_NUMBER_PREFIX`, `WARNING_LIST_SEPARATOR` | **`.verification` 로 변경** |
| `management/commands/revalidate_summaries.py:19` `build_review_warnings`, `validate_summary` | 그대로 둠 (재수출로 동작) |
| `management/commands/summarize_disclosures.py:20` | 그대로 둠 (재수출로 동작) |
| `tests.py` `summarizer.*` 30여 곳 | 그대로 둠 (재수출로 동작, **파일 무수정**) |

### 1.3 판단이 갈렸던 지점

**(a) `validate_summary` 가 던지는 `SummaryValidationError` 를 어디에 둘 것인가 — 순환 import**

`summarizer` 가 `verification` 을 재수출하려면 모듈 최상단에서 import 해야 한다.
그런데 `validate_summary` 는 `SummaryValidationError` 를 던지는데 그 예외는
`SummarizerError` 계층의 일부라 `summarizer.py` 에 있어야 자연스럽다. 양방향 최상단
import 는 **어느 모듈을 먼저 import 하느냐에 따라 터진다**(`verification` 을 먼저
import 하면 `summarizer` 실행 중 부분 초기화된 `verification` 을 되받아 ImportError).

세 안을 놓고 골랐다.

| 안 | 문제 |
|---|---|
| A. 양쪽 최상단 import | 위와 같이 import 순서에 따라 깨진다. 채택 불가 |
| B. 예외 계층을 `exceptions.py` 신규 모듈로 | 가장 깔끔하지만 **명세에 없는 세 번째 신규 파일**이고, `summarizer.py`(ai-prompt 소유)에서 예외 정의를 들어내는 큰 변경이 된다 |
| C. `validate_summary` 안에서 지연 import | **채택.** 1줄, 위험 0, 의존 방향은 `summarizer → verification` 단방향으로 유지 |

C를 고른 결정적 이유는 **이 프로젝트에 이미 같은 관용이 있다**는 점이다.
`models.py` 의 `accuracy_warnings`·`unsupported_numbers` 가 똑같이 함수 안에서 import 한다.
없던 패턴을 들여오는 게 아니라 있는 패턴을 따랐다.

Phase C에서 예외를 더 늘려야 한다면 그때 B(`exceptions.py`)로 옮기는 것이 맞다. 지금은 이르다.

**(b) `models.py` 의 import 를 바꿀 것인가**

바꿨다. 재수출이 있으니 안 바꿔도 동작하지만, `models.py` 가 `summarizer` 를 import 하면
**모델이 LLM 클라이언트 모듈에 의존**하게 된다. `verification.py` 는 Django도 OpenAI도
안 쓰는 순수 문자열 모듈이라 모델이 기대도 좋은 대상이다. 이번 분리의 목적 자체가
소유권 분리인데 models(backend)가 summarizer(ai-prompt)를 계속 가리키면 절반만 한 셈이다.

**(c) 관리 명령 2개의 import 는 왜 안 바꿨나**

일부러 남겼다. 재수출 경로가 **실제로 살아 있는지 테스트가 매번 확인**하게 하려는 것이다.
전부 새 경로로 바꾸면 재수출은 아무도 안 쓰는 코드가 되고, 조용히 깨져도 모른다.
`revalidate_summaries` 는 실데이터 140건을 태우는 경로라 회귀 감지에 특히 좋다.
일관성이 없어 보이는 건 인정한다 — 대신 `summarizer.py` 상단 주석에
"새 코드는 verification 에서 직접 가져오라"고 명시했다.

### 1.4 검증 결과 (실행 그대로)

```
$ .\venv\Scripts\python.exe manage.py test disclosures
Ran 209 tests in 101.165s

OK

$ .\venv\Scripts\python.exe manage.py check
System check identified no issues (0 silenced).

$ .\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
No changes detected

$ .\venv\Scripts\python.exe manage.py revalidate_summaries --dry-run
재검증 대상 140건 (LLM 호출 없음)

  경고 있는 요약 : 32건 → 32건 (전체 140건)
  경고 비율      : 23% → 23%
  판정이 바뀐 요약: 0건
```

`tests.py` 는 손대지 않았다(`git status` 기준 변경 파일: `models.py`, `summarizer.py` + 신규 2개).
줄바꿈은 체크아웃 전체가 CRLF라 신규 파일도 CRLF로 맞췄다.

---

## 2. `units.py` — 한국 단위 환산

### 2.1 API

```python
UNIT_EXPONENTS = ((16, '경'), (12, '조'), (8, '억'), (4, '만'), (0, ''))
UNIT_STEP = 4
DEFAULT_MAX_UNITS = 2
APPROX_MARK = '약 '
WON_SUFFIX = '원'

split_units(value: int) -> list[tuple[int, int]]
    # 45453450000000 → [(12, 45), (8, 4534), (4, 5000)]   계수 0인 자리는 뺀다
    # 음수·float·bool 은 ValueError/TypeError

format_korean_amount(value: int, *, max_units=2, approx_mark='약 ') -> str
    # 45453450000000 → '약 45조 4,534억'
    # max_units=None → '45조 4,534억 5,000만'  (절사 없음)

format_korean_won(value: int, *, max_units=2, approx_mark='약 ') -> str
    # 45453450000000 → '약 45조 4,534억 원'

parse_korean_amount(text: str) -> int | None
    # '약 45조 4,534억 5,000만 원' → 45453450000000
    # '39,890,534,790,000'        → 39890534790000
    # '계약금액은 3조 원이다'      → None  (통짜 금액 표기가 아님)
```

Django import 없음. 전역 상태 없음. `float` 없음(전부 `int` 연산 — 39조를 float로 다루면
`2**53 ≈ 9,007조` 경계에 붙어 유효자리가 위태롭다).
`sys.path` 에 `disclosures/` 만 넣고 Django 미로드 상태로 import 되는 것을 실제로 확인했다.

### 2.2 표기 정책 — 왜 이렇게 정했나

**(a) 절사 기준을 '항 개수'가 아니라 '자릿수 폭'으로 잡았다**

`max_units=n` 은 *최상위 단위에서 아래로 4n자리까지 쓰고 버린다*는 뜻이다.
처음엔 "0이 아닌 항 n개"로 만들었는데 경계값에서 깨졌다.
`10,000,000,000,001`(10조 1)은 0인 자리가 다 생략돼 항이 2개뿐이라 `10조 1` 이 그대로
살아남는다. `10조 1 원`은 사람이 쓰는 말이 아니다. 폭 기준으로 바꾸니 `약 10조 원`이 된다.
반대로 `1,000,050,000`(10억 5만)처럼 원래 정보가 적은 값은 폭 안에 다 들어와
**절사 없이 정확한 표기**가 나온다. 표기가 짧아지는 건 값이 단순해서지 잘라서가 아니다.

**(b) 기본 `max_units=2`**

- 독자는 회계를 배운 적 없는 일반인이다(PLAN.md 1.4). `45조 4,534억 5,000만 원`은
  한 호흡에 안 읽힌다.
- 그렇다고 `약 45조`(1단위)로 끊으면 4,534억이 통째로 사라진다. 45조와 45.5조는
  일반인에게도 다른 크기다.
- 상위 2단위 ≈ 유효 8자리. 정확한 값이 필요한 검수 화면·원문 대조는 `max_units=None`.
- 표기를 줄인 자리에는 언제나 DART 원문 링크가 병기된다(CLAUDE.md 컨벤션).

**(c) 반올림이 아니라 버림**

이게 판단이 제일 갈렸던 지점이다. `약`을 붙일 거면 반올림이 더 정확해 보인다.
그럼에도 버림을 골랐다.

1. 반올림은 **원문에 없는 숫자를 만든다.** `39조 8,905억 3,479만`을 억에서 반올림했을 때
   3,479만이 5,000만을 넘겼다면 `8,906`이 나온다. 원문 어디에도 `8906`은 없다.
   검증기는 요약 수치를 원문 수치와 대조하는데, 그 숫자는 잡을 근거가 없다.
   버림은 **항상 원문의 앞자리를 그대로 보존**한다. Phase C에서 코드 환산값과 원문을
   접두 비교로 대조할 길이 열린다.
2. 버림은 금액을 부풀리지 않는다. 과장 금지(프롬프트 §2)와 방향이 같다.
3. 버린 자리가 있으면 `약`을 붙이므로 근사임이 표기에 남는다.

**(d) 역방향 파서를 넣었다 — 단, 통짜 일치만**

`verification.extract_numbers` 와 겹쳐 보이지만 용도가 반대다.
`extract_numbers` 는 자유 문장에서 오탐을 감수하고 **넓게** 긁는다.
`parse_korean_amount` 는 금액 표기 하나를 **통짜로** 받고 아니면 `None` 을 준다
(`'계약금액은 3조 원이다'` → `None`). 좁게 잡는 이유는 용도가 "코드가 만든 표기를 되읽어
값이 보존됐는지 확인"하는 것이기 때문이다. 넓게 잡으면 그 확인이 무의미해진다.
왕복 일치(`parse(format(v, max_units=None)) == v`)를 경계값 전건에서 확인했고, 이건
**표기 로직의 자기 검산**이라 회귀 안전망으로 값이 크다. qa가 property 테스트로 쓰기 좋다.

쉼표는 자릿수 구분자로만 보고 무시한다. 그래서 원문의 `39,890,534,790,000`과
표기의 `4,534억`을 같은 규칙으로 읽는다 — 이번 버그의 원인이 정확히 이 두 규칙의 혼동이었다.

### 2.3 경계값 확인 결과

스크래치패드 스크립트로 확인했다(테스트는 qa가 `tests.py`에 쓴다).
`C:\Users\김석준\AppData\Local\Temp\claude\C--Users-----Desktop-DART-project\a9cecf84-9074-45db-9a97-c2205f832bab\scratchpad\check_units.py`

| 입력 | `max_units=2` (기본) | `max_units=None` (정확) | 왕복 |
|---|---|---|---|
| `0` | 0 원 | 0 원 | OK |
| `1` | 1 원 | 1 원 | OK |
| `9,999` | 9,999 원 | 9,999 원 | OK |
| `10,000` | 1만 원 | 1만 원 | OK |
| `10,001` | 1만 1 원 | 1만 1 원 | OK |
| `12,345` | 1만 2,345 원 | 1만 2,345 원 | OK |
| `100,000,000` | 1억 원 | 1억 원 | OK |
| `1,000,000,000,000` | 1조 원 | 1조 원 | OK |
| `10,000,000,000,000` | 10조 원 | 10조 원 | OK |
| `10,000,000,000,001` | 약 10조 원 | 10조 1 원 | OK |
| `100,000,000,000,001` | 약 100조 원 | 100조 1 원 | OK |
| `1,000,050,000` | 10억 5만 원 | 10억 5만 원 | OK |
| **`39,890,534,790,000`** | **약 39조 8,905억 원** | 39조 8,905억 3,479만 원 | OK |
| **`45,453,450,000,000`** | **약 45조 4,534억 원** | 45조 4,534억 5,000만 원 | OK |
| **`43,140,750,000,000`** | **약 43조 1,407억 원** | 43조 1,407억 5,000만 원 | OK |
| `400,000,000,000` | 4,000억 원 | 4,000억 원 | OK |
| `-39,890,534,790,000` | 약 -39조 8,905억 원 | -39조 8,905억 3,479만 원 | OK |
| `123,456,789,012,345,678,901` | 약 12,345경 6,789조 원 | 12,345경 6,789조 123억 4,567만 8,901 원 | OK |

**굵은 세 줄이 이 작업의 표적이다.** 00_input.md 2.1의 '실제 값' 열과 정확히 일치한다
(39조 8,905억 · 45조 4,534억 · 43조 1,407억).

AI가 냈던 틀린 표기를 같은 파서로 되읽어 배수를 확인했다:

| AI 출력 | 되읽은 값 | 코드 표기 | 배수 |
|---|---|---|---|
| 3조 9,891억 | 3,989,100,000,000 | 약 39조 8,905억 | **10.0** |
| 4조 5,453억 | 4,545,300,000,000 | 약 45조 4,534억 | **10.0** |
| 4조 3,140억 | 4,314,000,000,000 | 약 43조 1,407억 | **10.0** |

정확히 10배. 진단이 맞았다.

기타 확인:
- `split_units(-1)` → `ValueError`, `split_units(1.0)` → `TypeError`, `max_units=0` → `ValueError`
- `parse_korean_amount` 거부: `'계약금액은 3조 원이다'` · `''` · `'원'` · `'abc'` ·
  `'3조 4,000억원어치'` · `None` → 전부 `None`
- `parse_korean_amount` 허용: `'4,000억원'`=4e11 · `'3조9891억'`(공백 없음) ·
  `'-1억'` · `'약 39조 8,905억 원'`
- 절사 표기를 되읽으면 **항상 원래 값 이하**임을 전건 확인(버림 정책의 불변식)

**경(10^16)을 넣은 이유**는 표기를 위해서가 아니라 `12,345조` 같은 어색한 출력을 막기
위해서다. 실무 금액은 경에 닿지 않지만, 알고리즘이 최상위에서 무너지지 않는다는 확인은
공짜다.

---

## 3. 표 헤더 단위 인식 — **미착수 (analyst 대기)**

`_workspace/10_analyst_number_triage.md` 가 아직 없다. 추측으로 구현하지 않았다.
**적용 범위를 잘못 잡으면 오탐을 다른 오탐으로 바꿀 뿐**이라는 리더 지시를 그대로 따랐다.
analyst-triage 에게 진행 상황과 필요한 3가지(표기 변형·적용 범위 규칙·위험 사례)를
메시지로 요청해 두었다.

착수 전에 현재 코드를 읽고 확인한 사실 하나를 남긴다 — **analyst 조사에 영향을 준다.**

`verification._value_supported` 는 헤더를 파싱하지 않는 대신
`_SCALE_MULTIPLIERS = (10^3, 10^4, 10^6, 10^8, 10^12)` 배수 일치를 `SCALE_TOLERANCE=1%`로
**무조건 허용**하고 있다. 즉 지금 구조는 *천·만·백만·억·조 배수 오차는 전부 통과시키고
그 외(10배·100배)만 잡는다.* 실제 오류 3건이 정확히 10배라서 걸린 것이고,
바꿔 말하면 **AI가 1,000배 틀려도 지금 검증기는 못 잡는다.**

그러면 id 8(`(단위 : 억원)` + `4,000` → `4,000억원`) 오탐은 왜 났는가?
억(10^8)은 `_SCALE_MULTIPLIERS` 에 있으므로 값 대조만으로는 통과했어야 한다.
**헤더 미인식 말고 다른 원인이 섞여 있을 가능성**이 있다(인용문에 `4,000`이 아예 없었거나,
`extract_numbers` 가 `4,000억원`을 다른 값으로 읽었거나). analyst에게 그 건의 원문·claim
대조 근거를 보고서에 남겨 달라고 요청했다.

**따라서 3번 작업은 헤더 파싱 추가만으로 끝나지 않을 수 있고, `_SCALE_MULTIPLIERS` 라는
'전부 허용' 완화를 좁히는 일과 짝이어야 한다.** 헤더로 정확한 배수를 알아내면 그 배수만
허용하고 나머지를 막을 수 있다 — 오탐을 줄이면서 동시에 탐지력을 올리는 유일한 길이다.
지금 상태로 헤더 인식만 얹으면 허용 범위가 더 넓어져 진짜 오류를 놓치게 된다.

**경고 감소 수치는 측정하지 못했다.** 현재 기준선만 기록한다: 140건 중 경고 보유 **32건**
(분리 전후 동일, 판정 변화 0건).

---

## 4. 다른 에이전트가 알아야 할 인터페이스

### ai-prompt 에게

- **`summarizer.py` 는 이제 열려 있다.** 프롬프트·JSON 스키마·클라이언트·비용 추정만 남았고
  604행이다. 검증 로직은 없으니 마음껏 고쳐도 된다.
- 다만 **상단의 `from .verification import (...)` 재수출 블록은 지우지 마라.**
  `tests.py`·관리 명령 2개가 이 경로로 붙어 있다. 이름을 추가할 일은 있어도 뺄 일은 없다.
- `ONE_LINE_MAX_CHARS` 는 `verification.py` 로 옮겼지만 재수출되므로
  `SUMMARY_JSON_SCHEMA` 에서 그대로 쓸 수 있다. 값을 바꿔야 하면 `verification.py` 에서
  바꿔라(모델 `max_length=200` 과 3자 일치가 테스트로 고정돼 있다).
- 프롬프트에서 **단위 환산을 금지**할 때, 코드가 대신 만들 표기는 이것이다:
  `units.format_korean_won(39890534790000)` → `'약 39조 8,905억 원'`.
  프롬프트에 예시로 쓸 문자열이 필요하면 이 함수 출력 형식을 그대로 참조하라.
  현재 시스템 프롬프트 §1의 `"금액은 읽기 쉬운 단위로 환산해 병기한다"` 가 정확히
  이번 버그를 지시하고 있는 문장이다.
- `EXPLANATION_MIN/MAX_SENTENCES` 도 `verification.py` 소유다. 프롬프트의 "3~5문장"과
  이 상수가 어긋나면 문체 경고가 상시로 뜬다.

### frontend 에게

- **지금 당장 바뀌는 건 없다.** 템플릿이 쓰는 `DisclosureSummary.accuracy_warnings` ·
  `unsupported_numbers` 는 시그니처·반환 형식 그대로다(내부 import 출처만 바뀜).
- 앞으로 쓰게 될 것: `disclosures.units.format_korean_won(value)` → 문자열.
  Django 의존이 없으니 템플릿 필터로 감싸기 쉽다(`templatetags/` 는 frontend 소유).
  코드 환산값과 AI 원문 인용을 구분해 보여주기로 하면 이 함수가 그 '코드 환산값' 쪽이다.
- 절사가 일어난 표기는 앞에 `약 ` 이 붙는다. 정확한 값이 필요하면 `max_units=None`.
  즉 **같은 값에 대해 짧은 표기와 정확한 표기 두 가지를 만들 수 있다** — 카드에는 짧게,
  툴팁·상세에는 정확하게 같은 식이 가능하다.

### qa 에게

- `units.py` 는 Django 없이 import 된다. `tests.py` 안에서 `from disclosures import units`
  로 그냥 쓰면 된다.
- 회귀 안전망으로 권하는 것: **왕복 property** —
  임의의 `v >= 0` 에 대해 `parse_korean_amount(format_korean_amount(v, max_units=None)) == v`,
  그리고 절사 표기에 대해 `parse(...) <= v` (버림 정책의 불변식).
- 위 2.3 표의 18종 + 3건의 실제 값이 그대로 경계값 케이스다.
- `verification.py` 의 공개 함수를 직접 테스트해도 되고, 재수출 경로(`summarizer.*`)를
  테스트해도 된다. **둘 다 하나씩은 남겨 두길 권한다** — 재수출이 조용히 깨지는 걸 막는다.

---

## 5. 남은 일

1. **표 헤더 단위 인식** — analyst 보고서 나오는 대로 착수. 위 3장의 `_SCALE_MULTIPLIERS`
   문제와 묶어서 판단해야 한다.
2. `revalidate_summaries` 로 경고 감소 측정 — 1번 이후.
3. Phase C 전체 (게시 차단 · 재생성 루프 · 검수 게이트 유형 기준) — 이번 범위 아님.
4. `units.py` 를 실제 파이프라인에 연결하는 일은 아직 안 했다. 지금은 **아무도 부르지 않는
   순수 모듈**이다. 호출 지점은 Phase C에서 프롬프트 변경과 함께 정해야 한다
   (LLM이 원문 숫자를 그대로 인용하게 된 뒤라야 환산할 대상이 생긴다).
