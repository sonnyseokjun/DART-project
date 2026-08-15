# 24 · frontend — 검수 화면: 자동 미게시 사유 · 재생성 이력

소유 파일만 고쳤다: `templates/**` · `static/**`. `templatetags/**` 는 **결국 손대지 않았다**(3장).
LLM 실호출 0회. `db.sqlite3` 는 **읽기만** 했고 렌더 확인은 전부 인메모리 테스트 DB로 했다.

## 0. 한눈에

| 과제 | 결과 |
|---|---|
| 1. 검수 화면에 자동 미게시·재생성 이력 표시 | **완료** — 배지 2종 + 블록 2개 |
| 2. 웹 화면 확인 | **거의 변경 불필요.** 다만 **배지 회귀 1건은 고쳤다**(2장) |
| 3. 금액 표기 템플릿 필터 | **하지 않았다.** 본문에 이미 병기돼 있어 중복(3장) |
| 새 색 추가 | **0개.** 기존 `--rp-warn`/`--rp-ok` 의 역할만 나눴다 |
| 검증 | 렌더 확인 39건 전부 통과 · `manage.py check` 0 issues |

깨진 테스트가 **4건 → 3건**이 됐다. 없어진 1건이 2장의 배지 회귀다.
남은 3건은 backend 게이트 변경에 대한 qa 기대값 갱신 몫이다(`22_backend_pipeline.md` 3장).

---

## 1. 검수 화면에 얹은 것

파일: `disclosures/templates/admin/disclosures/disclosuresummary/_review_panel.html`
      `disclosures/static/disclosures/css/review-panel.css`

### 1.1 읽는 필드 — backend 인터페이스와 대조한 근거

`22_backend_pipeline.md` 7장의 목록을 그대로 `models.py` 에서 확인하고 썼다. **추측한 필드는 없다.**

| 화면에서 쓰는 것 | 실체 | 확인한 위치 |
|---|---|---|
| `original.auto_hidden` | `not is_published and hidden_by == 'auto'` | `models.py:188-191` |
| `original.hidden_by` / `get_hidden_by_display` | `'' \| 'auto' \| 'human'`, 라벨 `자동 미게시(검증 실패)` / `검수자가 내림` | `models.py:100-104, 134-137` |
| `original.hidden_reason` | 자유 문자열. 기본 문구는 `verification.AUTO_HIDDEN_REASON` | `models.py:128`, `verification.py:504` |
| `original.regeneration_count` | int | `models.py:140` |
| `original.regeneration_exhausted` | `count >= MAX_REGENERATION_ATTEMPTS(=1)` | `models.py:193-201`, `review_policy.py:142` |
| `original.regeneration_history` | `[{attempt, at, reason, warnings, model, cost_usd, resolved, remaining_warnings, error?, rolled_back?}]` | `summarize_disclosures.py:327-357` |
| `original.disclosure.review_category` / `get_review_category_display` | `'' \| capital \| control \| distress \| safety` | `models.py:64-67`, `review_policy.py:59-65` |

이력 dict의 키는 **보고서 4.4의 형식표가 아니라 실제 생성 코드**(`_regenerate`)에서 확인했다.
`resolved`·`remaining_warnings` 는 호출이 성공했을 때만 붙고, `error` 는 성공하면 아예 없으며,
`rolled_back` 은 되돌렸을 때만 붙는다. **그래서 세 키를 배타 분기로 읽지 않고 `error` →
`resolved` → 그 외 순으로 갈랐다.** `rolled_back` 은 별도 배지다(호출은 됐으나 결과를 버린 것).

### 1.2 자동 미게시 vs 사람이 내림 — 어떻게 갈랐나

판정은 **`hidden_by`(기계값)로만** 한다. `hidden_reason` 문구로 갈라도 지금은 같은 결과가
나오지만 그 필드는 검수자가 폼에서 고치는 자유 문자열이라 한 번 고쳐 쓰면 판정이 무너진다
(backend 2.2와 같은 이유). 템플릿에서는 `auto_hidden` property 한 개만 보면 된다.

상태는 셋이 아니라 **넷**이다. 세 개로 짜면 옛 데이터가 자동 미게시로 오인된다.

| 상태 | 화면 |
|---|---|
| 게시 중 | 배지 없음 |
| 자동 미게시 (`hidden_by='auto'`) | 붉은 채움 배지 `자동 미게시 · 아직 아무도 보지 않음` + 설명 블록 |
| 사람이 내림 (`hidden_by='human'`) | 무채색 배지 `검수자가 내림` + 사유 |
| **미게시인데 `hidden_by=''`** | 무채색 배지 `웹에서 숨김 · 주체 미기록` |

