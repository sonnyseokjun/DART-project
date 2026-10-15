## Overview

A German automotive marketing system (BMW's model pages, configurator, and model
overview surfaces). The voice is engineered restraint: a near-black band opens the
page with one object named in enormous light type, the specification figures sit in
a lighter panel beside it, and everything below is white canvas separated by hairline
rules rather than cards. Nothing is rounded. Nothing floats. The page is built from
bands, columns, and 1px lines.

Typography does the work that photography does elsewhere: a 64px display name over a
wide-tracked 13px label, with a 300-weight body underneath. The system never fills a
shape to signal importance — it counts segments in a gauge, or it puts an 8px square
of semantic color next to ink-colored text. Color is scarce on purpose: one blue
({colors.primary}) for interactive affirmation, three semantic colors at 8px, and
otherwise grayscale.

**Key Characteristics:**
- Zero border radius everywhere — buttons, panels, inputs, chips. Squareness is the signature.
- Full-bleed near-black bands ({colors.surface-dark}) holding the hero and the closing call to action; white canvas between them
- Specification figures as a hairline-divided grid: value above (24px/700, tabular), name below (13px/700, +1.5px tracking, muted)
- Flat throughout — no shadow at any level. Hierarchy comes from surface value and 1px rules
- Wide-tracked bold 13px labels as the system's connective tissue: section links end in `›`
- Text links are labels, not sentences: 13px/700/+1.5px, never underlined

## Colors

### Brand & Accent
- **Primary Blue** ({colors.primary}) `#1c69d4`: Filled primary buttons, the current nav item's underline and text, focus rings.
- **Primary Active** ({colors.primary-active}) `#0653b6`: Pressed state of anything primary; pressed state of text links.

### Surface
- **Canvas** ({colors.canvas}) `#ffffff`: Page background, card surface.
- **Surface Soft** ({colors.surface-soft}) `#f7f7f7`: Secondary navigation band, footer.
- **Surface Card** ({colors.surface-card}) `#fafafa`: The upper plate of a list card — the panel the title sits in.
- **Surface Strong** ({colors.surface-strong}) `#ebebeb`: Notice strips, band rules.
- **Surface Dark** ({colors.surface-dark}) `#1a2129`: Hero and closing bands. A blue-shifted near-black, not neutral.
- **Surface Dark Elevated** ({colors.surface-dark-elevated}) `#262e38`: The figures panel inside a dark band.

### Line
- **Hairline** ({colors.hairline}) `#e6e6e6`: Row dividers, section rules, input borders.
- **Hairline Strong** ({colors.hairline-strong}) `#cccccc`: Secondary button borders, unfilled gauge segments, dashed outlines.
- **Hairline On Dark** ({colors.hairline-on-dark}) `#3a434e`: The same two roles inside a dark band.

### Text
- **Ink** ({colors.ink}) `#262626`: Headings, chip labels, figure values.
- **Body** ({colors.body}) `#3c3c3c`: Body copy.
- **Body Strong** ({colors.body-strong}) `#1a1a1a`: Lead paragraphs and quoted source text.
- **Muted** ({colors.muted}) `#6b6b6b`: Figure names, captions, timestamps, inactive tabs.
- **On Dark** ({colors.on-dark}) `#ffffff` / **On Dark Soft** ({colors.on-dark-soft}) `#bbbbbb`: Heading and body on a dark band.

### Semantic
Used **only** as an 8px square beside ink-colored text, never as a filled label.
- **Success** ({colors.success}) `#22c55e`: Verified, confirmed, matches source.
- **Warning** ({colors.warning}) `#f59e0b`: Needs checking.
- **Error** ({colors.error}) `#dc2626`: Destructive action, validation failure.

## Typography

