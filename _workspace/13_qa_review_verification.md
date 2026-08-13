# 4단계 QA — 사람 검수 플로우 검증

기준: `_workspace/00_input.md` 4장 qa 완료 조건 · `11_backend_review_workflow.md` 7장 ·
`12_frontend_review_screen.md` 8장. 소유 파일 `disclosures/tests.py`에만 코드를 썼다.

## 0. 실행한 명령과 결과

```
.\venv\Scripts\python.exe manage.py check                 → System check identified no issues (0 silenced)
.\venv\Scripts\python.exe manage.py test disclosures      → Ran 208 tests, OK (expected failures=1)
.\venv\Scripts\python.exe manage.py makemigrations --check → No changes detected (신규 마이그레이션 없음)
.\venv\Scripts\python.exe -m pip install coverage         → coverage 7.15.4 설치
.\venv\Scripts\python.exe -m coverage run --source=disclosures --omit="*/migrations/*,*/tests.py" manage.py test disclosures
```

기존 130건 전부 통과 + 신규 78건. `expected failures=1`은 아래 **결함 1**을 고정한 것이다.

개발 DB(`db.sqlite3`)는 건드리지 않았다. 검증 후 실측: 요약 140건 · 노출 140 · 숨김 0 ·
검수 완료 0 · 검수 필요 56 · `auth_user` 0건. 마이그레이션 `0005` 적용 확인(`showmigrations`).
탐색용 프로브는 스크래치패드에서 격리 테스트 DB로만 돌렸다.

---

## 1. 발견한 결함

| 심각도 | 위치 | 증상 | 재현 | 기대 / 실제 | 담당 |
|---|---|---|---|---|---|
| 중간 | `disclosures/admin.py` `DisclosureSummaryAdmin` | admin '**AI 요약 추가**' 저장 시 500 | 스태프 로그인 → `/admin/disclosures/disclosuresummary/add/` → 아무 값이나 넣고 저장 | 기대: 추가 불가(403) 또는 정상 저장 / 실제: `IntegrityError: NOT NULL constraint failed: disclosures_disclosuresummary.disclosure_id` | backend |
| 낮음 | `disclosures/admin.py` `save_model` | `llm_original` 보존이 **읽기 전용 설정에만** 의존 | 아래 1.2 | 기대: 어떤 저장 경로로도 보존 / 실제: admin 폼 경로에서는 보존됨(확인), 비-폼 경로에서는 obj 값이 그대로 저장됨 | backend |
| 낮음 | `templatetags/review_panel.py` `has_key` | 문자열 입력에 대해 **부분 문자열 검사**로 동작 | `has_key('quote_found 를 담은 문자열', 'quote_found')` → `True` | 기대: dict 키 검사 / 실제: `in` 연산자 그대로 | frontend |
| 운영 | (코드 아님) | 검수 화면을 **열 수 있는 계정이 없다** | `auth_user` 0건 | 4단계 목표인 큐 56건 소진이 실행 불가 | 리더 |

### 1.1 결함 1 — admin 요약 추가 화면이 500 (4단계 회귀)

4단계에서 `disclosure`가 `readonly_fields`에 들어갔다(HEAD 시점 `readonly_fields =
('evidence', 'review_warnings', 'created_at')` → 현재 `('disclosure', 'dart_link', ...)`).
읽기 전용 필드는 **추가 폼에서 아예 빠지므로** 저장 시 `disclosure_id`가 NULL이 되어 터진다.
추가 권한은 열려 있어 목록 화면 우상단에 '추가' 버튼이 그대로 보인다.

- 4단계 이전에는 `disclosure`가 편집 가능해서 추가가 동작했다 → **이번 단계에서 생긴 회귀**다.
- 제안: `DisclosureSummaryAdmin.has_add_permission()` → `False`.
  인라인(`DisclosureSummaryInline`)이 이미 같은 이유로 추가를 막아 두었고
  ("요약은 요약 파이프라인만 만든다", PLAN.md 11) 두 화면의 정책이 일치하게 된다.
  손으로 요약을 만들 수 있게 두면 "공시당 1회" 원칙 밖의 요약이 생길 수도 있다.
