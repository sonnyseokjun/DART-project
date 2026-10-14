## Overview

Meta's commerce surfaces (homepage, Quest configurator, Ray-Ban product detail, prescription page) read as a confident hardware merchandiser. The brand voice is photography-first: large, full-bleed product imagery dominates above-the-fold real estate, with white space and tight typographic hierarchy carrying the rest. The system has a recognizable dual-CTA pattern — a black pill-shaped primary on marketing surfaces shifting to a saturated cobalt blue ({colors.primary}) inside the buy-now flows, paired with an outlined ghost button for secondary navigation.

Optimistic VF — Meta's variable display face — anchors the entire system, ranging from a 64px hero display down to a 12px caption. The face's `ss01` and `ss02` stylistic sets are switched on across every heading role, contributing to the brand's slightly humanist, friendly geometric character. Below 768px the system collapses cleanly: hero stacks, pill nav becomes a hamburger, three-up feature grids flatten to a single column, and product configurators drop their right-rail summary into a sticky bottom bar.

**Key Characteristics:**
- Stark white canvas ({colors.canvas}) carrying full-bleed product photography with `{rounded.xxxl}` (32px) corner softening on showcase tiles
- Two-tier primary button system: marketing CTAs use {colors.ink-button} pills; commerce CTAs use {colors.primary} cobalt pills inside buy-now panels
- Optimistic VF as the universal display + body face with consistent `ss01, ss02` OpenType features
- Pill-shaped buttons ({rounded.full}) and `{rounded.xxxl}`/`{rounded.feature}` cards as the dominant geometric signature
- Saturated promotional banners (yellow {colors.warning}, dark {colors.ink-deep}) used sparingly above the nav for time-bound offers
- Photographic feature cards with no card chrome (no border, no shadow) — the product imagery IS the surface treatment

## Colors

> Source pages: meta.com/ (homepage), /ai-glasses/ray-ban-meta-skyler-gen-2/ (PDP), /quest/quest-3s/buy-now/ (configurator), /ai-glasses/prescription/ (lens upsell). Token coverage was identical across all four pages — the design system is genuinely unified.

### Brand & Accent
- **Cobalt Primary** ({colors.primary}): The buy-now CTA color. Used on every "Add to cart", "Configure", "Pre-order" button inside the commerce flow and the right-rail purchase panel.
- **Deep Cobalt** ({colors.primary-deep}): Pressed-state and dark-surface variant of the cobalt primary; also the active link color.
- **Soft Cobalt** ({colors.primary-soft}): Translucent background tint for informational callouts (`{colors.primary-soft}` at 15% alpha).
- **Facebook Blue** ({colors.fb-blue}): Selected radio/checkbox state and inline form-control activation color.
- **Meta Link Blue** ({colors.meta-link}): Reserved for legacy navigation and footer link affordances.
- **Oculus Purple** ({colors.oculus-purple}): VR product accent — used inside Quest-branded surfaces for category emphasis.

### Surface
- **Canvas White** ({colors.canvas}): Page background and primary card surface.
- **Soft Cloud** ({colors.surface-soft}): Subtle product-thumbnail and warranty-card background; also the search-pill rest state.
- **Hairline Gray** ({colors.hairline}): 1px input border and form-control divider.
- **Hairline Soft** ({colors.hairline-soft}): Quieter divider used on cards, footer separators, and section breaks.

### Text
- **Deep Ink** ({colors.ink-deep}): Primary headline and body text on light surfaces.
- **Ink** ({colors.ink}): Standard body and secondary headline text.
- **Charcoal** ({colors.charcoal}): Tertiary body text and form-button labels.
- **Slate** ({colors.slate}): Section-header copy and supporting microcopy.
- **Steel** ({colors.steel}): Quieter caption text and footer link hierarchy.
- **Stone** ({colors.stone}): Disabled or de-emphasized labels.

### Semantic
- **Success** ({colors.success}): "In stock", "Free returns" affirmations.
- **Attention** ({colors.attention}): Mid-priority alerts and timed callouts.
- **Warning** ({colors.warning}): Promotional banners ("Get 25% off…") and limited-time tags.
- **Critical** ({colors.critical}): Validation errors, destructive feedback.
- **Critical Strong** ({colors.critical-strong}): Form-input error border and inline error labels.

## Typography

### Font Family
**Optimistic VF** is Meta's proprietary variable display face. Fallbacks: Montserrat, Helvetica, Arial, Noto Sans. The variable axes carry from 300 (light heading-md, used for editorial intro headlines like "Look forward") through 500 (display, hero, heading-sm) up to 700 (subtitle, body emphasis, button labels). Stylistic sets `ss01` and `ss02` are switched on across every heading role — they soften the geometry and give the type a slightly humanist breathing.

A secondary Helvetica fallback chain is used for technical microcopy (12px) inside spec sheets and footer fine print.

### Hierarchy