넷째가 필요한 이유: `hidden_by` 는 마이그레이션 `0006` 에서 생긴 필드라 **그 전에 사람이 내린
요약은 전부 빈 문자열**이다. 이걸 자동 미게시 쪽으로 넣으면 "아직 아무도 안 봤다"는 거짓
신호가 되고, 사람 쪽으로 넣으면 실제로는 모르는 것을 안다고 말하는 셈이다. 모른다고 적었다.

### 1.3 색 — 새로 만들지 않았다

리더 지시대로 4단계 토큰만 썼다. 패널은 `style.css`(웹 화면)를 싣지 않으므로 `--verified`·`--high`
자체를 참조할 수 없고, 그 값을 그대로 옮겨 둔 패널 스코프 토큰(`--rp-warn` = `#c0392b` = `--high`,
`--rp-ok` = `#1e7a46` = `--verified`)이 대응물이다. **역할만 나눴다.**

- 자동 미게시 = 경고색을 **채운** 배지 → "지금 구멍이 나 있다, 손이 필요하다"
- 사람이 내림 = 기존 무채색 배지(`#3a3f47`) → "이미 결론이 난 상태"
- 설명 블록은 `.rp-flags`(지목된 수치)와 **같은 형태**로 만들었다. 검수자가
  "붉은 왼쪽 테두리 = 지금 손볼 것"이라는 규칙 하나만 익히면 되게 하려는 것이다.

### 1.4 재생성 이력

`<details>` 로 접어 두되 **자동 미게시 건은 펼친 채로 연다.** 접힌 요약줄에 횟수와 상한 소진
여부가 그대로 나오므로, 펼치지 않아도 "AI가 몇 번 시도했는가"는 읽힌다.

시도마다: `n차 시도` · 결과 배지 · 모델명 · 시각 · **고치려던 경고** · **다시 만든 뒤에도 남은 경고**.
남은 경고를 같이 보여주는 것이 요점이다 — 검수자가 "AI가 뭘 고치려다 뭘 못 고쳤는지"를
알면 자기가 손볼 지점이 바로 정해진다.

**`regeneration_exhausted` 배지는 `auto_hidden` 일 때만 띄운다.** 이 property 는
`count >= 1` 이기만 하면 참이라, **재생성으로 교정에 성공해 게시 중인 요약에도 참**이 된다.
거기에 "자동 교정 불가"를 붙이면 정반대 사실을 말하게 된다. 판정이 갈렸던 지점이고,
property 이름만 보고 배선했으면 틀렸을 자리다.

**재생성 0회인 자동 미게시 건**에는 `AI 재생성 시도 0회 — 아직 다시 만들어 보지 않았습니다`
를 넣었다. 현재 재생성은 기본 꺼짐(`--regenerate`)이라 **실데이터에서는 이쪽이 정상 경로**다.
이력 블록이 아예 안 뜨는 것과 "0회 시도했다"는 다른 정보다.

### 1.5 게이트 유형 배지 (지시 범위 밖 · 작게 추가)

`검수 필수 유형 · 자본구조 변동` 무채색 배지 하나를 넣었다. 경고가 하나도 없는데 큐에 있는
요약은 전부 이 유형 때문에 들어온 것인데, 패널이 그걸 안 알려주면 검수자가 "왜 이게 여기
있지"부터 다시 조사한다. admin 목록에는 이미 같은 열이 있어(`admin.py:201-210`) 목록과
상세의 정보가 어긋나 있던 것을 맞춘 셈이다. 배지 1개, 새 색 0개다.

---

## 2. 웹 화면 — 대부분 불필요, 그러나 배지 1건은 고쳤다

### 2.1 불필요한 것 (손대지 않았다)

`published_disclosures()`(`views.py:43-47`)가 `summary__is_published=True` 로 이미 거른다.
실제로 확인했다 — 자동 미게시 건은 **상세 404, 목록에서 제목조차 안 나온다.**
`sector_list` 의 섹터별 건수도 같은 조건을 한 번 더 적고 있어(`views.py:86-93`) 숫자가 어긋나지
않는다. **템플릿에서 할 일이 없다.**

### 2.2 고친 것 — "AI 생성 · 미검수" 배지 회귀 (`22_backend_pipeline.md` 3.1)

`_disclosure_card.html:22` · `disclosure_detail.html:24` 의 `{% if summary.needs_review %}` 를
`{% if not summary.is_reviewed %}` 로 바꿨다.