- 대안: `get_readonly_fields(request, obj)`에서 `obj is None`일 때만 `disclosure`를 편집 허용.
- 고정 위치: `AdminReviewScreenTest.test_add_form_does_not_500` — `@unittest.expectedFailure`.
  **수정 후 이 데코레이터를 반드시 지울 것**(고쳐지면 unexpected success로 테스트 러너가 실패한다).

### 1.2 관찰 — `llm_original` 가드가 막는 것과 막지 못하는 것

backend 산출물 3장의 주장(`previous` 기준 가드)은 **의도한 대로 동작한다**. 다만 정확히는:

- 가드가 막는 것: **재스냅샷**. 저장하려는 객체의 `edited_by_human`이 초기화된 채 들어와도
  이미 사람이 고친 본문을 'LLM 원본'으로 다시 찍지 않는다.
  (`obj` 기준이었다면 여기서 1차 수정본이 LLM 원본으로 둔갑한다 —
  `test_stale_audit_flags_do_not_resnapshot_human_text`가 이 시나리오를 재현한다.)
- 가드가 막지 못하는 것: 들어온 객체의 `llm_original`이 `{}`이면 그 `{}`가 그대로 저장된다.
  즉 **기존 값 보존은 `llm_original`이 `readonly_fields`에 있다는 사실에만 의존**한다.
- 현재 admin 경로로는 재현 불가임을 확인했다(POST에 `llm_original={}`·`edited_by_human=''`을
  직접 실어도 무시된다 — `test_readonly_audit_fields_ignore_posted_values`).
- 방어를 원하면 `body_changed` 분기 밖에서 `obj.llm_original = obj.llm_original or
  previous.llm_original` 한 줄이면 된다. 지금은 잠재 취약점 수준이라 결함으로 올리지 않았다.

### 1.3 관찰 — `has_key` 문자열 부분일치

`in`이 문자열에서는 부분 문자열 검사다. evidence 항목이 dict가 아닌 옛 형식이면 판정이
부정확할 수 있으나 **예외는 나지 않아 화면이 깨지지는 않는다**(패널은 `인용 미검증`으로
안전하게 흐른다). 엄격히 하려면 `isinstance(value, dict)` 검사를 넣으면 된다.
현재 동작을 `test_has_key_on_a_string_degrades_to_substring_matching`으로 있는 그대로 고정했다.

### 1.4 운영 — 검수 계정 부재

`auth_user`가 0건이라 지금 상태로는 아무도 검수 화면에 들어갈 수 없다. frontend도 같은
이유로 브라우저 확인을 못 했다(산출물 8.1). `createsuperuser` 후 실제 브라우저에서
2단 레이아웃·칩 점프를 한 번 봐야 4단계가 실제로 닫힌다.

---

## 2. 추가한 테스트 (78건)