### Font Family
**Pretendard** (SIL OFL, self-hosted variable). Fallbacks: `system-ui`,
`-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, `Roboto`, sans-serif.

Three weights carry the whole system: **300** for body and large display, **400** for
captions and small labels, **700** for headings and every interactive label. The
300/700 jump — not a middle weight — is what gives the system its tension.

### Hierarchy

| Token | Size | Weight | Line Height | Letter Spacing | Use |
|---|---|---|---|---|---|
| `{typography.display-xl}` | 64px | 700 | 1.05 | 0 | Hero object name |
| `{typography.display-lg}` | 48px | 700 | 1.10 | 0 | Band opener |
| `{typography.display-md}` | 32px | 700 | 1.15 | 0 | Section heading |
| `{typography.display-sm}` | 24px | 700 | 1.25 | 0 | Subsection heading, figure value |
| `{typography.title-lg}` | 20px | 700 | 1.30 | 0 | Panel title |
| `{typography.title-md}` | 18px | 700 | 1.40 | 0 | Card title |
| `{typography.title-sm}` | 16px | 700 | 1.40 | 0 | Inline emphasis in a meta row |
| `{typography.lead}` | 18px | 300 | 1.55 | 0 | Hero and section lead paragraph |
| `{typography.body}` | 16px | 300 | 1.55 | 0 | Body copy |
| `{typography.body-sm}` | 14px | 300 | 1.55 | 0 | Card copy, secondary body |
| `{typography.caption}` | 12px | 400 | 1.40 | +0.5px | Timestamps, status text, fine print |
| `{typography.label}` | 13px | 700 | 1.30 | +1.5px | Figure names, tabs, text links |
| `{typography.button}` | 14px | 700 | 1.00 | +0.5px | Button labels |
| `{typography.plate}` | 22px | 300 | 1.35 | 0 | The raw object name on a card plate |

### Principles
- Letter spacing is **zero on every heading**. The system widens tracking only on the 12–13px roles, where it reads as mechanical labelling rather than styling.
- `{typography.label}` is one role doing three jobs — figure name, tab, text link — which is why tabs and links look related.
- Figure values use `tabular-nums` so columns of numbers align vertically.
- The 300-weight lead paragraph beside a 700-weight display heading is the system's primary contrast device. Do not set leads at 400.

## Layout

### Spacing System
- **Base unit**: 4px.
- **Tokens**: `{spacing.xxs}` 4px · `{spacing.xs}` 8px · `{spacing.sm}` 12px · `{spacing.md}` 16px · `{spacing.lg}` 24px · `{spacing.xl}` 32px · `{spacing.xxl}` 48px · `{spacing.section}` 80px.
- **Band rhythm**: every band is `{spacing.section}` top and bottom, collapsing to 56px below 768px.
- **Card internal padding**: `{spacing.lg}` on the plate; the body below the plate has no side padding — it sits flush to the column.

### Grid & Container
- Container max-width **1440px**; gutter `{spacing.xxl}` 48px → 32px at 1024px → 16px at 768px.
- **Hero**: two columns, `1fr` and `minmax(0, 520–560px)`, 64px apart, vertically centered. Collapses to one column at 1024px.
- **Reading layout**: `minmax(0, 720px)` article + `minmax(0, 360px)` sticky aside, 64px apart.
- **Card grids**: 4 columns on marketing bands, 3 on list bands; 2 at 1024px, 1 at 768px.
- Alignment is **left throughout**. The only centered band is the closing call to action.

### Whitespace Philosophy
Whitespace is architectural, not atmospheric. Bands are tall and content is dense
inside them — a hero gives its heading 64px of type and then starts the body
immediately. Horizontal space is spent on the 64px gap between the two hero columns,
because that gap is what makes the right panel read as a separate instrument.

## Elevation & Depth

**There is no elevation.** No shadow appears at any level, including on sticky
headers and panels. Depth is produced by three means only:

| Device | Treatment |
|---|---|
| Surface value | `canvas` → `surface-card` → `surface-strong` → `surface-dark` |
| 1px rules | `hairline` between rows and sections; `hairline-strong` for interactive borders |
| Dashed outline | `1px dashed hairline-strong` on canvas marks a slot whose content has not arrived |

## Shapes

### Border Radius Scale
**One value: 0.** Buttons, inputs, chips, panels, plates, and bands are all square.
The only non-rectangular element in the system is the 44px circular hamburger button
below 768px, and the 8px semantic square (square, not a dot).

### Signature Shapes
- **Gauge** — three 12×4px segments, 2px apart. Filled segments count the level: 3 / 2 / 1.
- **Figure cell** — padding right, 1px right rule, 24px right margin; the last cell drops both. Below 768px the rule moves to the bottom edge.
- **Plate** — a `surface-card` rectangle holding a meta row at the top and the object name pushed to the bottom with `margin-top: auto`.

## Components

### Buttons
Minimum height 48px (36px for `small`), padding 14px 32px, 1px border, square.

| Variant | Rest | Pressed |
|---|---|---|
| `{components.button-primary}` | `primary` fill, white label | `primary-active` fill |
| `{components.button-secondary}` | canvas fill, `hairline-strong` border, ink label | `surface-soft` fill |
| `{components.button-on-dark}` | transparent, 1px white border, white label | `surface-dark-elevated` fill |

### Chips (filter)
Square, 1px `hairline-strong` border, canvas fill, 12px/400 label, 36px minimum
height. The **selected** chip inverts to an `ink` fill with a white label — not blue.
Blue is reserved for navigation and primary buttons; a filter is not navigation.

### Gauge and status
- `{components.gauge}` — the three-segment bar plus a 12px label, both `ink`.
- `{components.status}` — an 8px semantic square plus a 12px label. The label stays `ink` (or `muted` when the state is an absence); only the square carries the color.

### Tabs
13px/700/+1.5px, `muted`, 2px transparent bottom border. Current tab turns `ink` with
an `ink` bottom border. The top-level nav is the exception: its current item is
`primary` with a `primary` border.

### Text links
`{typography.label}`, ink (or white on dark), **never underlined**, ending in a
space and `›`. Pressed turns `primary-active`.

### Inputs
48px tall, 1px `hairline` border, square, 16px/300 text. Focus draws a 2px inset ink
ring rather than an outline, so the field's geometry does not shift.

## Motion

Essentially none. No entrance animation, no scroll reveal, no hover transition on
cards. The system's only motion is the mobile navigation sheet appearing, and even
that is an instant state change rather than a slide.

---

# 적용 규칙 — 이 서비스에 옮길 때

위 문서는 자동차 마케팅 사이트의 언어다. 이 서비스에는 자동차도, 사진도, 구매 흐름도
없다. 아래가 옮기는 방식이고, **`style.css`는 이 절의 결정을 코드로 옮긴 것이다.**
색·크기를 바꿀 일이 생기면 `style.css`가 아니라 여기를 먼저 고친다.

## 1. 차 사진 자리에 수치가 아니라 원문 구절이 들어간다

이 언어의 히어로는 **왼쪽에 차 이름, 오른쪽에 제원 패널**이다. 제원은 출력·토크·
0-100km/h 같은 **구조화된 값**이다.

우리 요약에는 그런 값이 없다 — `DisclosureSummary`는 한 줄 요약·쉬운 설명·왜 중요한가·
원문 근거(인용문)만 가진다. 계약 금액을 값으로 꺼내려면 출력 스키마와 프롬프트를 고치고
기존 요약을 전부 다시 만들어야 하는데, 그건 화면 작업이 아니라 요약 파이프라인 변경이다
(월 $6.5 상한 안에서 146건 재요약).

그래서 제원 패널 자리에 **원문에서 확인된 인용 구절**을 넣는다. 성격이 마침 맞는다 —
제원표처럼 이름(`claim`)과 값(`quote`)의 쌍이고, 숫자가 원 단위 그대로 들어 있어
`tabular-nums`가 제 일을 한다. 그리고 이 서비스의 가장 특징적인 물건이 **요약과 원문의
대조**라, 히어로에 놓을 것으로 요약 문장보다 낫다.

`.spec-value` / `.spec-label`의 위아래 관계는 뒤집는다 — 제원은 값이 위지만, 인용은
**어느 주장의 근거인지가 먼저** 읽혀야 한다.

수치 칸을 나중에 제대로 만들려면 이 패널의 **마크업을 그대로 두고 내용만 바꾸면 된다.**
그때 위아래를 다시 뒤집는다.

## 2. 중요도는 색이 아니라 칸 수다

이전 디자인 언어에서는 중요도 3단계가 **배지 세 가지 색**이었다. 이 언어는 그렇게 하지
않는다 — 색이 늘어나는 것을 막는 대신 **게이지의 칸 수**로 가른다(3 / 2 / 1).

이 바꿈이 PLAN.md 9.5의 요구("중요도 3단계와 검수 상태가 서로 구분될 것")를 더 잘
지킨다. 전에는 중요도와 검수 상태가 **둘 다 색 배지**라 한 줄에 네 개가 늘어서면 무엇이
중요도인지 알 수 없었다. 지금은 모양이 다르다.

| 뜻 | 표시 | 모양 |
|---|---|---|
| 중요도 높음 / 보통 / 낮음 | `.grade[data-level="high\|medium\|low"]` | 칸 3 / 2 / 1 + 먹 글자 |
| 사람이 검토·수정함 | `.status-done` | 초록 8px + 먹 글자 |
| AI 생성 · 미검수 | `.status-ai` | 테두리만 있는 8px + 회색 글자 |
| 수치 확인 필요 | `.status-warn` | 주황 8px + 먹 글자 |
| 요약 전 / 정리 중 / 단순 보고 | `.status-wait` | 점선 8px + 회색 글자 |

**미검수와 요약 대기만 8px 안이 비어 있는 이유:** 둘 다 *없음*의 신호다. 면을 칠하면
상태가 있는 것처럼 읽힌다. 요약 대기는 점선으로 한 번 더 갈라 "아직 오지 않음"을 말한다.

## 3. 검정 띠는 세 곳에만 둔다

이 언어는 검정 띠를 아낌없이 쓰지만, 여기서는 **읽는 화면을 검게 만들지 않는다.**
공시 요약은 길게 읽는 글이라 긴 본문이 검정 위에 오면 눈이 빨리 지친다.

검정 띠가 서는 곳은 셋이다.

- **소개 화면 히어로** — 로그인 전 첫 화면
- **공시 상세 히어로** — 공시명 + 한 줄 요약 + 원문 인용 패널까지. 본문(쉬운 설명부터)은 흰 바탕이다
- **닫는 띠** — 소개 화면과 공시 상세의 마지막. 공시 상세의 닫는 띠는 면책 고지를 다시 말하는 자리다

회원 홈의 **머리 공시 띠**도 검정이다(네 번째). 이것은 목록이 아니라 "지금 가장 봐야 할
공시 하나"라 히어로와 같은 역할이고, 바로 아래 목록은 흰 바탕이다.

머리말은 **어느 화면에서나 흰색**이다. 검정 머리말 변형은 만들지 않는다 — 소개 화면에서
검정 머리말과 검정 히어로가 붙으면 두 요소의 경계가 사라진다. 대신 알림 띠(`.message-band`)가
히어로 위에 올 때는 검정으로 칠해 흰 줄이 끼지 않게 한다.

## 4. 파랑의 자리

이 언어는 링크를 파랗게 하지 않는다 — 원 문서가 `a { color: inherit }`로 두고, 링크를
**색이 아니라 역할(`{typography.label}` + `›`)로** 알린다. 그래서 본문·바닥글·목록의
링크는 먹 글자이고, 문장 안에 섞인 링크만 밑줄로 가른다.

대비는 막는 요인이 아니다 — 재어 보면 `primary`가 흰 바탕에서 **5.21:1**, `primary-active`가
**7.18:1**로 둘 다 16px 본문에서 AA를 넘긴다. 쓰지 않는 것은 **파랑이 뜻하는 것을 하나로
묶어 두기 위한 선택**이다.

파랑이 서는 곳은 셋뿐이다.

- **채움 1차 버튼** — 파랑 면 위의 흰 글자
- **머리말에서 지금 보는 메뉴** — 파란 글자 + 2px 파란 밑줄. 밑줄이 같은 말을 한 번 더 하므로 색만으로 전달하지 않는다
- **키보드 초점 테두리** — 2px, 2px 바깥쪽

필터 칩의 켜진 상태는 파랑이 아니라 **먹 채움**이다. 한 화면에서 파랑이 "누르면 다른
곳으로 간다"와 "지금 고른 조건"을 동시에 뜻하면 둘 다 흐려진다.

## 5. 쓰지 않는 것

원 문서의 구성 요소 중 이 서비스에 해당 화면이 없는 것들 — 색상·휠 선택기, 가격 패널,
금융 계산기, 시승 예약 폼, 전시장 찾기 지도, 360° 뷰어 — 는 **쓰지 않는다.** 모양만 빌려
다른 뜻으로 쓰지도 않는다.

`hover`는 **따로 만들지 않는다.** 이 언어가 기본·눌림만 규정하므로 마우스 hover에는
눌림(pressed) 값을 그대로 쓴다. 없는 상태를 지어내지 않으면서 마우스 사용자도 반응을
받는다.

## 6. 한글

- 어절 중간에서 잘리지 않게 `word-break: keep-all`을 `body`에 둔다.
- 제목 자간은 0을 지킨다. 한글은 제목에서 자간을 좁히면 뭉친다 — 원 문서가 제목 자간을 0으로 둔 것이 마침 한글에 맞다.
- `+1.5px` 자간은 **13px 라벨에만** 쓴다. 한글은 자간을 넓히면 낱자가 흩어져 읽기 어려워지므로, 이 역할의 글자는 가능하면 짧게(두 어절 이내) 쓴다.
- 금액은 `tabular-nums`로 세로줄을 맞춘다. 접수번호처럼 **값이 아니라 식별자**인 것에만 시스템 모노를 쓴다.