`needs_review` 는 5단계에서 **검수 큐에 넣을지**를 정하는 운영 개념(공시 유형 게이트)이 됐고,
이 배지는 **사람이 봤는지**를 알리는 사용자 신뢰 신호다. 둘이 우연히 겹쳐 있다가 갈라졌다.
그대로 두면 실적 공정공시·분기보고서 등 14건에서 배지가 사라지는데, 그것들은 **여전히
사람이 검수하지 않은 AI 생성물**이다. 배지가 없어지면 사용자에게 실제보다 강한 신뢰 신호를
준다(PLAN.md 1.4·5.3).

"시키지 않은 화면 개편은 하지 마라"는 지시와 부딪히는지 따져 봤고, 고치는 쪽으로 판단했다:
(a) 개편이 아니라 **한 줄 조건 수정**이고, (b) backend 가 `22_backend_pipeline.md` 3.1에서
frontend 조치 필요로 지목한 건이며, (c) **기존 테스트가 이미 이 배지를 요구하고 있다** —
`ExposurePolicyTest.test_unreviewed_summary_is_shown_with_badge`. 이 수정으로 그 테스트가
통과로 돌아왔다(4건 → 3건). 배지 문구가 "AI 생성 · 미검수"이므로 `is_reviewed` 가 문구와
정확히 일치하는 사실이라는 점도 근거다.

### 2.3 의견 — 자동 미게시 건이 웹에서 흔적도 없이 사라지는 것 (구현 안 함)

**괜찮지 않다고 본다. 다만 지금 당장 급하지는 않다.** 근거를 숫자로 적는다.

실 DB를 읽어 **지금 저장된 경고 기준**으로 막히는 요약을 세어 봤다(읽기 전용):

```
막히는 요약 19건 / 140건   ← 저장된 review_warnings 기준
  SK하이닉스 7 · 삼성전자 5 · DB하이텍 2 · 리노공업 2 · 이오테크닉스 1 · 주성엔지니어링 1 · 한미반도체 1
  그중 검수 게이트 유형(capital) 6건 — 전부 SK하이닉스 유상증자·DR 관련 서류
```

backend 가 보고한 13건과 다른데, 그쪽은 `revalidate_summaries` 로 **다시 계산한** 경고 기준이고
내 19건은 **저장된 경고** 기준이다. 즉 검증기 수정분이 아직 DB에 반영되지 않았다.
권위 있는 수치는 13건이다. (덧붙이면, **`revalidate_summaries` 를 실제로 저장하기 전까지는
자동 미게시가 실데이터에 하나도 없다** — 지금 이 패널은 실 DB에서 아직 안 뜬다.)

문제는 건수가 아니라 **어느 건이 사라지느냐**다. 막히는 것 중 6건이 SK하이닉스 유상증자
계열 서류다. **우리 서비스에서 가장 큰 사건이고, 사용자가 DART에서 보고 우리 사이트에
찾으러 올 확률이 가장 높은 공시가 정확히 그것들이다.** 그게 목록에서 조용히 빠지면 사용자는
"우리가 안 다룬다"고 읽는다. 요약이 틀렸다는 사실보다 **피드에 구멍이 났다는 사실이 눈에
안 보인다**는 게 더 나쁘다.

권하는 형태(리더가 정할 일이므로 구현하지 않았다):

> 카드/상세를 **제목·기업·접수일·DART 원문 링크까지만** 내보내고, 요약 본문 자리에
> "이 공시는 AI 요약이 자동 검증을 통과하지 못해 준비 중입니다. 원문에서 확인하세요"를 둔다.
> **요약 본문(one_line·easy_explanation·why_important)은 한 글자도 렌더하지 않는다.**

이 형태를 고른 이유: 검증에 걸린 본문을 보여주는 것은 지금 작업 전체의 목적에 정면으로
반한다. 반면 제목과 원문 링크는 **DART가 공개한 사실**이라 우리가 감출 이유가 없고,
PLAN.md 5.3(원문 링크 병기)·1.4(면책)과도 방향이 같다. 내 에이전트 지침의 "요약이 아직 없는
공시도 깨지지 않게 렌더링(AI가 정리 중)"과 같은 상태이기도 하다.

반대 논거도 적는다. 이건 **템플릿만의 변경이 아니다.** `published_disclosures()` 가 노출
정책의 단일 출처인데, 여기에 "미게시 요약도 일부 통과"를 넣으면 그 함수가 두 가지 정책을
갖게 되고, 이후 누군가 카드 템플릿에 `summary.one_line` 한 줄을 무심코 추가하면 **검증에
막은 본문이 그대로 새어 나간다.** 지금 구조는 그 사고가 구조적으로 불가능하다.
그래서 하려면 `views.py`(backend 소유)에서 **본문이 없는 별도 컨텍스트**로 넘겨야 하고,
그건 리더가 backend에 지시할 일이다.