| 클래스 | 건수 | 막는 회귀 |
|---|---|---|
| `HiddenSummaryExposureTest` | 6 | 숨긴 요약이 섹터 피드·기업 타임라인·메인 하이라이트·상세 중 **한 곳으로라도 새는 것**, 숨김 사유가 웹에 노출되는 것, 복구가 안 되는 것 |
| `SectorCardCountConsistencyTest` | 3 | 카드 숫자 ↔ 실제 목록 건수 불일치("카드에 12건인데 들어가면 11건") |
| `HumanEditedBadgeTest` | 6 | 검수 완료 후에도 '미검수' 배지·정확성 배너가 남는 것, 수정만 하고 검수 전인데 '사람이 검토·수정함'이 뜨는 것, 배너를 걷으며 면책 문구까지 날리는 것 |
| `AdminHumanEditTrackingTest` | 10 | `llm_original` 덮어쓰기, 본문 외 필드 저장을 '사람 수정'으로 오인, BODY_FIELDS 중 일부가 감지에서 누락, 감사 필드 폼 주입, 검수 되돌림 시 거짓 이력 잔존 |
| `AdminBulkActionTest` | 8 | `reviewed_by`·`reviewed_at` 누락, 재검수 시 최신 검수자 미반영, 기존 숨김 사유 덮어쓰기, 복구 시 사유 잔존, '모두 선택' 액션이 정렬식과 충돌 |
| `NeedsReviewFilterParityTest` | 4 | **admin 필터 ↔ 모델 property 어긋남** (18개 조합 전수 + yes/no 분할 불변식) |
| `AdminReviewScreenTest` | 10 | 패널 미렌더, '원문에 없음' 칩 소실, 검수 화면에서 DART·LLM 호출, 우선순위 정렬 붕괴, 원문 미확보 공시에서 화면이 안 열리는 것, DART 링크 없는 행에서 목록이 깨지는 것 |
| `HighlightTermsSecurityTest` | 13 | **XSS**(원문·검색어 이스케이프), 태그 경계에 걸친 수치로 마크업이 깨지는 것, 표기 관용 상실, 짧은 표기가 긴 표기를 잡아먹는 것, 구분자만 있는 항목의 무한 분할, 정규식 메타문자 오작동 |
| `ReviewPanelFilterTest` | 6 | `quote_found` 키 부재를 '인용 미발견'으로 오표시, 비-dict 입력 500, 필드 라벨 누락 |
| `ReviewContractConsistencyTest` | 12 | 계약(3.1/3.2) 필드·property 이름·타입·기본값 변경, `was_human_edited` 정의 변질, **템플릿이 참조하는 속성이 모델에서 사라지는 것**, 인라인을 통한 `save_model` 우회, 액션 이름 변경, 감사 필드가 편집 가능해지는 것 |

특히 검증하라고 지시받은 3개 지점:

- **경계면 중복 A** — `NeedsReviewFilter` SQL ↔ `needs_review` property:
  `is_reviewed`(2) × `importance`(3) × `review_warnings`(3) = 18조합 전수 생성 후
  admin changelist 실 응답의 `cl.queryset`과 property 판정 집합을 **집합 동등**으로 비교.
  추가로 `yes ∪ no == 전체`, `yes ∩ no == ∅` 불변식까지 건다(한쪽만 고치면 여기서 먼저 깨진다).
  경계 5종(중요도 높음+검수완료 / 경고만+미검수 / 높음+미검수+경고없음 / 둘 다 아님 /
  검수완료+경고있음)을 말로 옮겨 별도 고정.
- **경계면 중복 B** — `sector_list().summary_count` ↔ `published_disclosures()`:
  카드 숫자·`total_count`·실제 렌더된 카드 수 **세 값을 한 튜플로 비교**한다.
  숨김 액션 실행 후·미요약 공시 존재 시에도 세 값이 같은지 확인.
- **XSS** — `raw_content`에 `<script>`·`<b>`·`&`·`"`가 있을 때 이스케이프되고 `<mark>`만
  삽입되는지, 검색어가 `data-term` 속성을 탈출하지 못하는지, 수치가 태그 경계에 걸리면
  (`값은 <b>3조</b> 9,891억`) 매칭되지 않고 원문 조각도 유실되지 않는지.

---

## 3. 커버리지

`coverage 7.15.4` · `--source=disclosures --omit="*/migrations/*,*/tests.py"`

```
Name                                                       Stmts   Miss  Cover   Missing
disclosures\admin.py                                         106      0   100%
disclosures\views.py                                          45      0   100%
disclosures\models.py                                         97      2    98%   17, 36
disclosures\templatetags\review_panel.py                      63      1    98%   108
disclosures\urls.py                                            4      0   100%
disclosures\apps.py                                            4      0   100%
disclosures\selection.py                                      40      2    95%   136, 163
disclosures\summarizer.py                                    311     71    77%   (아래 참고)
disclosures\dart.py                                          144     49    66%   (아래 참고)
disclosures\management\commands\apply_selection.py            60      0   100%
disclosures\management\commands\seed_companies.py             25      0   100%
disclosures\management\commands\poll_dart.py                  81      1    99%   153
disclosures\management\commands\fetch_documents.py            80      4    95%   67, 113, 168-169
disclosures\management\commands\revalidate_summaries.py       62      5    92%   42-43, 66, 70, 91
disclosures\management\commands\summarize_disclosures.py     106    106     0%   15-228
TOTAL                                                       1228    241    80%
```

