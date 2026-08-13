# 11 · backend — poll_dart 백필 기능 (임의 구간 + 3개월 한도 자동 분할)

- 작업 범위: `_workspace/00_input.md` §4 백필 기능
- 브랜치: `feature/stage2-ai-summary` (커밋하지 않음)
- 모델·마이그레이션 미변경 (`disclosures/models.py` 무수정, `makemigrations`/`migrate` 미실행)

## 1. 변경 파일

| 파일 | 변경 내용 |
|---|---|
| `disclosures/dart.py` | `MAX_LIST_SPAN_DAYS = 89` 상수 + `split_date_range(bgn, end, max_span_days)` 추가. `timedelta` import |
| `disclosures/management/commands/poll_dart.py` | `--bgn`/`--end` 옵션, 입력 검증(`_resolve_range`·`_parse_date`), 청크 순회, 호출량 경고(`_warn_if_call_heavy`), 수집 본문을 `_collect`로 분리 |
| `disclosures/tests.py` | `_RecordingIter` 대역 + `SplitDateRangeTest`(6건)·`PollDartBackfillTest`(13건) 추가 |
| `PLAN.md` | 4.4 장애 복구 서술에 3개월 한도·청크 순회 명시. 12.2에 전략의 전제로 3개월 한도 + "전략 유효, 백필만 예외" 명시 |
| `.claude/skills/dart-api-know-how/SKILL.md` | "호출량 규칙"에 `list.json` 검색기간 3개월 제한 절 추가(실측 경계·오류 코드 100·대응 절차) |
| `CLAUDE.md` | 명령어 목록에 백필 사용례 1줄 추가 |

## 2. CLI 인터페이스

```
poll_dart [--days N]                  # 오늘부터 N일 전까지 (기본 3)
poll_dart --bgn YYYYMMDD [--end YYYYMMDD]   # 임의 구간, --end 생략 시 오늘
```

`--days`의 기본값을 `3` → `None`으로 바꿔 "명시적으로 준 것"과 "안 준 것"을 구분한다.
기본값이 남아 있으면 `--bgn`과의 상호 배타 판정이 불가능하다. 미지정 시 `DEFAULT_DAYS=3`이 적용돼 기존 동작은 그대로다.

검증 규칙 (모두 `CommandError`):

| 입력 | 결과 |
|---|---|
| `--days`와 `--bgn`/`--end` 동시 | `--days와 --bgn/--end는 함께 지정할 수 없습니다.` |
| `--end`만 (`--bgn` 없음) | `--end만으로는 구간을 정할 수 없습니다.` |
| 날짜 형식 불량 (`2026-07-01`, `20260732`, `202607`) | `--bgn 날짜 형식이 잘못되었습니다: ... (YYYYMMDD)` |
| `bgn > end` | `시작일(...)이 종료일(...)보다 늦습니다.` |
| `end`가 미래 | `종료일(...)이 미래입니다. 오늘(...) 이후는 조회할 수 없습니다.` |
| `--days` 음수 | `--days는 0 이상이어야 합니다.` |

검증은 추적 기업 조회보다 **먼저** 수행한다 — 잘못된 입력은 DB·DART에 닿기 전에 걸러진다.

## 3. 분할 알고리즘

```python
chunk_bgn = bgn
while True:
    chunk_end = min(chunk_bgn + timedelta(days=89), end)
    yield (chunk_bgn, chunk_end)
    if chunk_end >= end:
        break
    chunk_bgn = chunk_end     # ← 경계일을 겹친다 (+1일이 아니다)
```

`(chunk_end - chunk_bgn).days <= 89`가 모든 창에서 성립한다. 실측 통과 경계가 89일이므로 이를 상한으로 잡았다.

### 경계 처리 방식과 근거

**다음 창을 `chunk_end + 1일`이 아니라 `chunk_end`부터 시작한다** — 인접 창이 하루 겹친다.