| Token | Size | Weight | Line Height | Letter Spacing | OpenType | Use |
|---|---|---|---|---|---|---|
| `{typography.hero-display}` | 64px | 500 | 1.16 | 0 | ss01, ss02 | Homepage hero ("Get 25% off…", category opener) |
| `{typography.display-lg}` | 48px | 500 | 1.17 | 0 | ss01, ss02 | Section-opener display ("Made for prescriptions. Built for comfort.") |
| `{typography.heading-lg}` | 36px | 500 | 1.28 | 0 | ss01, ss02 | Subsection headlines ("Why buy from Meta", "Tech specs") |
| `{typography.heading-md}` | 28px | 300 | 1.21 | 0 | ss01, ss02 | Editorial subheads in lighter weight ("Look forward", "Built for prescriptions") |
| `{typography.heading-sm}` | 24px | 500 | 1.25 | 0 | ss01, ss02 | Card titles, feature-tile headers |
| `{typography.subtitle-lg}` | 18px | 700 | 1.44 | 0 | — | Bold callouts, FAQ question titles |
| `{typography.subtitle-md}` | 18px | 400 | 1.44 | 0 | — | Body lead and longer-line subtitles |
| `{typography.body-md}` | 16px | 400 | 1.50 | -0.16px | — | Primary body text |
| `{typography.body-md-bold}` | 16px | 700 | 1.50 | -0.16px | — | Body emphasis and link-md |
| `{typography.body-sm}` | 14px | 400 | 1.43 | -0.14px | — | Secondary body, helper text |
| `{typography.body-sm-bold}` | 14px | 700 | 1.43 | -0.14px | — | Pill tab labels, footer headings |
| `{typography.caption-bold}` | 12px | 700 | 1.33 | 0 | — | Badge labels, timestamps |
| `{typography.caption}` | 12px | 400 | 1.33 | 0 | — | Footer fine print, legal microcopy |
| `{typography.button-md}` | 14px | 700 | 1.43 | -0.14px | — | Pill button labels |
| `{typography.link-md}` | 16px | 700 | 1.50 | -0.16px | — | Inline navigation links |

### Principles
- Negative letter-spacing on body roles (`-0.14px` to `-0.16px`) tightens the type fractionally — Optimistic VF was designed for this snug-but-not-condensed setting.
- Editorial subheads use the 300 weight to introduce visual rest between the 500-weight display headlines and the 400-weight body, creating a three-tier visual rhythm.
- All headings carry `ss01, ss02` stylistic sets together — the design treats them as a paired alternates package, never one without the other.
- Buttons, pill tabs, and footer headings share `{typography.body-sm-bold}` (14px / 700 / -0.14px), creating a tight visual relationship between interactive elements.

## Layout

### Spacing System
- **Base unit**: 4px increment with 8px as the dominant primary step.
- **Tokens**: `{spacing.xxs}` (4px) · `{spacing.xs}` (8px) · `{spacing.sm}` (10px) · `{spacing.md}` (12px) · `{spacing.base}` (16px) · `{spacing.lg}` (20px) · `{spacing.xl}` (24px) · `{spacing.xxl}` (32px) · `{spacing.xxxl}` (40px) · `{spacing.section-sm}` (48px) · `{spacing.section}` (64px) · `{spacing.section-lg}` (80px) · `{spacing.hero}` (120px).
- **Section rhythm**: Marketing sections separate at `{spacing.section-lg}` (80px); product detail sections compress to `{spacing.section}` (64px); FAQ stacks tighten further to `{spacing.xxl}` (32px).
- **Card internal padding**: Standard `{spacing.xxl}` (32px); icon-feature tiles compress to `{spacing.xl}` (24px); promo-strip cards expand to `{spacing.section}` (64px) for hero presence.

### Grid & Container
- Marketing page max-width sits around 1280px with 32–48px gutters.
- The PDP layout uses a 2-column split: hero gallery (~58% width) + sticky purchase rail (~42%, with `max-width: 380px` on the rail).
- Three-up feature grids ("Why buy from Meta") use a 24px column gap; six-up product thumbnail rows (color/SKU pickers) use a 12px gap.

### Whitespace Philosophy
Whitespace is product-photography-first. Hero sections give product imagery 50–70% of the viewport height; copy is given oxygen to breathe in `{spacing.xxl}` to `{spacing.xxxl}` blocks above and below. Inside the configurator, whitespace tightens — the buy-now panel is information-dense, with `{spacing.base}` to `{spacing.lg}` rhythm between option groups.

## Elevation & Depth

The system runs predominantly flat. Elevation is reserved for two interaction layers:

| Level | Treatment | Use |
|---|---|---|
| 0 (flat) | No shadow; `{rounded.xxxl}` rounding + `{colors.hairline-soft}` border | Default product cards, why-buy tiles |
| 1 (subtle) | `rgba(0, 0, 0, 0.2) 1px 1px 0px 0px` | Pill-tab activation indicator |
| 2 (sticky panel) | `rgba(20, 22, 26, 0.3) 0px 1px 4px 0px` | PDP right-rail purchase summary, sticky mobile checkout bar |

