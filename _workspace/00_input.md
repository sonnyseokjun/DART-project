# 4단계 작업 입력 — 사람 검수 플로우 구축

- **로드맵 단계**: PLAN.md 9장 4단계 (품질·검증)
- **브랜치**: `feature/stage4-review-workflow` (기준 커밋 `f840359`)
- **투입 에이전트**: backend · frontend · qa (analyst·ai-prompt는 범위 밖)
- **실행 모드**: 서브 에이전트 (팀 도구 미가용)

---

## 1. 문제

검수가 필요한 요약이 **56건인데 검수 완료가 0건**이다. admin에 요약을 검수할 *수단*이
없어 큐가 전혀 소진되지 않고, 그 결과 웹의 정확성 경고 배너 **29건이 영구히 사라지지 않는다.**

실제 사례: SK하이닉스 유상증자 요약(`20260715800045`)이 금액을 10배 잘못 적었다
(39조 8,905억 → 3조 9,891억). 자동 검증은 정확히 잡아냈지만 **고칠 방법이 없어**
배너를 단 채 노출 중이다.

### 현황 (2026-08-11)

```
요약 140건 / 검수 필요 56건 / 검수 완료 0건 / 정확성 경고 29건
사유별:  중요도 높음만 24건 · 경고만 18건 · 둘 다 14건
경고:    근거 없는 수치 19건 · 인용 미발견 14건 · 문장 수(문체) 4건
```

### 이미 되어 있는 것 (다시 만들지 말 것)

- 숫자 대조 검증 — `summarizer.validate_summary` / `verify_evidence`
- 경고 종류 구분 — `summarizer.ACCURACY_WARNING_PREFIXES`
- 면책 문구 — `templates/disclosures/base.html` 전 페이지 상시 노출
- admin 기초 — `HasWarningsFilter`, `검수 필요` 컬럼, 경고 수 표시, evidence 읽기 전용
- 재검증 명령 — `manage.py revalidate_summaries` (LLM 미호출·멱등)

---

## 2. 확정된 설계 판단 (사용자 승인 완료 · 재검토 금지)

1. **검수 화면은 Django admin 커스텀**으로 만든다. `change_form` 템플릿 오버라이드 방식.
   별도 스태프 화면을 새로 짜지 않는다 — 인증·권한·목록·필터를 Django가 이미 준다.
2. **사람이 수정한 요약은 웹에 밝힌다.** "사람이 검토·수정함" 표시.
3. **숨김 처리한 공시는 웹 목록에서 완전히 제거한다.** 빈 껍데기 카드를 남기지 않는다.
4. 검수 조치는 **(a) 직접 수정 + (c) 노출 숨김** 두 가지뿐.
   **LLM 재생성은 채택하지 않는다** — 같은 프롬프트로 다시 뽑아도 같은 오류가 날 수 있고
   "공시당 1회" 원칙(PLAN.md 11)과 충돌한다.

---

## 3. 인터페이스 계약 (backend·frontend 공통 · 변경 시 즉시 SendMessage)

### 3.1 `DisclosureSummary` 신규 필드 — backend가 소유

| 필드 | 타입 | 기본값 | verbose_name | 의미 |
|---|---|---|---|---|
| `is_published` | BooleanField | `True` | `노출 여부` | False면 웹에서 완전히 감춘다 |
| `hidden_reason` | CharField(200) | `''` blank | `숨김 사유` | 검수자가 남기는 메모 |
| `edited_by_human` | BooleanField | `False` | `사람 수정 여부` | 본문을 사람이 고쳤는지 |
| `llm_original` | JSONField | `dict` blank | `LLM 원본` | 첫 사람 수정 시점의 LLM 출력 스냅샷 |
| `reviewed_by` | FK(settings.AUTH_USER_MODEL) | `null=True, blank=True` | `검수자` | `on_delete=SET_NULL` |
| `reviewed_at` | DateTimeField | `null=True, blank=True` | `검수 시각` | |

`llm_original`은 `{'one_line':…, 'easy_explanation':…, 'why_important':…}` 형태.
**사람이 처음 수정할 때만 채우고 이후 덮어쓰지 않는다** — 원본 추적이 목적이므로
두 번째 수정 때 덮어쓰면 LLM이 실제로 뭐라고 했는지 잃는다.