- `list.json`은 `bgn_de`·`end_de`를 **모두 포함**해 조회하므로, 창을 딱 붙여 나눠도(다음 창을 `+1일`부터) 이론상 누락은 없다. 즉 겹침은 정확성상 필수는 아니다.
- 그래도 겹치는 이유는 **실패 비용의 비대칭** 때문이다. 경계 계산이 하루라도 어긋나면 그 날짜의 공시는 **영구히 누락**되고(폴링 창이 이미 지나갔으므로 자동 복구되지 않는다), 사후 발견도 어렵다. 반면 중복 조회는 `rcept_no` unique + `get_or_create`로 **완전히 무해**하다 — 중복 저장도, 중복 LLM 호출도 발생하지 않는다.
- 추가 비용은 `(창 수 - 1)`일치 재조회뿐이다. 150일 백필이면 창 2개 → 하루 재조회.

150일 예: `end - 150 ~ end`
- 청크 1 = `bgn` ~ `bgn+89`
- 청크 2 = `bgn+89` ~ `end` (61일)

### 멱등성이 유지되는 구조

별도 명령을 만들지 않았다. `poll_dart.handle`이 청크를 순회하고, 실제 적재는 `_collect` 안의 `get_or_create` **한 곳**만 지나간다. 청크 수·중복 조회 여부와 무관하게 멱등 경로가 단일하다.

### 진행 출력

청크가 2개 이상일 때만 청크별 로그를 낸다(단일 청크 정상 폴링의 출력은 기존과 동일).

```
조회 범위 20260101 ~ 20260630 (181일) · 추적 기업 10곳 · 날짜 청크 3개
  corp_code 없는 list.json은 검색기간 89일 초과 시 오류(코드 100)이므로 구간을 분할해 순회합니다(경계일 1일 중복 조회).
[청크 1/3] 20260101 ~ 20260331 조회
[청크 1/3] 완료: 스캔 3,672건, 신규 41건 (누적 스캔 3,672건, 누적 신규 41건)
...
완료: 전체 공시 N건 스캔, 신규 저장 M건 (청크 3개)
```

### 호출량 경고

```python
estimated = 청크수 × 공시유형 10종 + 총일수 × EST_PAGES_PER_DAY(10)
if estimated >= 10_000:  # 일일 한도 20,000회의 절반
    경고 출력
```

`page_count=100`, 시장 전체 공시 하루 1,000건 안팎을 근거로 한 어림수다. 실제 페이지 수는 조회 전에 알 수 없으므로 정확한 예측이 아니라 **규모 감지용 임계**로 쓴다. 약 1,000일(2.7년) 이상 범위에서 경고가 뜬다. 실행을 막지는 않고 경고만 출력한다.

## 4. 추가 테스트 (19건)

### `SplitDateRangeTest` — 분할 단위 테스트 (6건)

| 테스트 | 고정 내용 |
|---|---|
| `test_short_range_is_single_chunk` | 10일 범위는 분할되지 않는다 |
| `test_range_at_limit_is_not_split` | 정확히 89일(실측 통과 경계)은 단일 창 |
| `test_one_day_over_limit_splits` | 90일은 2개로 쪼개지며 경계값이 기대와 일치 |
| `test_every_chunk_within_limit_and_covers_range` | 2년 반 범위: 모든 창 ≤ 89일 **AND** 창들의 날짜 합집합 = 전체 범위 (누락 0일) |
| `test_chunks_overlap_by_one_day` | 앞 창의 종료일 == 뒤 창의 시작일 |
| `test_reversed_range_raises` | `bgn > end`는 `ValueError` |

### `PollDartBackfillTest` — 명령 레벨 (13건)

`iter_disclosures`를 `_RecordingIter`로 대체해 호출된 `(bgn_de, end_de)`를 기록한다. **DART 실호출 없음.**

| 테스트 | 고정 내용 |
|---|---|
| `test_bgn_end_queries_given_range` | `--bgn/--end`가 그 구간으로 조회되고, 유형 10종을 모두 호출 |
| `test_bgn_without_end_uses_today` | `--end` 생략 시 오늘까지 |
| `test_days_with_bgn_end_raises` | `--days`+`--bgn`/`--end` 3가지 조합 모두 `CommandError` |
| `test_end_without_bgn_raises` | `--end` 단독 `CommandError` |
| `test_invalid_date_format_raises` | 4가지 불량 형식 `CommandError` |
| `test_bgn_after_end_raises` / `test_future_end_raises` / `test_negative_days_raises` | 각 검증 규칙 |
| `test_long_range_splits_into_expected_chunks` | **20260101~20260630 → `[('20260101','20260331'), ('20260331','20260628'), ('20260628','20260630')]` 정확히 일치.** 호출 수 = 3청크 × 10유형 |
| `test_chunks_cover_range_without_gap` | 2025-01-01~2026-07-20: 명령이 실제로 조회한 창들의 합집합이 전체 범위를 완전히 덮고, 각 창이 89일 이하 |
| `test_days_over_limit_completes_without_error` | **`--days 150`이 오류 없이 완료**, 청크 2개, 마지막 창 종료일 = 오늘 |
| `test_overlapping_chunks_do_not_duplicate` | 대역이 청크마다 같은 공시를 반환(3회 조회) → 저장은 2건, `rcept_no` 건당 1행 |
| `test_backfill_then_rerun_is_idempotent` | 같은 백필 2회 실행 후에도 2건 |