### Decorative Depth
- Photography-as-depth: full-bleed product imagery on `{rounded.xxxl}` cards creates atmospheric layering without shadows.
- Translucent overlays (`rgba(255, 255, 255, 0.1)` to `rgba(10, 19, 23, 0.12)`) cover dark hero photography to lift legibility of overlaid text.
- Decorative pastel tints inside accessory cards — soft pink, ice-blue, mint — appear briefly behind product cutouts but are NOT formalized as system tokens (treated as photographic content).

## Shapes

### Border Radius Scale

| Token | Value | Use |
|---|---|---|
| `{rounded.xs}` | 2px | Inline checkbox marks, fine UI corners |
| `{rounded.sm}` | 4px | Tags, micro-controls |
| `{rounded.md}` | 6px | Square thumbnail rounding |
| `{rounded.lg}` | 8px | Form inputs, radio-option containers |
| `{rounded.xl}` | 16px | Standard feature cards, FAQ accordion items |
| `{rounded.xxl}` | 24px | Warranty / accessory tiles, ghost-style action cards |
| `{rounded.xxxl}` | 32px | Photographic feature cards, big promo strips |
| `{rounded.feature}` | 40px | Accessory hero panels, "Built for prescriptions" cards |
| `{rounded.full}` | 100px | Pill buttons, tab chips, badges |
| `{rounded.circle}` | 50% | Color swatches, circular icon buttons |

### Photography Geometry
- Product hero photography sits in `{rounded.xxxl}` (32px) frames more often than rectangles.
- Color/material swatches are perfect circles (`{rounded.circle}`, 32px diameter, 2px white border ring when selected).
- Square product thumbnails (`aspect-ratio: 1/1`) use `{rounded.xl}` rounding.
- Six-up "color & SKU" picker rows use 1:1 aspect tiles with `{rounded.lg}` (8px) corners — tighter than the hero photography frames to differentiate selection-grid context from showcase context.

## Components

> Per the no-hover policy, hover states are NOT documented for any component below. Default and pressed/active states only.

### Buttons

**`button-primary`** — Black pill primary CTA for marketing surfaces ("Shop", "Pre-order").
- Background `{colors.ink-button}`, text `{colors.on-ink-button}`, typography `{typography.button-md}`, padding `14px 30px`, rounded `{rounded.full}`.
- Pressed state `button-primary-pressed` flips background to `{colors.charcoal}`.
- Disabled state `button-primary-disabled` uses `{colors.disabled-text}` background.

**`button-buy-cta`** — Cobalt pill primary CTA for commerce flows ("Add to cart", "Configure", "Continue").
- Background `{colors.primary}`, text `{colors.on-primary}`, typography `{typography.button-md}`, padding `14px 30px`, rounded `{rounded.full}`.
- Pressed state `button-buy-cta-pressed` deepens background to `{colors.primary-deep}`.
- This variant ONLY appears inside the buy-now configurator and PDP purchase rail. Marketing surfaces use `button-primary` instead.

**`button-secondary`** — Outlined ghost CTA, often paired with primary in dual-CTA hero patterns.
- Background transparent, text `{colors.ink-deep}`, border `2px solid {colors.ink-deep}`, typography `{typography.button-md}`, padding `12px 28px`, rounded `{rounded.full}`.

**`button-ghost`** — Quieter outlined variant used for tertiary CTAs.
- Background transparent, text `{colors.ink-deep}`, border `2px solid rgba(10, 19, 23, 0.12)`, typography `{typography.button-md}`, padding `10px 22px`, rounded `{rounded.full}`.

**`button-pill-tab`** + **`button-pill-tab-active`** — Top-of-page category navigation pills ("Glasses / Quest / Apps").
- Inactive: background `{colors.canvas}`, text `{colors.ink}`, border `1px solid {colors.hairline}`, padding `8px 16px`, rounded `{rounded.full}`.
- Active: background `{colors.ink-deep}`, text `{colors.canvas}`. No border in active state — the dark fill replaces it.

**`button-icon-circular`** — 40×40px circular utility buttons (carousel arrows, share, favorite).
- Background `{colors.canvas}`, icon color `{colors.ink}`, rounded `{rounded.circle}`.

### Cards & Containers

**`card-product-feature`** — White feature card with product photography and copy (homepage "Designed for sport", "Advanced. Inside and out.").
- Background `{colors.canvas}`, rounded `{rounded.xxxl}`, padding `{spacing.xxl}`, border `1px solid {colors.hairline-soft}`.

**`card-feature-photo`** — Edge-to-edge photographic showcase tile with NO chrome (homepage "Built for prescriptions" full-bleed glasses card).
- Background `{colors.canvas}` (visible only at the corners), rounded `{rounded.xxxl}`, no padding, no border. The image fills the card; copy is overlaid bottom-left in white.

