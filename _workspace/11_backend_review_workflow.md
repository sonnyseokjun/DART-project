# 4단계 backend — 사람 검수 플로우 (구현 완료)

기준: `_workspace/00_input.md` 3장 인터페이스 계약 · 4장 backend 완료 조건

## 1. 변경 파일

| 파일 | 변경 |
|---|---|
| `disclosures/models.py` | 필드 6개 · `BODY_FIELDS` 상수 · `body_snapshot()` · `was_human_edited` |
| `disclosures/admin.py` | 본문 수정 허용 · `save_model` 변경 감지 · 액션 3개 · 우선순위 정렬 · DART 링크 · `NeedsReviewFilter` · 인라인 읽기 전용화 |
| `disclosures/views.py` | `published_disclosures()`에 `summary__is_published=True` · 섹터 카드 집계 동기화 |
| `disclosures/migrations/0005_disclosuresummary_edited_by_human_and_more.py` | 신규 (생성만 함, `migrate` 미실행) |

남의 소유 파일(`tests.py`, `templates/**`, `static/**`)은 건드리지 않았다.

## 2. 추가한 필드 (00_input.md 3.1 표 그대로)

`is_published`(BooleanField/True/노출 여부) · `hidden_reason`(CharField 200/blank/숨김 사유) ·
`edited_by_human`(BooleanField/False/사람 수정 여부) · `llm_original`(JSONField/dict/LLM 원본) ·
`reviewed_by`(FK AUTH_USER_MODEL/SET_NULL/검수자, `related_name='reviewed_summaries'`) ·
`reviewed_at`(DateTimeField/null/검수 시각).

설계 근거 중 표에 없던 것:
- **삭제가 아니라 숨김**: 요약을 지우면 재수집 시 LLM을 다시 부르게 되고(PLAN.md 11 위배)
  왜 내렸는지도 사라진다. `is_published`는 노출만 끊는다.
- **`reviewed_by`는 SET_NULL**: 사용자 계정이 지워져도 "검수는 되었다"는 사실은 보존해야 한다.
- **`BODY_FIELDS` 상수 + `body_snapshot()`**: 변경 감지와 `llm_original` 기록이 같은 필드 목록을
  봐야 어긋나지 않는다. 모델에 두어 단일 출처로 삼았다.

## 3. `save_model`의 사람 수정 판정

**판정 기준 = DB에 저장된 값 vs 저장하려는 값의 차이** (`form.changed_data` 아님).

```
previous = DisclosureSummary.objects.filter(pk=obj.pk).first()   # change=True 일 때만
body_changed = any(previous.<f> != obj.<f> for f in BODY_FIELDS)
if body_changed:
    if not previous.edited_by_human and not previous.llm_original:
        obj.llm_original = previous.body_snapshot()   # ← 최초 1회
    obj.edited_by_human = True
```

- `changed_data` 대신 DB 값을 보는 이유: 저장 경로(폼·스크립트)와 무관하게 같은 규칙이 적용되고,
  값을 고쳤다가 원래대로 되돌린 경우를 "수정"으로 세지 않는다.
- **덮어쓰기 방지 가드가 `previous` 기준인 점이 핵심**이다. `obj` 기준으로 판정하면 폼에서
  `edited_by_human`을 끄는 순간 두 번째 수정에서 LLM 원본이 날아간다. 그래서
  `edited_by_human`·`llm_original`은 admin에서 읽기 전용이고, 판정도 DB 값으로만 한다.
- 조건이 `not edited_by_human` **and** `not llm_original` 둘 다인 이유: 둘 중 하나가 데이터
  마이그레이션 등으로 어긋나도 이미 사람 손이 닿은 본문을 'LLM 원본'으로 오인해 덮지 않는다.

같은 메서드에서 검수 상태 전이도 처리한다(일괄 액션과 동작을 맞추기 위해):
- `is_reviewed` False→True: `reviewed_by=request.user`, `reviewed_at=now()`
- True→False: `reviewed_by=None`, `reviewed_at=None` (되돌렸는데 검수 이력이 남으면 거짓 이력)

## 4. frontend·qa가 알아야 할 인터페이스

**property (frontend 소비)**
- `summary.was_human_edited` → `edited_by_human and is_reviewed`.
  수정만 하고 검수 완료 전이면 **False**다. 고치다 만 상태를 "사람이 검토함"으로 내보내면
  실제보다 강한 신뢰 신호가 되기 때문. 웹의 "사람이 검토·수정함" 표시는 이 값을 쓴다.
- `summary.needs_review` / `accuracy_warnings` / `unsupported_numbers` — **의미 변경 없음**.
- `summary.body_snapshot()` — 본문 3필드 dict. 원문 대조 패널에서 `llm_original`과 비교할 때 쓸 수 있다.
- `summary.llm_original` — 사람 수정 전이면 `{}` (빈 dict). 템플릿에서 존재 여부로 분기 가능.

**admin 액션 (메서드 이름 / 화면 라벨)**
- `mark_reviewed` / "선택한 요약을 검수 완료 처리" → `is_reviewed=True` + `reviewed_by` + `reviewed_at`
- `hide_summaries` / "선택한 요약을 웹에서 숨김" → `is_published=False`, 사유가 비었으면
  `admin.DEFAULT_HIDDEN_REASON`('검수자 일괄 숨김 처리') 기입
- `restore_summaries` / "선택한 요약의 노출 복구" → `is_published=True`, `hidden_reason=''`

**admin 필터**: `?needs_review=yes|no`, `?has_warnings=yes|no`, `importance`, `is_reviewed`,
`is_published`, `edited_by_human`