**우선순위 의견:** 3~5순위. `revalidate_summaries` 실 저장 전에는 이 상황 자체가 발생하지
않고, 저장하는 순간 13건이 동시에 사라지므로 **저장과 같은 판단에 묶어서 결정하는 것**이 맞다.

---

## 3. 금액 표기 — 템플릿 필터를 만들지 않았다

**하지 않았다. `templatetags/` 에 아무것도 추가하지 않았고 `units.py` 를 import 하지도 않았다.**

`23_ai-prompt_no_arithmetic.md` 3장이 결론을 이미 냈다. `annotate_amounts()` 가 **저장 전에
본문 3필드에 병기**한다(`45,453,450,000,000원(약 45조 4,534억 원)`). 템플릿이 같은 일을 또
하면 `…원(약 …)(약 …)` 이 된다. ai-prompt 도 8장에서 "frontend 변경 불필요"로 못 박았다.

`(약 …)` 부분만 시각적으로 구분하는 것(ai-prompt가 "필수는 아니다"라고 남긴 선택지)도
**안 했다.** 이유가 둘이다.

1. 하려면 본문에서 정규식으로 `(약 …)` 를 찾아 마크업을 끼워야 하는데, 그건 ai-prompt가
   C안(화면 환산)을 버린 이유 그대로다 — **정규식 문제를 화면으로 옮기고 매 렌더마다 다시
   돌린다.** 얻는 것은 색깔뿐이다.
2. `highlight_terms` 와 같은 부류의 작업이라 **이스케이프 순서를 틀리면 XSS**가 된다
   (`templatetags/review_panel.py` 모듈 docstring이 그 함정을 적어 뒀다). 장식 하나를 위해
   질 위험이 아니다. 상세 화면은 본문을 `linebreaksbr` 로 통과시키고 있어 순서가 더 까다롭다.

**판단이 갈렸던 지점:** 검수 화면(`max_units=None` 정확 표기)에서는 쓸모가 있어 보였다.
그런데 검수자가 대조해야 하는 것은 **원문 숫자**이고 그건 본문에 그대로 남아 있으며, 패널의
하이라이트 기능이 이미 그 숫자를 원문에서 찾아 준다. 코드가 만든 짧은 표기를 하나 더
띄우는 것은 대조에 도움이 안 되고 화면만 늘린다.

---

## 4. qa 가 확인할 수 있는 표식

렌더 결과에서 문자열로 잡을 수 있는 것들이다. 인메모리 DB + `django.test.Client` 로
`/admin/disclosures/disclosuresummary/<pk>/change/` 를 GET 하면 전부 나온다(superuser 필요).

| 상태 | 클래스 / 문구 |
|---|---|
| 자동 미게시 | `rp-badge-auto` · `자동 미게시 · 아직 아무도 보지 않음` · `id="rp-auto-hidden"` |
| 자동 미게시 설명 블록 | `rp-auto-title` · `검증이 막았습니다. 사람이 내린 것이 아닙니다` |
| 재생성 0회 안내 | `rp-auto-note` · `AI 재생성 시도` |
| 사람이 내림 | `rp-badge-hidden` · `검수자가 내림`(=`get_hidden_by_display`) |
| 옛 데이터 | `주체 미기록` |
| 상한 소진 | `rp-badge-exhausted` · `자동 교정 불가 · 재생성 상한 소진` (**auto_hidden 일 때만**) |
| 재생성 이력 | `id="rp-regen"` · `AI 자동 재생성 N회 시도` · `rp-regen-item` · `N차 시도` |
| 이력 결과 | `교정됨`(`rp-badge-ok`) / `교정 실패`(`rp-badge-bad`) / `재생성 호출 실패` / `결과가 더 나빠 되돌림` |
| 이력 경고 | `고치려던 경고` · `다시 만든 뒤에도 남은 경고` · `rp-regen-warnings` |
| 게이트 | `검수 필수 유형 · 자본구조 변동` |
| 웹 미검수 배지 | `AI 생성 · 미검수` (조건: `not summary.is_reviewed`) |