**`card-promo-strip`** — Dark full-width promo card with embedded copy + CTAs (homepage "Meta Quest brings the magic of virtual reality" wide strip).
- Background `{colors.ink-deep}`, text `{colors.canvas}`, rounded `{rounded.xxxl}`, padding `{spacing.section}`.

**`card-icon-feature`** — Three-up feature tile with line icon, headline, and short copy ("Free 2-day delivery", "Free 30-day returns", "Worry-free warranty", "Buy now, pay later").
- Background `{colors.canvas}`, rounded `{rounded.xl}`, padding `{spacing.xl}`, border `1px solid {colors.hairline-soft}`.

**`card-checkout-summary`** — PDP right-rail sticky summary with title, price, color picker, "Add to cart" button.
- Background `{colors.canvas}`, rounded `{rounded.xl}`, padding `{spacing.xl}`, border `1px solid {colors.hairline-soft}`, shadow `rgba(20, 22, 26, 0.3) 0px 1px 4px 0px`.

**`product-thumbnail`** — Square product image cell used in color/SKU pickers and "People also bought" rows.
- Background `{colors.surface-soft}`, rounded `{rounded.xl}`, padding `{spacing.base}`, aspect-ratio `1 / 1`.

**`warranty-card`** — Promo callout for warranty + finance offers ("1y Warranty", "Meta Horizon+").
- Background `{colors.surface-soft}`, rounded `{rounded.xxl}`, padding `{spacing.xxl}`. Uses pastel-tinted variants for additional perks.

**`why-buy-tile`** — 4-up reassurance tile row in the lower marketing zone.
- Background `{colors.canvas}`, rounded `{rounded.xl}`, padding `{spacing.xxl} {spacing.xl}`, border `1px solid {colors.hairline-soft}`. Heading in `{typography.subtitle-lg}`, body in `{typography.body-sm}`.

### Inputs & Forms

**`text-input`** — Standard form field (footer email subscribe, support form).
- Background `{colors.canvas}`, text `{colors.ink}`, border `1px solid {colors.hairline}`, rounded `{rounded.lg}`, padding `{spacing.md}`, height 44px.

**`text-input-focused`** — Activated state.
- Border switches to `2px solid {colors.fb-blue}`.

**`text-input-error`** — Validation error state.
- Border switches to `1px solid {colors.critical-strong}`; error label below in `{colors.critical-strong}` `{typography.body-sm}`.

**`search-pill`** — Top-nav search field.
- Background `{colors.surface-soft}`, text `{colors.steel}`, typography `{typography.body-sm}`, rounded `{rounded.full}`, height 40px.

**`radio-option`** + **`radio-option-selected`** — Configurator option cards (storage, color variant, shipping option).
- Inactive: background `{colors.canvas}`, rounded `{rounded.lg}`, padding `{spacing.lg}`, border `1px solid rgba(10, 19, 23, 0.12)`.
- Selected: border switches to `2px solid #0143b5` (deep cobalt) — the cobalt theme persists into form-control selection signaling.

**`color-swatch-circle`** — Round color/material picker (Ray-Ban frame finishes, glass colors).
- 32px diameter, `{rounded.circle}`, `2px solid {colors.canvas}` ring on selection over the swatch's own fill color.

### Badges & Status

**`badge-promo-yellow`** — Limited-time offer chip ("Limited time", "Sale").
- Background `{colors.warning}`, text `{colors.ink-deep}`, typography `{typography.caption-bold}`, rounded `{rounded.full}`, padding `4px 10px`.

**`badge-attention`** — Mid-priority status indicator ("Almost gone", "Selling fast").
- Background `{colors.attention}`, text `{colors.canvas}`, typography `{typography.caption-bold}`, rounded `{rounded.full}`, padding `4px 10px`.

**`badge-success`** — Affirmative status ("In stock", "Verified", "Free shipping").
- Background `{colors.success}`, text `{colors.canvas}`, typography `{typography.caption-bold}`, rounded `{rounded.full}`, padding `4px 10px`.

**`badge-critical`** — Urgent/destructive label ("Out of stock", "Discontinued", error chips).
- Background `{colors.critical}`, text `{colors.canvas}`, typography `{typography.caption-bold}`, rounded `{rounded.full}`, padding `4px 10px`.

**`promo-banner`** — Sticky full-width promotional strip ABOVE the top nav ("Get 25% off the #1 selling AI glasses").
- Background `{colors.ink-deep}` (or `{colors.warning}` for yellow promo variants), text `{colors.canvas}` (or `{colors.ink-deep}` on yellow), typography `{typography.body-sm-bold}`, padding `{spacing.md} {spacing.xl}`. Carries one-line offer copy plus an inline CTA link.

### Navigation

**Top Navigation (Desktop)** — Sticky white bar with category pill tabs, search, account, cart.
- Background `{colors.canvas}`, height ~64px with bottom `1px solid {colors.hairline-soft}`.
- Left: Meta wordmark logo (61×14px). Center: pill-tab category nav. Right: search-pill + circular icon buttons (account, cart).