**admin 화면 구조 (frontend의 `change_form` 오버라이드 대상)**
fieldsets 5개: `공시` / `요약 본문 (검수자가 직접 수정)` / `자동 검증` / `검수` / `기록`.
읽기 전용: `disclosure`, `dart_link`, `evidence`, `review_warnings`, `edited_by_human`,
`llm_original`, `reviewed_by`, `reviewed_at`, `created_at`.
수정 가능: `one_line`, `easy_explanation`, `why_important`, `importance`, `is_reviewed`,
`is_published`, `hidden_reason`.

**뷰 컨텍스트**: 변경 없음. `published_disclosures()`가 숨김 요약을 거르므로 템플릿에서
`is_published`를 따로 검사하지 말 것.

## 5. 판단이 갈렸던 지점

| 지점 | 선택 | 이유 |
|---|---|---|
| 정렬 구현 | `get_ordering()`이 `Case(...).asc()` **정렬식**을 반환 | 처음엔 `get_queryset()`에서 annotate + 별칭 정렬로 짰는데 `ModelAdmin.get_queryset()`이 **주석을 붙이기 전에** `order_by`를 먼저 적용해 `FieldError`가 났다(스모크 검증에서 잡음). `ordering` 속성은 별칭을 못 써서 `admin.E033`에 걸린다. 정렬식 직접 반환이 유일하게 깨끗한 길이고 열 머리글 클릭 정렬도 유지된다 |
| `needs_review` 필터 | SQL 조건을 admin에 한 번 더 적음 | property라 `list_filter`에 못 올린다. 조건식은 모델 property와 동일하게 유지 — **바뀌면 두 곳을 같이 고쳐야 한다** |
| 요약 인라인(공시 화면) | 전 필드 읽기 전용 + 추가 금지 | 인라인 저장은 `DisclosureSummaryAdmin.save_model`을 타지 않는다. 여기서 본문을 고칠 수 있게 두면 사람 수정 감지가 통째로 우회되어 `edited_by_human`·`llm_original`이 비게 된다. 수정은 'AI 요약' 화면 한 곳에서만 |
| 감사 필드 편집 | `edited_by_human`·`llm_original`·`reviewed_by`·`reviewed_at` 읽기 전용 | 손으로 고칠 수 있으면 감사 기록이 아니다. `evidence`·`review_warnings`도 계속 읽기 전용 — 경고를 지워 문제를 덮는 길을 막는다 |
| `mark_reviewed` 재실행 | 검수자·시각을 최신으로 덮어씀 | 다시 도장을 찍는 행위는 "지금 이 사람이 다시 확인했다"는 뜻이다. `llm_original`과 달리 최신값이 맞다 |
| 섹터 카드 집계 | `sector_list()`의 `summary_count`에도 `is_published=True` 추가 | 집계는 `published_disclosures()`로 대체할 수 없어 조건을 한 번 더 적었다. 안 하면 카드에 "12건"인데 들어가면 11건인 불일치가 난다. **두 곳이 같이 움직여야 한다** |

## 6. 검증 (실행한 명령과 결과)

```
.\venv\Scripts\python.exe manage.py check                      → 0 issues
.\venv\Scripts\python.exe manage.py makemigrations disclosures  → 0005_... 생성 (필드 6개)
.\venv\Scripts\python.exe manage.py test disclosures            → Ran 130 tests, OK (exit 0)
```

추가로 스크래치패드 스모크 스크립트(인메모리 테스트 DB, `db.sqlite3` 미접촉)로 확인:
정렬 순서 · `NeedsReviewFilter` 양방향 · `llm_original` 최초 1회 기록 · 두 번째 수정 시 보존 ·
본문 외 필드만 바꾼 저장은 수정으로 세지 않음 · 검수 기록 설정/해제 · 액션 3종 ·
숨김 시 `published_disclosures()` 제외 · admin 목록/필터/`?o=` 정렬/변경 화면 200 렌더.
**이건 임시 검증이고 정식 테스트는 qa가 `tests.py`에 작성한다.**

## 7. 미해결 사항·주의점

1. **`migrate` 미실행** (지시대로). 통합 시점에 `manage.py migrate` 필요.
   기존 요약 140건은 `is_published=True`·`edited_by_human=False`·`llm_original={}`로 채워진다.
2. **`revalidate_summaries`가 `review_warnings`를 다시 쓴다.** 사람이 본문을 고친 뒤 이 명령을
   돌리면 수정된 본문 기준으로 경고가 재계산된다(= 고쳤으면 경고가 사라지는 게 맞다). 다만
   `is_reviewed`는 건드리지 않으므로 검수 완료 상태는 유지된다. 명령 파일은 내 소유가 아니라
   손대지 않았다 — 4단계 범위 밖으로 판단.
3. **조건 중복 2곳**: `NeedsReviewFilter`(모델 `needs_review`와), `sector_list`의 `summary_count`
   (`published_disclosures()`와). 둘 다 주석으로 명시했지만 구조적 중복이므로 향후 변경 시 주의.
4. 검수 화면에서 원문은 `disclosure.raw_content`만 쓴다. DART·LLM 호출 코드는 넣지 않았고
   `views.py`는 `dart`·`summarizer`를 여전히 import하지 않는다(`ViewsDoNotCallExternalApisTest` 통과).
5. `hide_summaries`는 일괄 액션이라 개별 사유를 입력받지 못한다. 구체적 사유는 변경 화면에서
   `hidden_reason`을 덧쓰는 방식이다. 액션 중간 확인 페이지가 필요하면 별도 요청.