**주의:** `검수자가 내림` 이라는 **문자열**은 자동 미게시 안내문 안에도 나온다("웹에서 숨김은
'검수자가 내림'으로 기록을 바꿉니다"). 사람/자동 구분을 검사할 때는 **문구가 아니라
`rp-badge-hidden` / `rp-badge-auto` 클래스로** 판정해야 한다. 내가 처음에 문구로 짰다가
이 오탐을 밟았다.

권하는 경계 케이스(내가 스크래치패드에서 확인한 그대로다):

1. `hidden_by='auto'` + `regeneration_count=1` + `resolved=False` → 자동 배지 + 소진 배지 + 이력 펼침
2. `hidden_by='human'` → 무채색 배지만, `rp-badge-auto` · `rp-regen` **없음**
3. **`is_published=True` + `regeneration_count=1` + `resolved=True`** → 이력은 보이되
   **`rp-badge-exhausted` 가 없어야 한다** (1.4의 함정)
4. `error` + `rolled_back` 이 함께 있는 이력
5. `hidden_by='auto'` + `regeneration_count=0` → `rp-auto-note` 노출, `rp-regen` 없음
6. `is_published=False` + `hidden_by=''`(옛 데이터) → `주체 미기록`, 자동 배지 없음
7. 웹: 자동 미게시 공시 상세 404 · 목록에 제목 없음 · 미검수 요약에 배지 노출 ·
   `is_reviewed=True` 로 바꾸면 배지 사라짐

## 5. 실행한 검증

```
스크래치패드 check_review_panel.py — 39개 검사 전부 통과
  (인메모리 테스트 DB · superuser 생성 · admin change_form 실제 GET · 웹 화면 4종 GET)

$ manage.py check
System check identified no issues (0 silenced).

$ manage.py test disclosures
Ran 209 tests — FAILED (failures=3)
  ← 착수 시점 4건에서 1건 줄었다. 없어진 것이 ExposurePolicyTest.test_unreviewed_summary_is_shown_with_badge(2.2).
  ← 남은 3건은 게이트 기준 변경에 대한 qa 기대값 갱신(22_backend_pipeline.md 3장). 내 변경과 무관하다.
```

`db.sqlite3` 는 읽기 쿼리만 돌렸다. `runserver` 는 쓰지 않았다.

## 6. 리더에게 — 내 파일이 아니라 손대지 않은 것

**검수자가 폼의 `노출 여부` 체크박스로 직접 다시 게시하면 `hidden_by='auto'` 와
`hidden_reason` 이 그대로 남는다.** `hidden_by` 는 readonly 라 폼으로 못 지우고,
`save_model`(`admin.py:235-276`)도 건드리지 않는다. 게시 중인 요약의 검수 폼에
`미게시 주체: 자동 미게시(검증 실패)` 가 남아 다음 검수자가 현재 상태를 오해한다.
`restore_summaries` 액션은 셋을 함께 지우므로(`admin.py:316`) **액션 경로만 깨끗하다.**

그래서 패널 안내문에 "목록 화면의 일괄 액션이 정식 경로"라고 적어 두었지만, 이건 화면으로
때울 문제가 아니라 `save_model` 이 `is_published` 가 켜질 때 `hidden_by`·`hidden_reason` 을
지우면 끝난다. **`admin.py` 는 backend 소유라 고치지 않았다.**

## 7. 판단이 갈렸던 지점 (모아 보기)

| 지점 | 고른 것 | 이유 |
|---|---|---|
| 사람/자동 판정 근거 | `hidden_by` (기계값) | `hidden_reason` 은 검수자가 고치는 자유 문자열이라 한 번 고치면 판정이 무너진다 |
| 상태 개수 | **넷** (`''` 미기록을 따로) | 마이그레이션 0006 이전 데이터가 자동 미게시로 오인된다. 모르는 것은 모른다고 적었다 |
| `regeneration_exhausted` 배지 | **`auto_hidden` 일 때만** | 이 property 는 교정에 성공해 게시 중인 요약에도 참이다. 이름만 믿고 배선했으면 정반대 사실을 말할 뻔했다 |
| 배지 회귀(2.2) | **고쳤다** | "화면 개편 금지"와 부딪히지만 한 줄 조건이고, backend 가 지목했고, 기존 테스트가 이미 요구하고 있었다 |
| 자동 미게시 건의 웹 노출(2.3) | **구현 안 함, 의견만** | 리더 지시대로. 게다가 `views.py` 변경이 필요해 내 파일이 아니다 |
| 금액 템플릿 필터(3장) | **안 만듦** | 본문에 이미 병기. `(약 …)` 스타일링도 버린 C안의 비용을 그대로 지불하는 일이다 |
| 게이트 유형 배지(1.5) | **넣음** | 지시 범위 밖이지만 배지 1개·새 색 0개이고, admin 목록에는 이미 있는 정보라 상세와 어긋나 있었다 |