**Top Navigation (Mobile)** — Compressed to logo + hamburger + cart icon. Pill-tab nav slides into a full-screen drawer below 768px.

**Promo Banner** — Full-width strip ABOVE the nav for time-bound offers.
- Background `{colors.ink-deep}` or `{colors.warning}` (yellow), text `{colors.canvas}` or `{colors.ink-deep}`, typography `{typography.body-sm-bold}`, padding `{spacing.md} {spacing.xl}`. Carries one-line offer copy + inline link.

**Breadcrumb (PDP)** — Inline path above the product hero ("Glasses › Ray-Ban Meta › Skyler (Gen 2)").
- Typography `{typography.body-sm}`, separator dot in `{colors.stone}`, active leaf in `{colors.ink}`, parent links in `{colors.steel}`.

### Signature Components

**`hero-band-marketing`** — Full-bleed photographic hero with overlaid copy + dual-CTA pair.
- Edge-to-edge product photography on a dark or photographic background. Overlay copy in `{typography.hero-display}` white. Below the title: 1-line subtitle in `{typography.subtitle-md}` then `button-primary` + `button-secondary` pair.

**`product-gallery-pdp`** — Product detail page main hero: 4-up vertical thumbnail strip on the left, large product image center, sticky purchase rail right.
- Thumbnails: 80×80px, `{rounded.lg}`, `{colors.surface-soft}` background, 1px `{colors.hairline-soft}` border (active border switches to `{colors.ink-deep}`).
- Main image area: ~720×720px on desktop, `{rounded.xxxl}` rounding, photographic surface.
- Sticky rail uses `card-checkout-summary`.

**`color-sku-picker-row`** — Six-up grid of square product variants with name + price below each.
- Tile background `{colors.surface-soft}`, rounded `{rounded.lg}`, image padded `{spacing.base}`. Active tile border switches to `2px solid {colors.ink-deep}`. Below the tile: variant name in `{typography.body-sm-bold}` and price in `{typography.body-sm}`.

**`feature-icon-row`** — Four reassurance benefits ("Free 2-day delivery", "Free 30-day returns", "Worry-free warranty", "Buy now, pay later").
- 4-column grid, each cell uses `card-icon-feature` chrome with a 32px line icon at top, headline `{typography.subtitle-lg}`, body `{typography.body-sm}`.

**`faq-accordion`** — Vertical stack of expandable Q&A items.
- Each item uses `faq-accordion-item` chrome. Question in `{typography.subtitle-lg}` left, chevron icon (`{colors.steel}`, 20px) right. Expanded answer drops in `{typography.body-md}` text below with `{spacing.base}` top padding.

**`tech-specs-table`** — Two-column key/value table for product specifications.
- Row layout: spec label (`{typography.body-sm-bold}` `{colors.ink}`) left, spec value (`{typography.body-sm}` `{colors.charcoal}`) right. Row separator `1px solid {colors.hairline-soft}`. Section headers in `{typography.heading-sm}` above each spec group.

**`testimonial-customer-card`** — Small editorial card with author + quote + photo.
- Background `{colors.canvas}`, rounded `{rounded.xl}`, padding `{spacing.xxl}`, border `1px solid {colors.hairline-soft}`. Avatar circle 40px, byline in `{typography.body-sm-bold}`, quote in `{typography.body-md}`.

**`footer-region`** — Dense multi-column site footer.
- Background `{colors.canvas}`, top border `1px solid {colors.hairline-soft}`, padding `{spacing.section} {spacing.xxl}`. Six column groups with section headings in `{typography.body-sm-bold}` `{colors.ink}` and link lists in `{typography.body-sm}` `{colors.steel}`. Bottom row contains language picker, region selector, legal links in `{typography.caption}` `{colors.stone}`.

## Do's and Don'ts

### Do
- Reserve `{colors.primary}` (cobalt) for buy-now CTAs only — its visual weight is meaningful precisely because it doesn't appear on marketing pages.
- Use `{colors.ink-button}` (black) for marketing-surface primary CTAs. Pair with `{colors.button-secondary}` ghost outline for the secondary action.
- Apply `{rounded.full}` to every button, every category pill, every badge, every chip — buttons are NEVER squared in Meta's system.
- Apply `{rounded.xxxl}` to photographic product cards and `{rounded.xl}` to icon-feature tiles to maintain the visible card-hierarchy contrast.
- Switch on `ss01, ss02` together for any Optimistic VF heading. Never one stylistic set without the other.
- Use the 300-weight `{typography.heading-md}` for editorial subheads — it creates the brand's signature visual rhythm against the 500-weight displays.

### Don't
- Don't use `{colors.primary}` (cobalt) for marketing-surface primary buttons — it conflicts with Meta's brand-history positioning of black-CTA-on-white-canvas marketing.
- Don't introduce additional accent colors beyond cobalt + Oculus purple. The hardware brand is deliberately monochromatic outside its product photography.
- Don't soften the corners of pill buttons below `{rounded.full}`. The pill is a brand signature.
- Don't run feature cards without rounding — `{rounded.xxxl}` is the minimum for any photographic surface.
- Don't reduce `{typography.body-md}` line-height below 1.50 — the negative letter-spacing already tightens the metric and any further compression breaks legibility.
- Don't apply heavy shadows to marketing cards. Elevation is a commerce-flow signal, not a marketing flourish.