**4단계 핵심 경로는 100%/98%다.** 측정 전 `admin.py`는 98%였고(미달 지점: `has_warnings=no`
분기, `dart_link`의 링크 없음 분기) 두 경로에 테스트를 보강해 100%로 올렸다.

남은 미커버 중 4단계 범위 안:

- `models.py` 17·36 — `Sector.__str__`·`Company.__str__`. 4단계와 무관한 사소한 누락.
- `review_panel.py` 108 — 빈 매칭 방어 `continue`. `_clean_terms`가 구분자만 있는 항목을
  이미 걸러서 **도달 불가능한 방어 코드**다(그 경로는 `test_separator_only_and_empty_terms_are_ignored`가 덮는다).

4단계 범위 밖(기존부터 비어 있던 곳, 이번에 손대지 않음):

- `summarize_disclosures.py` **0%** — LLM 호출 관리 명령. 이 프로젝트에서 비용이 걸린 유일한
  명령인데 테스트가 전무하다. **가장 큰 잔존 위험**이다(별도 단계로 다룰 것을 권한다).
- `dart.py` 66% · `summarizer.py` 77% — 네트워크·재시도 경로 중심의 미커버.

---

## 4. 검증했으나 문제없던 항목

- **숨김 요약 노출 0건** — 섹터 피드·기업 타임라인·메인 하이라이트·상세(404) 네 경로 모두.
  `published_disclosures()`가 단일 출처로 동작하며 템플릿에서 따로 거르지 않는다(계약 3.3 준수).
- **검수 완료 시 배지·배너 소멸** — 상세와 목록 카드 양쪽에서 '미검수'·'수치 확인 필요'·
  정확성 배너가 사라지고, 면책 문구는 남는다.
- **`llm_original` 최초 1회 기록** — 2차·3차 수정에도 1차 스냅샷이 유지된다(admin 실 POST 경로).
  본문 3필드 각각이 감지 대상이며, 본문 외 필드만 바꾼 저장은 사람 수정으로 세지 않는다.
- **일괄 액션 3종** — `reviewed_by`(요청 사용자)·`reviewed_at` 기록, 재실행 시 최신 검수자로 갱신,
  검수 되돌림 시 이력 삭제, 숨김 시 기존 사유 보존·빈 사유만 기본값 기입, 복구 시 사유 삭제.
  '모두 선택(select_across)'으로 정렬식이 걸린 전체 큐리셋에 `update()`를 걸어도 정상 동작.
- **검수 대기 정렬** — 중요도 높음 → 경고 있음 → 접수일 최신 순서가 실제 changelist에서 유지.
- **인라인 우회 불가** — `DisclosureSummaryInline`은 편집 가능한 모델 필드가 0개이고
  추가 권한도 없다. 공시 변경 화면 HTML에 요약 입력 위젯이 렌더되지 않는다.
- **계약 이름 일관성** — 템플릿이 `summary.`·`original.`로 참조하는 21개 속성
  (`was_human_edited`·`unsupported_numbers`·`accuracy_warnings`·`needs_review`·
  `llm_original` 등)이 전부 모델에 실재한다. `was_human_edited = edited_by_human and
  is_reviewed` 진리표 4조합 일치. `llm_original`의 키 3종이 패널이 읽는 이름과 같다.
- **PLAN.md 12.1** — `ViewsDoNotCallExternalApisTest` 통과 유지. 추가로 **검수 화면**
  (admin 변경 폼·목록)에서도 `_call_openai`·`dart.requests.get`이 호출되지 않음을 확인.
