# Console redesign — design tokens (extracted from the approved mockup)

Scope: re-skin the 16 existing admin-ui pages to this visual system. No backend work,
no new pages, no new features. Pure token/CSS/component-styling pass.

## Palette (light theme — the mockup has no dark mode; if globals.css keeps one, use these
same hues shifted only in lightness, not hue, and never remove the dark-mode block wholesale)

```
--bg:        #FAF7F0   (page background)
--surf:      #FFFFFF   (cards, table rows on hover use a tint of this)
--sidebar:   #17201E   (dark sidebar, NOT the same as page bg — this app has a dark sidebar
                         on a light canvas, unlike the current all-dark theme)
--border:    #E4DFD4
--border-2:  #EDE9E0   (lighter hairline, e.g. table row dividers)

--ink:       #17201E   (primary text)
--text-2:    #5C645F   (secondary text)
--text-3:    #636C67   (tertiary / meta text, timestamps)

--teal:      #0B5F5A   (primary brand/accent — replaces --cyan)
--teal-hover:#08423E
--teal-dim:  #E6EFED   (teal tint background, e.g. active pill)
--teal-dim-border: #CFE3DF

--amber:     #C8A96A / warning bg #FDF9F0 / warning text #7A5B08 / warning border #EFE3C9
--red:       #B3341F  / danger bg  #FDF3F0 / danger border #EFD3C9
--green:     #1B7A43  / success bg #F0F6F1 / success border #CFE3D4

--sidebar-text:       #E9E5DC
--sidebar-text-dim:   #7E8A85
--sidebar-active-bg:  #0B5F5A (teal, not a neutral highlight)
```

Fonts: `Geist` (body/UI) and `Geist Mono` (numbers, ids, code, timestamps) — both via
Google Fonts, already an allowed CDN. Fall back to the existing `--font`/`--mono` stacks.

Radii: `--r: 12px` (cards), `--rs: 8-9px` (buttons/inputs/pills), sidebar width unchanged
unless it visibly clips at 224px against the mockup's proportions (check both).

## Component patterns to adopt (see mockup for exact markup/spacing)

- **Stat tile row**: 4-up grid, white card, label (11.5px, --text-2) over a large tabular-nums
  value (24-27px, 600 weight, -0.025em tracking) with a one-line footnote (10.5px, --text-3).
- **Section header**: h1 25px/600/-0.025em + one-line 13px/--text-2 subtitle, primary action
  button (teal, white text) right-aligned, secondary buttons (white bg, --border, --ink text).
- **Table**: header row is 10.5px/600/0.07em-tracked/uppercase/--text-2 on a very light tint
  (#F7F4ED), 11px vertical padding; body rows 13-14px padding, hover tints to #FCFAF6, hairline
  border-bottom #F4F1EA between rows (not the darker --border).
- **Pill/status badge**: colored bg + matching-hue text + 1px border in a slightly darker tint
  of the same hue, 6px radius, 2-8px padding, 11px/500. Never a bare colored dot alone for a
  primary status — pair a dot with a labeled pill where the mockup does.
- **Tabs**: underline style (2px bottom border in teal when active, transparent otherwise),
  not filled pill-tabs, for in-page section tabs (e.g. agent detail tabs, campaign filter tabs
  use a different filled-pill style — check which the mockup uses per screen, they differ).
- **Sidebar nav**: dark (#17201E) sidebar on a light page — this is the biggest structural
  change from the current all-dark theme. Active item gets a solid teal pill background, not
  a border or underline. Group labels are 10px/600/0.1em-tracked/uppercase/muted.
- **Drawers/modals**: white surface, 14-16px radius, drop shadow `0 30px 80px rgba(23,32,30,.26)`
  (modals) or `0 18px 44px rgba(23,32,30,.14)` (dropdowns/lighter panels), slide-in from the
  right for detail drawers, center-fade for modals.

## Explicit non-goals for this pass

- Do NOT touch dark-mode media-query support if admin-ui currently has a user-toggleable
  dark theme with real usage — flag it as a decision if you find one, don't silently drop it.
- Do NOT add new backend fields, new pages, or new nav items beyond what already exists.
- Do NOT change any page's data-fetching logic, only its presentation.
- Do NOT restyle the login/invite/no-access pages unless they visibly clash after the
  sidebar/app-shell change (their layout has no sidebar today).

## Per-page checklist (16 files)

/dashboard, /calls, /campaigns, /ai-voice, /phone-numbers, /users, /tenants, /settings,
/knowledge-base (new from PR #28 — restyle it as part of this, it's currently unstyled-mockup),
/workflows, /workflows/[tenant]/[agent], /workflows/[tenant]/[agent]/settings,
AppShell.tsx (sidebar + topbar), globals.css (token source of truth),
CustomApisPanel.tsx, AgentCustomApisPanel.tsx, KnowledgeBasePanel.tsx, KnowledgeBaseTabs.tsx
(these four just landed in #28 — restyle them to match everything else in the same pass
rather than leaving them on the old dark theme while everything around them goes light).

Reference file (not to be copied verbatim — it is a mockup, not real data or a real API
contract): the "Voice AI Console.dc.html" artifact shared in chat. Match its visual language;
do not port its fake data, its client-only state machine, or its feature set (live calls,
billing, telephony, contacts/DNC, IVR builder, API connections-as-separate-page are NOT in
scope — see the AskUserQuestion answer: re-skin existing pages only).