### 실행 결과

```
.\venv\Scripts\python.exe manage.py check
  System check identified no issues (0 silenced).

.\venv\Scripts\python.exe manage.py test disclosures
  Ran 26 tests in 0.128s
  OK
```

기존 7건 + 신규 19건 = 26건 전부 통과. 기존 테스트는 무수정(`--days` 기본값 변경이 `days=7` 호출에 영향 없음).

## 5. 실호출 검증 내역

### (a) 3개월 한도와 분할 효과 직접 확인 — 핵심 근거

95일 범위를 분할 없이 / `split_date_range` 적용 후로 나눠 실호출 (총 3회):

```
span days = 95
분할 없이 조회 → DART API 오류 [100] corp_code가 없는 경우 검색기간은 3개월만 가능합니다.
청크 1 20260422~20260720 (89일) → status 000, total_count 3672
청크 2 20260720~20260726 (6일)  → status 000, total_count 20
```

한도 초과가 오류로 떨어지는 것과, 분할된 각 창이 정상 응답(`000`)을 받는 것을 라이브 API로 확인했다.

### (b) 신규 옵션 경로 실호출

```
poll_dart --bgn 20260723 --end 20260724
  → 조회 범위 20260723 ~ 20260724 (2일) · 추적 기업 10곳 · 날짜 청크 1개
    완료: 전체 공시 1,438건 스캔, 신규 저장 0건 (청크 1개)
```

이미 수집된 구간이라 신규 0건 = 실호출 경로에서도 멱등. `poll_dart --bgn 20260725 --end 20260726`은 0건 스캔(7/25 토·7/26 일, 주말이라 공시 없음 — 정상).

### (c) 검증 규칙 실행

```
poll_dart --days 3 --bgn 20260701   → CommandError: --days와 --bgn/--end는 함께 지정할 수 없습니다.
poll_dart --bgn 20260101 --end 20261231 → CommandError: 종료일(20261231)이 미래입니다. ...
poll_dart --bgn 2026-07-01          → CommandError: --bgn 날짜 형식이 잘못되었습니다: '2026-07-01' (YYYYMMDD)
```

### 미검증 항목

- **150일 전체 실호출은 하지 않았다** (수십 분 소요, 지시에 따라 생략). 분할 로직은 목 테스트와 위 (a)의 청크별 실호출로 검증했다.
- 89/92/95일 경계 재실측은 하지 않았다 — 팀 리더 실측값을 (a)에서 95일로 재확인하는 데 그쳤다.

`db.sqlite3` 표본은 실호출 후에도 **963건 유지**(신규 0건). 삭제·초기화 없음.

## 6. 남은 일 / 후속 작업자에게

- **모델 무변경.** 선별 정책 필드(대상 여부·제외 사유) 추가는 다른 에이전트 담당.
- `EST_PAGES_PER_DAY = 10`은 어림수다. 긴 백필을 실제로 돌려 페이지 수가 실측되면 이 값을 조정하면 경고 정확도가 올라간다.
- Celery 전환 시 `handle`의 청크 순회는 그대로 태스크로 옮길 수 있다. 다만 청크 단위로 태스크를 쪼개면 실패한 청크만 재시도할 수 있어 더 낫다 — `_collect(bgn_de, end_de, companies)`가 이미 그 경계로 분리돼 있다.
- `MAX_LIST_SPAN_DAYS`는 DART가 정책을 바꾸면 무효화된다. 오류 코드 100이 다시 뜨면 이 상수를 먼저 낮춰볼 것.