## Responsive Behavior

### Breakpoints
| Name | Width | Key Changes |
|---|---|---|
| Mobile (small) | < 480px | Single column. Hero text drops to `{typography.display-lg}` or smaller. Pill tabs collapse into hamburger drawer. PDP gallery stacks above purchase rail; rail becomes sticky bottom bar. |
| Mobile (large) | 480 – 767px | Same as small but feature tiles render two-up. |
| Tablet | 768 – 1023px | Two-column feature grids. Pill-tab nav returns. PDP gallery + purchase rail render side-by-side at compressed proportions (~60/40). |
| Desktop | 1024 – 1359px | Full three- and four-up feature grids; full pill-tab category nav; PDP at standard 58/42 split. |
| Wide Desktop | ≥ 1360px | Same as desktop with wider hero gutters and larger product photography. |

### Touch Targets
- Pill buttons render at 40–44px effective height (with the 14px button text + `14px 30px` padding). Above the WCAG AAA 44px floor.
- Circular icon buttons are 40×40px — at the AA floor; bumps to 44×44px on mobile via override.
- Color swatch circles are 32×32px. To hit AAA, the swatch carries a 12px clear hit zone around it (effective hit target ~56px).
- Form inputs render at 44px height to align with primary button height.

### Collapsing Strategy
- **Promo banner** stays full-width on all breakpoints; truncates with ellipsis on small mobile, retains the inline link affordance.
- **Pill-tab nav** below 768px collapses into a hamburger drawer; the active tab is rendered as a label inside the closed nav.
- **PDP layout**: gallery stacks above the purchase summary at < 1024px; the summary becomes a sticky bottom bar with price + "Add to cart" button at < 768px. The full summary remains scrollable above the sticky bar.
- **Feature grids** (3-up, 4-up) collapse to 2-up at 768–1023px and 1-up at < 768px. Card padding compresses from `{spacing.xxl}` to `{spacing.xl}` at the 1-up breakpoint.
- **Hero typography**: `{typography.hero-display}` (64px) drops to `{typography.heading-lg}` (36px) at < 768px and `{typography.heading-sm}` (24px) at < 480px.
- **Footer**: 6-column desktop layout reflows to 2-column at tablet and accordion-collapsed groups at mobile.

### Image Behavior
- Product photography uses 1:1 (thumbnails, color pickers), 4:3 (PDP gallery), and 16:9 (homepage promo strips) ratios.
- Hero photography is full-bleed with `{rounded.xxxl}` corners; lazy-loaded below the fold.
- Product variant images preserve their `{rounded.lg}` thumbnails across all breakpoints — they never go full-width on mobile.

## Iteration Guide

1. Focus on ONE component at a time. The system has high internal consistency — small precision wins compound.
2. Reference component names and tokens directly (`{colors.primary}`, `{component-name}-pressed`, `{rounded.full}`) — do not paraphrase.
3. Run `npx @google/design.md lint DESIGN.md` after edits to catch broken refs, contrast issues, orphaned tokens.
4. Add new variants as separate `components:` entries (`-pressed`, `-disabled`, `-focused`) — do not bury them inside prose.
5. Default to `{typography.body-md}` for body and `{typography.subtitle-lg}` for emphasis. Headlines step down through `hero-display → display-lg → heading-lg → heading-md → heading-sm`.
6. Keep `{colors.primary}` (cobalt) scarce. If it appears outside the buy-now flow on a viewport, ask whether the surface really needs to look like a checkout panel.
7. Pill-shaped buttons (`{rounded.full}`) always; squared buttons signal "third-party widget" in this design language and should be filtered out of any work surface.

## Known Gaps

- Selected/checked states for non-button form controls (toggle, multi-select) were not visible on the captured surfaces — implement following the cobalt-on-white pattern from `radio-option-selected`.
- Animation/transition timings are not extracted; recommend 150–250ms ease-out for primary surface transitions and 300ms ease-in-out for accordion expand/collapse.
- Specific dark-mode token values for canvas, surface, ink, and hairline are not defined; the brand has not surfaced a published dark-mode token set on these commerce pages.
- Pastel decorative tints inside accessory cards (soft pink, ice blue, mint) appear visually but are not formalized — treat them as photographic content, not as system colors.

---

# 적용 규칙 — 공시 쉽게 보기

위 문서는 **영문 하드웨어 커머스 사이트**에서 뜬 디자인 언어다. 사진이 한 장도 없고
파는 물건도 없는 한글 서비스에 옮기려면 비는 자리를 메워야 한다. 그 결정을 여기 적는다.
구현(`style.css`)은 이 절까지 포함해 따른다. 값을 바꿀 일이 생기면 여기를 먼저 고친다.