### 3.2 신규 property — backend가 소유, frontend가 소비

```python
DisclosureSummary.was_human_edited  # edited_by_human and is_reviewed
```

기존 `needs_review` / `accuracy_warnings` / `unsupported_numbers` 의미는 **바꾸지 않는다.**
(`accuracy_warnings`는 이미 `is_reviewed`면 빈 리스트를 돌려준다 — 검수 완료 시
웹 배너가 자동으로 사라지는 근거다.)

### 3.3 노출 정책 — backend가 소유

`disclosures/views.py`의 `published_disclosures()`에 `summary__is_published=True` 추가.
**노출 정책은 이 함수 하나가 단일 출처다.** 템플릿이나 다른 뷰에서 따로 거르지 말 것.

### 3.4 파일 소유권 (충돌 방지 · 남의 파일을 고치지 말 것)

| 에이전트 | 소유 파일 |
|---|---|
| backend | `disclosures/models.py`, `disclosures/admin.py`, `disclosures/views.py`, `disclosures/migrations/*` |
| frontend | `disclosures/templates/**`, `disclosures/static/**` |
| qa | `disclosures/tests.py` |

상대 파일에 변경이 필요하면 직접 고치지 말고 **SendMessage로 요청**한다.

---

## 4. 완료 조건

### backend
- [ ] 3.1 필드 추가 + 마이그레이션 생성
- [ ] admin에서 요약 본문 3필드(`one_line`·`easy_explanation`·`why_important`) 수정 가능
- [ ] 사람이 본문을 수정하면 `edited_by_human=True`, `llm_original` 최초 1회 기록
      (`ModelAdmin.save_model`에서 변경 감지)
- [ ] 일괄 액션: **검수 완료 처리** / **노출 숨김** / **노출 복구**
- [ ] 검수 완료 시 `reviewed_by`(요청 사용자)·`reviewed_at` 기록
- [ ] 검수 대기 우선순위 정렬 (중요도 높음 → 경고 있음 → 접수일 최신)
- [ ] `published_disclosures()`가 숨김 요약을 제외
- [ ] admin 목록에 DART 원문 링크 컬럼

### frontend
- [ ] admin `change_form` 오버라이드로 **원문 대조 패널** 추가
      (요약 ↔ `disclosure.raw_content` 병렬 표시)
- [ ] 경고가 지목한 수치(`summary.unsupported_numbers`)를 원문에서 **하이라이트**
- [ ] `evidence`를 JSON이 아닌 읽기 좋은 형태로 표시 (claim + quote 쌍, 검증 결과 배지)
- [ ] 웹 상세·카드에 "사람이 검토·수정함" 표시 (`was_human_edited`)
- [ ] 검수 완료 시 '미검수' 배지와 정확성 배너가 사라지는지 확인 (기존 로직으로 자동)

### qa
- [ ] 숨김 요약이 웹 목록·상세·메인 하이라이트 어디에도 노출되지 않는지
- [ ] 검수 완료 시 배지·배너가 사라지는지
- [ ] `llm_original`이 두 번째 수정에서 덮어써지지 않는지
- [ ] 일괄 액션이 `reviewed_by`·`reviewed_at`를 올바로 남기는지
- [ ] `coverage.py`로 커버리지 측정, 결과를 `_workspace/`에 기록
- [ ] `manage.py check` + `manage.py test disclosures` 전체 통과

---

## 5. 제약

- **뷰·검수 화면에서 DART API나 LLM을 호출하지 않는다** (PLAN.md 12.1).
  원문은 이미 `raw_content`에 있다. "검수 화면에서 최신 원문을 가져오자"는 코드 금지.
  `disclosures/views.py`가 `dart`·`summarizer`를 import하지 않는지 확인하는 테스트가
  이미 있다(`ViewsDoNotCallExternalApisTest`) — 깨뜨리지 말 것.
- 사람의 요약 수정은 "공시당 1회" 원칙을 어기지 않는다 (LLM 호출이 아니라 교정).
- 모델 필드·admin에 한국어 `verbose_name`. 주석·docstring·커밋 메시지도 한국어.
- SQLite 단일 파일이므로 마이그레이션과 대량 작업을 동시에 돌리지 않는다.
- 기존 테스트 130개를 깨뜨리지 않는다.
- `.env`는 수정 금지 (권한 정책).