- **`revalidate_summaries`의 사람 수정 필드 간섭 없음** — `save(update_fields=['review_warnings',
  'evidence'])`라서 `edited_by_human`·`llm_original`·검수 이력을 덮지 않는다
  (backend 산출물 7.2의 우려는 경고 재계산에 한정된다).
- **회귀** — 기존 테스트 130건 전부 통과, `manage.py check` 0 issues, 신규 마이그레이션 없음.

---

## 5. 남은 위험

1. **결함 1이 고쳐질 때까지 스태프에게 500이 보이는 버튼이 열려 있다.** 데이터 오염은 없다.
2. **검수 계정이 없어 4단계가 실사용 검증되지 않았다.** 서버사이드 렌더까지는 확인했지만
   CSS·JS(2단 레이아웃, 칩 → 원문 점프)는 브라우저에서 아무도 못 봤다.
3. **`summarize_disclosures.py` 커버리지 0%** — 비용이 발생하는 유일한 경로.
4. `evidence` 인용문은 원문에서 하이라이트하지 않는다(frontend 8.2). 의도된 선택이며
   테스트도 그 전제로 작성했다 — 나중에 정규화 재사용으로 바뀌면 테스트도 같이 고쳐야 한다.
5. 하이라이트는 문자열 매칭만 한다. 억↔조 단위 환산 때문에 '원문에 없음'으로 뜨는 오탐이
   남아 있다(frontend 8.3). 검수자가 이를 '오류 확정'으로 읽지 않도록 화면 문구가 방어 중.

---

## 6. 재검증 (결함 수정 후 · 리더 수행)

qa 세션이 보고 단계에서 중단되어(API 오류) 리더가 흡수해 마무리했다.

**결함 3건 수정 반영 확인**
| # | 수정 | 확인 |
|---|---|---|
| 1 | `DisclosureSummaryAdmin.has_add_permission() → False` | 추가 URL GET·POST 모두 403, 요약 미생성 |
| 2 | `obj.llm_original = obj.llm_original or previous.llm_original` (`body_changed` 분기 밖) | 두 번째 수정에서도 LLM 원본 보존 |
| 3 | `has_key`가 `isinstance(value, dict)` 검사 | 문자열·리스트·None 전부 False |

**전체 실행 결과**
```
manage.py test disclosures            → Ran 209 tests, OK (expected failure 0건)
manage.py check                       → 0 issues
manage.py makemigrations --check      → No changes detected
```

**커버리지 (coverage run --source=disclosures)**

| 모듈 | 커버리지 |
|---|---|
| `admin.py` · `views.py` · `urls.py` | 100% |
| `models.py` | 98% |
| `templatetags/review_panel.py` | 98% |
| `summarizer.py` | 77% |
| `dart.py` | 66% |
| `summarize_disclosures.py` | **0%** (5장 위험 3) |
| **TOTAL** | **91%** |

**재검증 중 고친 것 — 테스트 1건 (`test_changelist_offers_no_add_button`)**

`assertNotContains(response, 'addlink')`가 실패했으나 **제품 코드가 아니라 단언이 틀렸다.**
응답을 보면 AI 요약 화면의 `object-tools`는 비어 있고 사이드바의 요약 행도 빈 칸이라 추가
버튼은 정상적으로 없다. 검출된 `addlink` 5개는 사이드바에 늘 함께 렌더되는 **다른 모델**
(공시·기업·섹터·그룹·사용자)의 링크였다. 클래스 이름은 이 화면 고유의 신호가 아니므로
`reverse('admin:disclosures_disclosuresummary_add')` URL 부재로 단언을 좁혔다.
단언이 좁아진 만큼 결함 1의 회귀는 `test_summary_cannot_be_added_by_hand`(403 고정)가 잡는다.

**5장 남은 위험의 갱신**: 위험 1(500 버튼)은 해소됐다. 위험 2(검수 계정 부재로 브라우저
미검증)·3(`summarize_disclosures.py` 0%)·4·5는 그대로 남아 있다.