## 1. 색 — 값 확정

위 문서는 토큰 **이름만** 적고 hex를 적지 않는다(`{colors.primary}` 식). 문서 안에
직접 박힌 값 둘만 확정값이고, 나머지는 아래로 고정한다. 대비비는 모두 실제로 계산했다.

| 토큰 | 값 | 출처 / 근거 |
|---|---|---|
| `primary` (코발트) | `#0064e0` | 커머스 전용. **이 서비스에는 등장하지 않는다**(아래 4번) |
| `primary-deep` | `#0143b5` | **문서에 명시**(`radio-option-selected`). 본문 링크 색도 이것 |
| `primary-soft` | `rgba(0,100,224,.15)` | 문서가 "15% alpha"로 규정 |
| `fb-blue` | `#0866ff` | 입력 포커스 테두리 **+ 히어로 제목**(아래 7번) |
| `ink-deep` | `#0a1317` | **문서에서 도출** — `rgba(10,19,23,.12)`가 두 번 나온다 |
| `ink-button` | `#14161a` | **문서에서 도출** — 그림자 `rgba(20,22,26,.3)` |
| `ink` | `#1c2b33` | 본문 강조 |
| `charcoal` | `#465a69` | 1차 버튼 눌림 상태 |
| `slate` | `#52646f` | 흰 바탕 5.6:1 |
| `steel` | `#5e6b73` | 흰 바탕 5.4:1 |
| `stone` | `#8a8d91` | **비활성 전용** — 읽어야 하는 글자에 쓰지 않는다 |
| `canvas` | `#ffffff` | |
| `surface-soft` | `#f1f4f7` | |
| `hairline` | `#dadde1` | |
| `hairline-soft` | `#e9ebee` | |
| `success` | `#1a7f37` | 흰 글자 5.1:1 |
| `attention` | `#9a5b00` | 흰 글자 5.4:1 |
| `warning` | `#f7b928` | 먹 글자 10.4:1 |
| `critical` | `#c9252d` | 흰 글자 5.6:1 |
| `critical-strong` | `#9c1620` | |

**의미 색의 원값을 한 단 어둡게 잡은 이유:** 배지 글자가 12px/700인데, 굵어도
18.66px 미만이면 WCAG는 **4.5:1**을 요구한다. Meta의 원래 초록(#31a24c)·빨강(#fa383e)은
흰 글자를 얹으면 3.3:1 / 3.7:1이라 읽기 기준에 못 미친다. 같은 색상(hue)을 유지한 채
명도만 내렸다.

`oculus-purple`·`meta-link`는 쓰지 않는다 — VR 제품 강조색과 레거시 내비게이션 색이라
이 서비스에 해당 자리가 없다.

## 2. 글꼴 — Optimistic VF 대신 Pretendard

Optimistic VF는 Meta 전용 서체라 쓸 수 없고, 문서가 적은 대체(Montserrat, Helvetica,
Arial, Noto Sans) 중 앞의 셋에는 **한글 글리프가 없다.** 화면이 거의 전부 한글인
이 서비스에서 그 셋을 앞에 두면 숫자·영문만 바뀌고 본문은 기기마다 갈라진다.

**Pretendard**(SIL OFL, 자체 호스팅)로 간다 — 문서가 Optimistic VF의 성격으로 적은
"기하학적이되 약간 휴머니스트"에 가장 가까우면서 한글을 덮는 가변 서체다. 가중치
300 / 400 / 500 / 700을 모두 쓸 수 있어 문서의 3단 리듬(300 편집용 부제 → 500 디스플레이
→ 400 본문)이 그대로 성립한다.

`ss01` / `ss02` 스타일 세트는 **재현하지 않는다.** Optimistic VF 고유의 대체 글리프라
다른 서체에는 같은 자리에 다른 것이 들어 있거나 아예 없다. 켜면 의도와 다른 모양이 나온다.

자간은 본문의 `-0.14px` ~ `-0.16px`를 **em으로 환산해 그대로** 쓴다(-0.01em).
다만 제목(`hero-display` 등)은 문서가 0으로 두므로 0을 지킨다 — 한글은 제목에서
자간을 좁히면 뭉치므로 이 규정이 마침 한글에 맞다.

## 3. 사진 자리 — 이 서비스에는 사진이 없다

이 언어의 핵심은 **"제품 사진이 곧 표면 처리"**(Key Characteristics 6번)다. 그런데
이 서비스에는 사진이 한 장도 없고, 앞으로도 없다 — 다루는 것이 텍스트 공시다.

장식용 이미지를 지어내지 않는다. 대신 **이 서비스에서 가장 특징적인 물건 — 공시
원문과 그 요약 — 을 사진 자리에 놓는다.** `card-feature-photo`(테두리·그림자 없는
32px 둥근 전면 타일)의 자리에 사진 대신 **실제 화면에 나오는 공시 카드의 모양**을
그린다. 문서의 "카드 크롬 없이 내용 자체가 표면"이라는 성질은 그대로 지켜진다.

`product-gallery-pdp` · `color-sku-picker-row` · `card-checkout-summary` ·
`color-swatch-circle` · `product-thumbnail` · `testimonial-customer-card` ·
`faq-accordion` · `tech-specs-table`는 **쓰지 않는다.** 상품 상세·장바구니·후기에
해당하는 화면이 이 서비스에 없다.

## 4. 코발트는 등장하지 않는다 — 1차 버튼은 검정

문서는 코발트를 **구매 흐름 전용**으로 못 박고("Don't use cobalt for marketing-surface
primary buttons"), 마케팅 면의 1차 버튼은 **검정 알약**이라고 규정한다.

이 서비스에는 구매 흐름이 없다. 그러므로 **코발트 채움 버튼은 어느 화면에도 없다.**

- 1차 행동(AI 요약 요청 · DART 원문 보기 · 기업 추가) → `button-primary` 검정 알약
- 2차 행동 → `button-secondary` 2px 먹 테두리 고스트
- 본문·목록 링크 → `primary-deep` `#0143b5` (문서가 "active link color"로 규정)

검정은 **버튼·고른 알약·작은 띠**에만 쓴다. 배경 전면을 덮는 검정 면은 두지 않는다.

## 5. 배지 — 네 개로는 모자란다

문서의 배지는 넷(promo-yellow · attention · success · critical)인데, 이 서비스는
**중요도 3단계와 검수 상태**까지 여섯을 구분해야 한다(PLAN.md 9.5). Iteration Guide
4번이 허용하는 대로 변형을 추가해 아래로 고정한다.

| 뜻 | 배지 | 모양 |
|---|---|---|
| 중요도 높음 | `badge-critical` | `critical` 면 + 흰 글자 |
| 중요도 보통 | `badge-neutral-dark` *(추가)* | `ink-deep` 면 + 흰 글자 |
| 중요도 낮음 | `badge-neutral` *(추가)* | `surface-soft` 면 + `steel` 글자 |
| 수치 확인 필요 | `badge-attention` | `attention` 면 + 흰 글자 |
| 사람이 검토·수정함 | `badge-success` | `success` 면 + 흰 글자 |
| AI 생성 · 미검수 | `badge-outline` *(추가)* | 면 없음 + `hairline` 테두리 + `steel` 글자 |
| 요약 대기 / 정리 중 | `badge-neutral` | 위와 같음 |

**미검수만 면이 없는 이유:** 사람이 안 봤다는 것은 *없음*의 신호라, 면을 칠하면
상태가 있는 것처럼 읽힌다.

## 6. 그 밖

- **hover를 새로 만들지 않는다.** 문서가 "no-hover policy"로 기본·눌림만 규정하므로,
  마우스 hover에는 **문서가 적은 눌림(pressed) 값을 그대로** 쓴다. 없는 상태를
  지어내지 않으면서 마우스 사용자도 반응을 받는다.
- **`npx @google/design.md lint`는 돌리지 않는다**(Iteration Guide 3번). 이 저장소는
  빌드 체인과 node 의존성을 두지 않는다(PLAN.md 9.5) — 재배포가 1GB 서버에서
  `docker compose up -d --build`로 돌기 때문이다. 대비비는 위 1번처럼 직접 계산한다.
- 한글은 어절 중간에서 잘리므로 제목에 `word-break: keep-all`을 준다.
- 수치는 Pretendard의 고정폭 숫자(`tabular-nums`)로 세로줄을 맞춘다. 접수번호처럼
  **값이 아니라 식별자**인 것에만 시스템 모노를 쓴다.

## 7. Facebook Blue 의 역할을 하나 넓혔다

위 문서는 `{colors.fb-blue}`를 **폼 컨트롤 활성 색**으로만 적는다. 여기서는 소개
화면의 히어로 제목("어려운 공시를, 읽을 수 있는 말로")에도 쓴다.

근거와 한계:

- 새 색을 만든 것이 **아니다.** 문서의 팔레트 안에 이미 있는 값이고, "코발트와
  오큘러스 퍼플 외에 강조색을 더하지 말라"는 금지에 걸리지 않는다.
- 흰 바탕에서 **4.82:1**이다. 본문 크기에도 AA를 넘기고, 제목은 64px이라 큰 글자
  기준 AAA다.
- **문장 전체를 한 덩어리로** 칠한다. 한 단어만 색을 달리해 강조하는 처리는 하지
  않는다 — 그건 어느 페이지에나 붙는 기본값이고, 제목을 끊어 읽게 만든다.
- 이 색을 **다른 화면의 제목으로 번지게 하지 않는다.** 소개 화면의 히어로 하나뿐이다.
  공시를 읽는 화면의 제목은 `ink-deep` 먹 그대로다 — 거기서 제목이 파랗게 되면
  본문 링크(`primary-deep` #0143b5)와 같은 계열이라 "누를 수 있는 것"으로 읽힌다.
