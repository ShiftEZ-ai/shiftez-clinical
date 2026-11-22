# Product experience direction

Mode: Operate. Redesign in place (vanilla TypeScript, no runtime dependencies).
Mockup names and numbers are
illustrative only; nothing from them is seeded into the application.

## Visual contract

- Cool neutral surfaces (#F8FAFC page, white cards, slate text), one teal accent.
  Light, dark and system themes from the same tokens (`app.css` `:root`).
- Colour carries meaning: each duty type has a hue (teal, sky, violet, amber, slate,
  coral, indigo, pink) shown as a soft tint plus a dot. A chosen colour stays; the others get the free hues one each
  in creation order (`setTypeColours`, the one assignment every view and key reads). Automatic colours never use
  coral or slate: red-like colour means a problem only. **Ranks have one tint each, used
  identically everywhere** (as tinted pills for everyone): person pills, rank
  chips, the Rules page and its pickers, timelines, popovers and every legend show a rank in the same hue, from the
  rank's own colour in the categories data (Configuration; a chosen colour stays). The others take the **rank palette**
  (`--rank1…12` tokens, `.duty-rankN`; olive, orchid, rose and sage at three strengths), one tint each in creation order,
  so a new or reordered rank never moves another's: tints of their own, clear of every hue that already means something
  (the clinic/duty-type hues, Group violet, Person sky, area hues, the status colours and the teal accent), so a rank
  chip never reads as a clinic, group or person (`tests/test_web_rank_hues.py` keeps each
  rank token ΔE00 ≥ 3 from those tints and ≥ 7 from their dots and text, chip text ≥ 4.5:1). It comes only from the
  one rank colour helper (`rankColour` in `dom.ts`), never a hashed or hard-coded hue, and the legend names it. Skills follow the same rule when they are coloured. Roles and
  audit-log record kinds (sign-ins, devices) are neutral (slate) chips. Area hues (People › Where is everyone): every
  area (clinic tag) and rotation type has its own hue, so Clinic A and Clinic B never share one. They draw from
  teal, amber, violet, sky, pink and three area-only tokens, magenta, green and brown (`--magenta*`, `--green*`,
  `--brown*` in each `:root` block; `.duty-magenta/green/brown`). They never use coral (a problem), indigo (academic
  time) or slate (leave). A set colour stays; the others take the free hues in name order (`areaHues` in
  `person_timeline.ts`). An area and a rotation with the same short name (AB) share their hue. Status colours (green, amber,
  red, blue) are always paired with text or an icon, never colour alone.
- No coloured side or edge bars on cards, rows or panels; show hue as a dot or swatch with a label (`tests/test_no_side_bars.py`).
- **Every mark has a legend entry; the legend is the shared component; always visible**. A mark is
  anything that carries meaning by hue, pattern, outline or glyph: duty, area, rank and tag hues, hatches and patterns,
  leave, wish and away marks, issue markers, team boxes, tags, zones, density. Every view draws each one through the one
  key, `legend()` in `dom.ts`: `key.mark(node, entry, swatch, words)` stamps the drawn element (`data-lg`) and adds its
  entry, so nothing can be drawn without its key and no view builds its own. The key lists only what the view draws
  (as drawn) and is always visible, one line below its view, never hidden (its line, "+N"
  and print are stated once, under the status footer). A sheet or panel that opens over a view never
  covers its key: it stops above the key or the key docks in its foot. The Rules views tag what they draw
  (`ruleMark`, `RULE_MARKS` in `rule_catalogue.ts`).
  Shared renderers take the view's key (`colourRule` `seen`/`seenChip`, `issueMarker({key})`, timeline `Bar.keys`).
  A mark another module adds to a view later (the rule map's exception forks on Week, People × Days, List, Coverage
  heads and chips, and the session panel) is drawn as an entry of that view's key: the view registers its root
  (`hostLegend`) and the decorator asks `legendAt(node)`; never a second key. A key follows what its view shows (`follow`):
  rows redrawn, paged, collapsed or hidden by a search in place leave the key with them.
  A meaning drawn in several views is one entry, from one helper (`markOpen`, `markRoleGap`, `markWeekend`,
  `markToday` in planner_week.ts): Week, Month, People × Days, the codes grid, Coverage and the phone agenda all use it.
  The CI guard `tests/legend_guard.py` (`assert_legend_matches`) renders each view with every kind of mark, then fails
  on a stamped mark with no entry, an entry with no mark, or a mark of the shared vocabulary (`MARKS`) drawn unstamped.
  `data-lg-self` is the only exception: an element whose own words are its whole meaning (the Rotation board's palette,
  and a #tag or rank chip that names its own tag or rank on a screen without a key). A band, swatch or mark that stands
  for a value it does not name is never self-explained: it goes through the key (`tests/test_web_ssot_guards.py` lists the
  allowed places). The session panel has its own key (its pills' duty hue and its team boxes); Year keys its today ring
  and the underline of a named date.
  A key **never scrolls sideways, clips or cuts an entry**: what its one line cannot hold goes behind "+N", which lists
  every entry (it replaces an earlier wrapping key, whose aim, never losing Open place or Weekend,
  "+N" keeps). An entry can be usable (`LegendUse`): hover or focus highlights its marks, a click acts, a short note
  (a total) follows its words (Fairness's people: their line highlights, their sessions open). One legend system only:
  no footer legends (`footerLegend` retired; the Rules and checks pages' keys sit beside their views). A chart of one
  point says nothing: one value is said in words and the chart (and its key entries) starts at two.
- Footer is one line; overflow collapses into a clickable +N.
- Never show what is obvious from context or already shown on the same screen (e.g. no date in the footer).
- One primary colour: every primary button is the teal accent (Add session and New rota too; no near-black primary).
  The action that starts a rota is named **New rota** everywhere.
- One status palette (`statusTone` in `dom.ts`): waiting for someone (submitted, proposed,
  offered…) amber; done or in force green; a problem red; running blue; a draft teal;
  ended grey. Every badge and state dot takes its tone from it. A badge's tone is one of exactly six words
  (`Tone`: ok, warn, err, info, draft, neutral; `badge()` accepts nothing else, a duty hue aside); a record's state goes
  through `statusBadge`, never a state name or a synonym as a class. `badge()` builds every badge (an icon, a tip, a
  button that opens something); nothing else writes `class:'badge …'` (`tests/test_web_ssot_guards.py`).
- Words: the Fix list's two levels have one map (`SEVERITY_WORDS` in `issue_marker.ts`: Must fix, Could improve) and a
  rule's strength one wording (`levelWords`: Mandatory, Advisory · high); "open places", "Fix list" and "session" are the
  only names for those things. `tests/test_web_copy.py` refuses the old synonyms (Blocking, Rule issue, Rule broken,
  Needs review, need staff, fix list, shift) in user-visible text. Counts, plurals and times read `plural` and `duration`.
- One status per record: a rota shows exactly one state badge (`rotaStatusPill`); a review the server
  holds out of date (`stale_inputs`) is **Needs resubmitting** (state `outdated`), never also "Waiting for approval".
  Everything else about it is facts in words on one line (`factLine` in `dom.ts`: "Independent approval · 0 of 1
  approvals · due Thu 5 Nov 18:10"); the "·" separators are drawn by CSS and clipped at a line start, so a wrap never
  ends a line with one. A due time that has passed reads **Overdue since …** in `--err-text` with the alert icon
  (8.7:1 light, 9.3:1 dark on `--surface`). Why a state holds is the "?" help beside the heading.
- Text colour: `--ink`, `--ink-2` and `--muted` carry text and status icons; `--faint` is decoration only (dividers,
  hover borders, never a `color:`), since it is under 3:1 on white. Every colour is a token in the `:root` blocks
  (`tests/test_web_css_tokens.py`). Placeholder text is `--muted` (one `::placeholder` rule; ≥ 5.3:1 light and ≥ 6:1
  dark on every field surface), never the browser's grey.
- **Disabled is tokens, never opacity** (WCAG 2.2 SC 1.4.3). Opacity blends the label and its reason into
  the surface (55 % gave 2.3:1 light, 2.95:1 dark). A disabled or `aria-disabled` control's text is `--muted`; a bordered
  control (button, icon button, field, option card) also takes the `--subtle` fill (fields `--sub`) and `--line` border;
  the cursor is not-allowed and the reason stays in words, so the state never rests on colour. One block at the
  end of `app.css` sets it, after every variant and hover; a busy button keeps its look. Lowest ratio (on `--sub`/hover):
  5.37:1 light, 6.07:1 dark. Raised surfaces (panel, menu, popover, dialog, sheet) are `--surface` in both themes, so `--muted` needs
  no raised variant there (5.89:1 light, 6.78:1 dark).
  This goes beyond the minimum on purpose: SC 1.4.3 exempts text in inactive components, but a disabled control shows its
  reason and that reason must be legible, so never "optimise" it back to opacity. Information that fades
  (undone, ended, cancelled, out-of-month, a read-only strength) is `--muted` too, with its words or line-through.
- **A cancelled or ended item drops its hue; the words name it**. `--muted` fails on the darker
  duty hues (3.13:1 on rank11 light, 2.78:1 on rank12 dark), so a closed, removed or leaving chip, bar or agenda item
  turns neutral: `--sub` fill, `--muted` text (5.37:1 light, 6.35:1 dark), line-through, and its words ("· closed",
  "Proposed removal", "leaving") in the text, legend or accessible description. An unchanged import chip is neutral the
  same way; its dot keeps the hue. Secondary text on a live hue (times, counts, card properties) is the hue's own
  `--duty-ink` at full strength (≥ 6.70:1 on every hue), set apart by size and weight, never by opacity. A Fairness heat
  cell's number is `--ink` on levels 1–4 (≥ 5.17:1, heat-4 dark) and `--accent-ink` on level 5 (5.16:1 light, 6.53:1 dark).
- **A spotlight dims by tokens; the dimmed words stay readable**. The rule highlight (`.rk-dim`)
  and a Fix-list cause (`.cause-focus`) step everything else back, but a dimmed row still names a person, duty or day,
  so it is read, not hidden. A dimmed place takes the neutral fill and `--muted` words (5.37:1 light, 6.35:1 dark on
  `--sub`; 5.89:1 and 6.78:1 on `--surface`), the cancelled-item recipe; only its non-text marks (icons, dots, outside buttons)
  fade to 38 %. It was opacity 28 % (1.52:1 light, 1.88:1 dark at the worst hue) and 38 % (1.82:1, 2.44:1). Nothing in
  it is transparent, so sticky heads stay opaque. Print shows the rota undimmed.
- **A state is in words, never only a title**. A `title` is not shown on keyboard focus or touch (SC 1.4.13),
  and it is not reliably read, so a state that changes what someone decides carries visible words. A
  staff-import chip that will be added shows "+". The key below the rows names both states ("Will be added",
  "Already current"), and each chip carries the same words as visually hidden text. Do not use `aria-description`
  for this: it is still an ARIA 1.3 draft, few screen readers support it and translation skips it (`aria-describedby` or hidden text instead).
- A selection checkbox that appears on hover (`.wk-check`, 35 % at rest) is at full strength under `hover:none`, as
  `.wk-more` is (SC 1.4.11).
- `--muted-inverse` (`color-mix(--bg 65 %, --ink)`) is muted text on the one inverse surface, the selection bar
  (`--ink` fill): its subtitle, its Esc key and disabled actions (7.7:1 light, 5.6:1 dark), never opacity. Page `kbd`
  tokens never sit on it. On touch or ≤ 720 px the Esc key is not shown (no Esc there); Clear keeps its name and icon.
- Danger is solid: an armed confirm button and an error toast are solid red
  (`--err-solid`) with white text in both themes (AA); an error toast also carries an
  alert icon. An armed button (`armed()` in `dom.ts`, the one same-button confirm) has no
  time limit (WCAG 2.2.1): it stays armed until the mouse pointer or focus leaves it, or
  Escape; it never shrinks. Use it only for actions that cannot be undone (publish, end,
  delete for good); a reversible action acts at once and offers Undo instead. One exception: **Archive** a rota takes a reason and the armed press, then offers Undo, because it changes what staff or
  reviewers see. Archive, Restore and Delete draft are one action per rota state, named only by the server's
  `allowed_actions` (`rota_lifecycle.ts`).
- Geist / Geist Mono type (system fallback), 13–14px dense UI, mono for times.
- Fine borders, 6–12px radii, shadows only on floating layers (menus, dialogs, cards on hover).
- Floating layers take their stacking from one scale in `app.css` `:root` (`--z-bottom-bar`, `--z-popover`), never a
  number of their own. Bottom bars share one stack: the selection bar sits at the bottom, and another bottom bar (the rule
  highlight) rises above it while it shows, never under it.
- Non-text contrast (WCAG 2.2 SC 1.4.11, 3:1): the boundary of a control you type in, pick from or switch
  (inputs, selects, pickers, search boxes, steppers, a switch's off track) uses `--control-line` (#838C9B light,
  #737980 dark); `--line` / `--line-2` stay for dividers and cards. Focus rings and on states use the accent
  (≥4.9:1); duty-hue dots are ≥3:1 on their tint (amber #B86E06 in light).
  `tests/test_browser_a11y.py` computes these ratios from the rendered page.
- Icons are outline SVG. Icon-only buttons always carry an accessible name and a tooltip. Icons are drawn at even
  sizes (12/14/16/18 px; an odd glyph lands on half pixels) and an icon-only control is a square with its glyph in
  the middle (`display:grid; place-items:center`, as `.icon-button`): a rule that shows such a control keeps its grid,
  never `inline-flex`. The rail's controls (logo, Search, pages, account) are one 36 px square each on one centre
  line, at the place they keep when the rail opens. `tests/test_browser_icon_alignment.py` measures every icon-only
  control (≤ 0.5 px off centre) on the main pages for four roles at 1440 and 390, the phone drawer included;
  `tests/test_web_loading.py` refuses odd icon sizes.
- Target size (WCAG 2.2 SC 2.5.8 AA): every pointer target is at least `--target-min` (24 × 24 CSS px,
  `app.css` › Target size). A glyph that must stay small (a remover ×, a drag corner, a resize grip) keeps its look; an
  invisible positioned `::before` or the control's own box is the hit area, because the target is the region that
  accepts the pointer, not the glyph, and overlapping areas of different targets do not count
  ([W3C Understanding 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)). Exceptions used:
  Spacing (a 24 px circle on the target touches no other target: the side-panel grip), Inline (links in sentences),
  User agent control (unstyled native controls), and a box or glyph inside a larger target of the same action.
  Shared roots: a table's column grip is 24 px centred on the boundary (12 px into each heading; inside its heading at
  the table's edge), each heading stacked above the next; a resized heading clips its own button, so its text ends in
  an ellipsis and never runs into the next heading. Left: the sheet's 9 px fill handle (drag shortcut; Ctrl D is its
  keyboard way, no pointer equivalent yet) and the native × of search fields.
- Touch targets (WCAG 2.2 SC 2.5.5): on a phone every interactive target is at least 44 × 44 CSS px, from one block
  in `app.css` (Touch targets) for the shared controls (buttons, icon buttons, collection toolbar tools, segmented
  options, menu rows, tabs, search fields, pickers, link buttons, the skip link). Pills and chips that act as buttons,
  issue markers, and the one-line footer keep their look and take a centred invisible 44 px `::after` as their target. A person's name in running
  text is exempt (SC 2.5.8 Inline). `tests/test_browser_icon_alignment.py` measures every visible target at 390.
  The phone footer row is 44 px (the target height); items keep their size (≤ 26 px, one line), and the bottom
  safe-area inset sits below that row, never inside it.
- Nothing overflows: long or unbreakable words wrap (`overflow-wrap` on `body`); a chip, a badge or a one-line label
  that must stay on one line ends in an ellipsis (`.chip-text`, `.truncate`) with its full wording in a tooltip; a strip
  that clips or scrolls draws its controls' focus rings inside (`outline-offset:-2px`). Layouts that depend on the page
  column's width (Schedule header, footer, Rules rows) use `@container work`, so a docked panel gets the phone layout
  too; the Week's toolbar follows its own column (`.planner-main`), and a narrow day column wraps its head's whole
  values ("28/30" on its own line) instead of cutting them. A segmented choice inside a popover wraps whole options.
  A pill is as wide as its cell allows; only a pill list caps each pill (220 px, 150 px on a phone).
  `tests/test_browser_overflow.py` checks every page, role, width (1440/1024/390/320) and theme, and the Week with
  the Fix list docked.

## Information architecture

Ten pages: Home, Schedule, Requests,
People, Programmes, Insights, Rules, Configuration, Access & approvals, System; the Inbox opens from the bell (its badge: the server's unread total, capped at 99+, the exact number in its
name and tooltip, the same number the Inbox says: `unreadCount`). One page
per subject; a page that merged several old ones uses tabs, each tab with the roles of the page it replaced, and the open
tab lives in the address (`#system/audit`). Old addresses redirect through one alias map (`apps/web/src/routes.ts`).
Home is everyone's landing page (approved mockups HomeCoord / HomeStaff; `home.ts`), never a wall of tiles: main column
and a 340px side column (one column on phones). **Needs you** is one ranked list (server priority order): a tone count pill,
a title, one muted context line and one action per row; zero rows hidden; "All clear ✓" when empty. Planners also get
**Rota** (last, this and the next two months: the published days teal where they fall and named as ranges, the draft
amber by sessions filled, today marked, Open / Start for that month) and **Today** (the shared table: a row per clinic, a
column per session time as the rota sets it, a duty still running from yesterday included with its day, each cell the
people on duty against its places ("90/90"), a dashed red "1 open" pill for an open place; the names
as `.rp` person pills behind **Show people**, and Open in Schedule for that day). The setup card shows only to someone who
can act on its next step and never repeats a Needs you row; a credentialer gets **Expiring in 30 days**, an approver who
is not a coordinator **Decided requests**. Needs you holds only what this person may act
on (never their own request, rule or rota to decide); their own waiting requests are a **My requests** side card. The side column holds **My next duties**
(staff: **Coming up**, after a hero card for the next duty with Offer swap and Add to calendar), **My requests** (own,
waiting), **Away today** (a dot per kind, "N people"; the reason only as Department settings allow) and **Notices**. Every card loads, fails and retries alone; the footer
says "Published to 31 Oct · November draft 86%" (planners). Staff Home: no screen status in the footer — the hero card already shows the next duty.
A person or a request opens in a side panel with tabs, not a dialog. The person hub (`person_profile.ts`) has the tabs the reader's role may read — Overview, Schedule, Workload, Requests, Rules, Quotas, History and Access — each reading its own permitted source; its sections are a heading with at most one action and compact dated rows (a badge only when a record is not simply current), and a section with nothing to say is left out. Its Overview is the **person card** (`person_card.ts`): today's duty as one quiet card (`--sub`, no border; its team box keeps the team style without a second border unless a problem changes it); every contact the reader may see as one grouped list like a native contact card (`--sub`, hairlines starting at the text, no borders or nested boxes): the type's label over the value in full, then right-aligned icon buttons, **Copy** on every row and the kind's action from the contract's one mapping `CONTACT_KIND_ACTIONS` (Call `tel:` for phone-like kinds, Email `mailto:` for email) where the server gives that dial target (a type may switch it off); tapping the value copies too, and both say "Copied." through the announced toast; 44px targets and the bottom safe-area inset on a phone; and **More** (a text button, More / Show less) with next duty, hours, fairness, can work, leave and rules, exactly as `GET /api/people/{id}/card` permits the reader; every person name (`personLink`) opens it in place, for every role. Labels, order, visibility and dialling come from the contact types and the card, never from code. Contact types (Configuration › People) are configuration rows: name, kind, who can see it and one-tap call. Side panels (`sidePanel` in `side_panel.ts`) are
docked beside the content, which reflows (never floating over it), carry no coloured edge stripe and resize from a grip
on their left edge (drag, double-click resets, ←/→ when focused; width kept per panel in this browser); narrower screens
stack them in the flow. Every side panel goes through `sidePanel` (the docked item and task panels, the Schedule's
session, help, Auto-schedule and rules panels, the Fix list, the year view's day summary; `tests/test_side_panel_kinds.py` lists
them), so the edge handle is one for every side panel: a
`role=separator` (vertical, aria-valuenow/min/max) with a small grip at rest and a thin accent line on hover, focus or
drag; ←/→ step 16px (Shift 64px), Home/End go to the limits, double-click resets to the panel's own default width;
a person's resize stays within 360px … 60% of the window, the default is each panel's own width, and the page beside
always keeps 360px; the width is kept per panel kind in this browser (`rota.panel.*`, forgotten at sign-out);
phones and sheets have no handle. Phones (≤720px): every side panel is a full-width sheet.
The Schedule's own panels (Auto-schedule, Rules in context, a session, help) rise from the status footer and may grow up
to the top bar, never under it (a session or help stops at 60% so the ringed session stays in sight); a list's item
panel covers the screen. Controls in panels and the wish calendar are 44px tall, a stepper's steps share one row, a date range is one tap row ("Sun 4 Oct → Sat 10 Oct · 7 days") that opens
the calendar, and
replacing a panel with unsaved changes asks Keep editing / Discard changes first. The header (`panelHead`) names exactly what is open ("Line 3 of 20"), with icon buttons ↑ ↓
(previous/next in its list) and × (Esc). It is always clear what opened a panel: the item it explains is outlined or
ringed in its list (`panelHighlight`; on the Schedule with an "Open in panel" label, Why › A person rings that person's
duties) and scrolled into view. A list's item panel — a person, a request, a rule, a tag — is dockPanel (side_panel.ts): the shell's last column beside the page (one at a time; another row, or ↑ ↓, replaces it after the unsaved-changes guard; leaving the page closes it, asking first when it holds unsaved changes), a labelled region (not modal) that takes focus on open and gives it back to its list on close; a phone or narrow screen shows it full width as a sheet. Task panels are the same docked panel: the session editor and session details on the Schedule, Workload targets for this rota, staffing options, the staff import review, bulk clinics/skills and the fairness drill-down (there is no modal side sheet). When a list is drawn again (a save, a live update) its "N of M", ↑ ↓ and outline follow the new list (`markOpen(…, locate)`), and ↑ ↓ reach rows behind other pages or "Show N more" (drawn first). One panel slot per page: a page's own panel (the Schedule's session, Auto-schedule and rules panels) and a docked panel never show side by side. A panel opened from inside another takes its place at its width with "‹ Back to <it>" in the header (Edit session ← session details ← the session panel), and Back returns focus to the control (or list row) that opened it; opened from anywhere else it replaces it; a page panel opened while a docked one is open replaces that. The unsaved-changes guard runs before anything is replaced or gone back from; × and Esc close the whole stack. Modal dialogs stay for small forms opened from a panel and for governed or irreversible confirmations.

### Phone scope

On a phone the product is for employees. "Phone" is the one breakpoint, `@media (max-width:720px)`
in `app.css` and `phoneQuery` in `dom.ts` (scripts never write their own query); tablets and desktops keep everything,
and a desktop window narrowed below it gets the same rule at once.

- **On a phone:** Home (the employee Home: next duties, my requests, and only the requests waiting for your decision under
  Needs you), the Schedule (your published duties; no drafts or planning), Requests (ask, view, withdraw; Approve /
  Decline with the schedule impact for coordinators and approvers) and the Inbox, plus the account menu. People with
  other roles get exactly these too, with or without a person link: every role gets
  Requests, the Schedule (read-only), the Inbox, approvals (Access & approvals › Review routes: Activate / Retire, each
  route a card with its steps stacked) and the staff contact directory (People); only operator editing stays on the
  desktop (planner editing, People & roles, Role history, self-approval, delegation, Rules, Configuration…). An
  unlinked account sees there what it would see on a desktop, never "Best on a desktop". One place decides: each page's
  `phone` flag in the route table (`app.ts` `paths`) and, for a tabbed page, each tab's `phone` in `PAGE_TABS`
  (`routes.ts`; the phone shows only those tabs), never a per-role check in a screen.
  Planners also get the **Fix list**: Home lists their draft's row with **Fix list**,
  which opens readable and actionable there (search, filter, the evidence behind Show evidence, each cause's action under its row). An action
  that needs the planner (a scoped auto-schedule, a session's cover) lands on the desktop notice as any planning link.
- **Phone-finishable coordinator work (mockups B1, B6):** on a published session a
  coordinator can Replace… and Mark absent from a phone (the session panel rises as a sheet). Home's Needs you lists
  only rows a phone can finish (`onPhone` in `home.ts`: requests, swaps and person facts to decide, a rota review,
  a reported absence → Replace, a draft's Fix list), then one quiet line naming the rest, "1 more item needs a
  desktop: Rule change", with no button into the "Best on a desktop" wall; the Rota card and New rota are
  desktop only. Staff get "Can't make it" (a quiet button on their next duty) on the phone too.
- **Your own duties:** a Schedule view that holds only the reader's duties (staff, or My duties on)
  says "You have no duties this week" when it is empty, never "Nothing published" (the rota may be published without
  them), with **Show everyone** where the reader may read the team's duties, else **Next duty: <day>** when there is
  one. Staff have no My duties control: their Schedule is always their own duties (`scheduleEmptyWords` in
  `schedule.ts`).
- **Approver review on a phone:** Home's **Rota to review** opens the
  approval review at once (`#schedule?approve=<rota>`, the one `phone` exception for approvers and programme leads):
  the combined rota in one section per scheduling group, each adjustment and acknowledged gap quoted with its author,
  Accept / Reject per item (a rejection needs its reason) and one primary, Approve rota or Return to coordinator.
  Private drafts, merging, group planning and Submit stay desktop work.
- **Everything else is for a desktop** (Schedule planning and the Fix list's All checks table, People, Programmes, Insights, Rules,
  Configuration, Access & approvals, System): it shows "Best on a desktop", one line naming the page ("Rules is built
  for a larger screen."; one builder, `desktopNotice(page)` in `dom.ts`, also the Bulk editor's and Sheet view's), **Back to Home**
  (primary) and a quiet **Continue anyway**, which opens the page as it is on a phone, remembered for that page for the
  browser session, under a slim dismissible "Not optimised for phones." line. Deep links land there and never break.
  The phone navigation and search list only the phone pages.
- **One switch, easy to lift:** each page's `phone` test in `paths` (`app.ts`) and `PHONE_DESKTOP_NOTICE`. Setting it to
  `false` restores every page on phones as before, with no other change. The operator screens' phone layouts are kept
  and stay tested: browser tests bypass the notice by default (`tests/test_browser.py` `Clocked`); the phone-scope tests
  (`tests/test_browser_phone_scope.py`) turn it on.

## Interaction contract

- **Disclosure**: no ▸/▶ triangle or other disclosure arrow glyph is ever shown. The base CSS hides every `<summary>` marker; a summary is a plain text control in the accent colour (24px target, 44px on a phone, visible focus, underline on hover, the native expanded state), worded as what it reveals ("How this was made", "Run details"); a toggle that changes its words is a button with `aria-expanded` (More / Show less). `tests/test_browser_disclosure.py` fails on any marker.
- **Fix list** (mockups A; `fix_list.ts`): one server object (`rota_summary.py`,
  `GET /api/workspaces/{id}/fix-list`) is every surface's count: Home, the footer, Rota tools, Submit, the approver's
  review and the auto-schedule result. Two levels only, **Must fix** (a mandatory rule is broken: Submit is blocked) and
  **Could improve** (advisory goals and fairness; never blocks). The count is of causes, ranked by the checks one fix
  clears (the row's pill, "Clears 53 checks"). A row is the Home Needs-you row: one sentence, one muted line, one action
  (the surface's top cause primary), Show evidence for the evidence, its exact rule lines and the cause's other fixes (Open leave,
  Extend a series, People for this duty: secondary, from its findings' fix hints). The Fix list page keeps the rota's bar
  (the rota switcher, a menu: another rota's Fix list, or **Open its schedule**; its stage; Auto-schedule and Submit for review…
  on a desktop) and its footer health. The footer
  says "⚠ 3 must fix · 5 could improve"; its popover is the one issue list of the causes (each cause once, its sessions
  as chips, ending **Open all in Fix list (N)**). The standalone score is hidden while anything must be fixed ("Score shows
  once nothing must be fixed"), then "✓ Ready · score 71.4"; score deltas between two versions always show. Submit with
  acknowledged gaps: only causes that are not safety kinds, one reason each, press twice; the approver accepts or
  rejects each item (a reject with its reason) and may add an optional note, never a fixed one (a self-approval still
  gives its reason). On these surfaces its words replace blocking, warning, issue, task and finding.
- **Fix list beside the Planner** (mockup direction A): on a desktop the Fix list is the Planner's docked side
  panel (`dockPanel` key `fix-list`), opened by the footer's health, Rota tools, Home, the run card and a must-fix marker's
  **Fix** (the same item). Must fix is open, Could improve collapsed (Show), grouped by what to do when the server
  says so. A row is one plain sentence (the server's), its line (`detail_line`; never checks counts or score numbers), a **Safety** tag on every safety cause, and where it is: one date chip per
  session ("Fri 11"; past 8, "+N more"; the last two wrap together). The cause in focus shows its fix in place: the
  session's day on one line, then the session panel's own candidates (top 3, "Show all N") and **Assign**. Candidates
  are asked for only when the person chooses the session (its chip, the row's Assign, a marker's Fix) or keeps fixing
  (the move on after their Assign); when the panel opens or an issue list's Open all opens it, the box offers **Show
  who can work it** instead (fill-options is costly). The schedule
  rings the session in focus solid and the cause's other sessions dashed. A safety cause offers no waiver anywhere.
  Phones keep the page with the same rows and Assign (the phone exception for this flow); `#schedule/issues` stays the
  full collection (saved views, Group, Sort, All checks).
  - **After an Assign:** the edit's own `fix_list_delta` updates the panel and the phone page in place, with no read.
    The session it fixed becomes ✓ who, the focus stays on a session that is still open or moves on, and the counts are
    the delta's ("Checking…" until the complete check lands, which is the next read). One source: the footer,
    its Rota health popover and the panel all read that delta, then the next full read; no surface keeps a count of its
    own. Its Undo (the toast's and the top bar's are one) is an edit with its own delta: the session is back in
    its cause and its ✓ chip goes; a ✓ chip never outlives a delta or read that lists its session again.
  - **Server fields:** a field is shown only where it replaces words or makes a choice right, never
    as an extra line.
    - `missing` is already the line (`detail_line`: "5 shifts · each has 1 of 2 <rank>"), so it is not drawn twice.
    - `where` leads the line only when the sentence does not already name it, and it names the chips for screen
      readers.
    - `eligible_people` 0: the server's best fix is **Who can work it**, never Assign.
    - Each of the top candidates shows the server's one short reason (fill-options `reason_short`) on one line, the
      whole of it in its tip; a name is shown whole, wrapping, never cut. The first who fits is the one primary
      Assign; who would add a warning folds behind **Would add a warning · N** (open when nobody fits). The people who
      cannot work the session stay in the session panel.
    - `could_score_gain` is not drawn: score numbers stay out of rows.
- **Rules and people for one rota**: the Planner's rules panel for the rota has two tabs, **Rules** and
  **Who's available** (Rota tools ⋯ › Check opens either). Rules: Needs attention › Changed for this rota › Checked; a
  safety kind carries a **Safety** pill and, when open, the plain sentence that it (and its lines' numbers) can only be made stricter, and offers no exception anywhere; every
  rota-only change asks for a justification, and the rule shows it with who and the review state. Who's available: one
  strip per person over the rota's dates (leave solid slate, academic or protected indigo hatch, rotation elsewhere
  outlined, not in the department cross-hatch), the result in words, built-in views (default Can work in this rota, by
  the server's eligibility; Available every day / Partly away / Away every day within it; Everyone in the department),
  search, a key.
  The panel is the one place a rota's rules change (one list, the server's rule map); every "rota rules" entry (a
  finding's Open rota rules, Score check, the auto-schedule card) opens it at the rule. **Workload targets for this
  rota** is its own dock: the check's targets, saved target adjustments with their reasons, Adjust workload target.
- **The selection panel**: on the
  Schedule, a day, row, group, person or any cell selects (Ctrl/⌘/Shift + click or Shift + Enter adds, Esc clears),
  shown with the accent ring; the rules panel follows it: one-line identity, then tabs Details (person card, a cell's
  sessions) or Summary, Rules (This selection | All rules; Change for this rota; Make it the department default…, a
  waiting proposal shown as Proposed), Issues (the one issue list) and a person's Duties. A single session or a person
  on one keeps the session panel (the staffing tool), with the same rule rows.
- **Rules explained**: every Rules tab has
  List | Explained. Explained shows each rule as its title (Safety marked) and up to three plain sentences (the server's
  `rules-explained`, else the rule's own sentence and its lines in words), under Must and Prefer; Who narrows to a person
  or rank; Show lines (a button, no arrow) opens the line table; Print prints the explanation alone. A person's Rules tab
  opens Explained for them, and their Details offers "What rules apply to …".
- Screens show the work, not the configuration. Secondary settings live behind icon
  buttons that open popovers (Notion-style); labels collapse to icons when space is tight.
- Sidebar auto-hides to an icon rail and expands on hover or focus; users can pin it (kept). Every
  page has its own icon (Configuration: settings; System: monitor).
  Account menu (bottom of sidebar) holds appearance, desktop alerts, shortcuts, password
  and sign-out — nothing that has a place elsewhere (the bell opens the inbox; the
  sidebar search and Ctrl K search; the Setup guide is in Configuration).
  Sign out is truthful: the page and everything kept for the account go at once ("Signing out…", the start-up
  look), other tabs follow; sign-in shows only once the server has ended the session. No answer in 20 s or a refusal
  says "Couldn’t sign you out. You may still be signed in on this device." with Try again, the data still hidden.
- Configuration (admin) holds every department setting behind one grouped left list with counts (the Setup guide first while setup is incomplete):
  Scheduling, Places & services, People, Department (tags, Holidays & dates off, department settings incl. sign-in
  sessions, Auto-schedule & scoring, Integrations, Sign-in, announcements). Sign-in (`sign_in_config.ts`) is the
  server-wide method: password only, institutional only or both; the provider fields show once institutional sign-in is
  on; the client secret is write-only ("Set"); the redirect URI is read-only with Copy; Remember me for (days), 1–90; each linked account shows its issuer and is confirmed for a new issuer
  before saving; saving takes a reason, the
  same-button confirm and the read revision (a conflict offers Reload); Undo one step. The break-glass line (a local
  administrator can always sign in with a password) shows only with institutional only. Programme set-up stays on Programmes & rotations
  (programme leads own it). The auto-schedule options are one component in five visible groups,
  used for the department defaults and for one run (Schedule › Auto-schedule).
- Score weights are relative: each part shows its share ("35%"); any total is fine and is not said. The warning line
  speaks only when the weights cannot be used, and says why.
- Score review (`score_review.ts`): Configuration › Auto-schedule & scoring ›
  Score review lists every soft rule line of a draft (the shared collection table) with its tier, weight and points
  lost (the number and its share, never a coloured bar), sorted by points lost; a tier or a Fair share weight changes
  through the governed rule proposal; Must lines last, not editable. The score's parts that lost points sit beside it;
  the plain explanation is its "?". Coordinators see this tab only. History with Restore sits beside Reset.
- Auto-schedule run card (mockup direction A): one card, no steps. Setup is one line (dates,
  clinics, people, keep existing) with Change, which opens those fields under it; anything that changes what happens
  (nothing to fill, what the basic scheduler does not weigh, a choice kept on this device) stays above them. Then the
  priorities as one segmented choice (the contract's presets, the chosen one's weights under it) and the two search
  numbers (Search time, Keep improving fairness). Running, the same card shows the stage and time used of the allowance,
  the best score so far, big, with its line, when the last better schedule came or that it is levelling off, and three
  numbers (open places, better schedules, the stop window); a queued run offers only Cancel, a searching one Stop and
  keep best (the primary: it keeps the best schedule for review), Add time (only when minutes can still be added) and
  Cancel (it discards: the same-button armed confirm), each with its "?". Times read as people read them, one format
  (`duration` in `dom.ts`: "2 min 4 s", "55 min"); while searching, time used and "up to … left" of the allowance.
  Every length of time is typed in the same words too (`durationField`: "15 min", "45 s"; a bare number is minutes),
  one input per time, its label, help and bounds from the contract (`GENERATION_TIME_FIELDS`), never a unit in the label.
  Who a run schedules is one short line everywhere (`conditionsSummary`: "Everyone", "9 ranks · 80 people"), the whole
  scope in its tip and the run's Review; a run still going is a Recent runs row with its stage and time, not Review. The result leads with one sentence, then four numbers (Must fix, Could improve, Open
  places, Score), then the one next step (the top must-fix cause, the rest counted with a link to the Fix list), then
  Discard and Apply. A rota check never takes the card: it is the footer's working item with its Cancel.
  Stop when (`stopFields` in `generation_options.ts`, the same component in Configuration › Timing) sits in
  Setup › Change: one line ("After 5 min, or 10 s without improvement") while every stop condition is the department's,
  the conditions under it (no improvement, within % of the best possible, a target score, Stop by — a time today, per
  run only — and If rules conflict: Stop and show me / Continue with fewest breaks, which warns that every break is a
  must-fix and blocks publishing); a changed condition stays open and is said on the setup line. Words and bounds are the
  contract's (`GENERATION_STOP_FIELDS`, `GENERATION_CONFLICT_CHOICES`); a run sends options only when one differs. Why a
  search stopped is said once, in the solver receipt above the result, in the contract's words (`GENERATION_STOP_REASONS`,
  from `search_stop.reason`). A
  run that stopped with a saved checkpoint (`resume_available`) shows "Best found before it stopped" with More search
  time and Continue (`job.continue`); a run continued past a conflict says "Continued with N breaks · each must be fixed
  before publishing". A rules conflict's clash card says where and when each cause is (clinics, dates) and its smallest
  fix as one button (an exception for one person, Allow for this rota — never a safety rule — or Leave as open place),
  then two choices: Stop and fix rules (Rules in this rota) or Continue with fewest breaks (a new run, same options).
- Show, don't hide: prefer tabs (`tabs()` in `dom.ts`), labelled facts and tables to `<details>`.
- No dead ends: a state that stops the work says why where it happens, with the way forward one
  click away. A choice with nothing to choose from yet says so under the field with its setup step (`missingSetup` in
  `setup_guide.ts`: the step's own button for someone who may set it up, "An admin sets these up" otherwise); an empty
  draft week names Add session (not the filters); a disabled Run says why before it is pressed (every disabled button
  does: `button()` refuses `disabled` without `why`, and `disabledWhy` shows it as words after the button and its
  description; a reason the control's place already says (first or last page, nothing changed, a run
  stopping) is its description for screen readers only, never shown twice); a list filtered to nothing
  offers Show all. An empty state is `empty()` (a page or section) or `emptyLine()` (a card, panel, popover or list):
  one short sentence and at most one next action, never a hand-written muted "No …" line.
- A read that failed is never an empty state: "No … yet" is only what an answered read says. A read the server
  refuses (403) says "Not available for your role." as information, with no Retry; any other failure is an error with
  Retry inside the message. One builder: `readFailed()` in `dom.ts` (`tests/test_web_ssot_guards.py` keeps it so).
- Ctrl/⌘ K opens the command menu from anywhere (each typed word matches a word start, in any order); `?` lists
  keyboard shortcuts.
- A failed refresh (server error or offline) never presents saved data as current: a page showing data keeps its header,
  toolbar and data under one banner, "Showing saved data from HH:MM. Couldn't refresh." with Retry inside it (app.ts
  `showStale`); a page that showed nothing says only "This page didn't load", Retry inside the message. In a tab still
  running an older bundle than the pulse's `build`, that failure reloads once onto the new one ("Updated to the latest
  version"), or, with anything unsaved open, offers "Reload to update" instead and keeps the update bar. An
  error message, an error notice or an uncaught error there (a bug or server fault) takes the same one step
  (`stale_bundle.ts` `updateAfter`, once per bundle).
- Feedback (`feedback.ts`): one dialog from the account menu, the footer, Ctrl Shift F, Ctrl K
  and "Report this problem" on an error toast or notice about an unexpected error (a 5xx, a lost connection, a bug), not on
  validation or other errors the person can fix. Writing on the left, the page picture on the right
  (one column below 900px); Mark a spot is red, Hide area is a solid slate block; "Details sent" lists exactly what
  goes. My feedback for everyone (phone too); System › Feedback for admins and auditors (desktop).
- Status footer, on every screen: the screen's own status on the left (Schedule with a
  draft: open places, blocking, overlap / other drafts and the score, each opening a list;
  Schedule published only: the published days in view for planners, your duties in view for
  staff; a page with one list: its row count; others add theirs with `footerStatus()`),
  general status on the right as icons when all is well, words in the tooltip and accessible name; a change of state
  (Saving…, Not saved, Offline) also shows its word beside the icon (a speech bubble: Send
  feedback, Ctrl Shift F, desktop only; ✓ saved, a spinner while
  saving, red when a save failed; a save whose answer was lost (no reply, a gateway error) is never "Not saved": it reads
  "Checking whether it saved…" while the same command is confirmed with its idempotency key, then Saved, Not saved (a
  real refusal) or "Couldn’t confirm" with Check the rota, never a blind retry; a message whose action saves
  (Undo, Redo, Try again) stays open with a spinner and the footer's own words until that save settles, its own timeout
  stopped; the outcome is then the next message; a background read (a refresh after a save, a live update)
  still waiting after 10 s shows a neutral "Updating…" with a small spinner at the footer's left until it lands, never
  a warning or a connection state, nor while another footer state already says it (a job's own progress such as
  "Checking the rota · m:ss", or a save's "Saving…") (supersedes "Server busy"; an opening page keeps its
  skeleton's Still loading · m:ss after 15 s; on failure “Couldn’t load <section>.” + Retry; on timeout “Didn’t load in 1:00.” + Retry); a connection dot, offline red with "Offline · retrying" shown in words beside it
  (colour is never the only signal); an amber flask "Test environment: test
  accounts only" / a shield for Production). The footer holds status only, never a legend (a
  screen's key is the shared `legend()` beside its view; `footerLegend` retired). A view's key (`legend()`, dom.ts) lists every
  colour, mark, code and state the view draws, even one a row head already names, and nothing it does not draw;
  it explains system symbols and encodings only, never people: no list of names or personal codes in any layout, print
  or export; a short or coded name shows the full name on hover and focus and opens the person or session on click or
  tap;
  Properties, Colour by and Layers in View settings choose both; the key has no settings button of its own (one View settings per collection).
  **A legend is always BELOW the content it explains and ONE line only, on every page; never above, never multi-line**.
  The one `legend()` component draws a single line under its grid, schedule or table. What does not fit goes behind "+N",
  which is the footer's +N overflow, not a second mechanism. "+N" opens the shared popover on hover, focus, click and
  tap. The popover lists every item, one per line, as symbol and meaning. "Always visible" means this one-line strip
  plus +N. Exports cannot hover, so a PNG or PDF prints the full list at the bottom of the page instead of +N
  (nothing hidden). Every
  status item shows inline while it fits; as the width shrinks the ones that no longer fit (the last first) go behind
  one "? +N" button, which lists them (same icons, words and actions); legend entries go first, then status items; the
  button shows only while something is hidden. Real widths (ResizeObserver), refitted on resize and live updates.
  A phone keeps the first item (the main count or the rota health) and the button. It is the only place for
  rota health. Its controls (rota health, score, overlap, proposal, Needs a check) act through one delegated handler
  (`dom.ts` `footerAct`): a footer redrawn between press and release still acts on the same control
  drawn again under the pointer, a control lent to "+N" keeps its action, and a popover that cannot be built says so
  instead of not opening. The window scrolls; a long main collection table uses one native viewport that reaches down to the
  status footer (`fitHeight` in `table_view.ts` publishes where it starts as `--dt-top`; at least 320px), so its
  header stays visible, both scrollbars remain reachable and no empty band is left below it on tall screens.
  Short tables keep their natural height; tables inside dialogs, docked panels and the Planner
  use their containing view's layout. Selection, expansion and wrapping retain the table's
  scroll position; changing its search, filters, sort or page starts the new results at the top.
  Dates and numeric values stay intact; deliberate column wrapping breaks between whole values,
  and scope pills remain inside their cells with the shared +N control. The footer sticks to the
  viewport bottom and publishes `--status-footer-height`, so bulk and selection bars,
  toasts and sticky form footers sit above it. Phones: one compact row, icons and numbers.
- Sticky blocks: a header, tab bar, stepper or toolbar and what it stacks on are one opaque
  block — no gap, nothing scrolled shows above, under or through it (the top bar is opaque too). Dialogs and docked
  panels keep their head fixed and scroll their body; a scroll container with top padding names it `--pad-top` and its
  sticky blocks rise over it (one rule in `app.css`); the Schedule's view scrolls under its toolbar so the day header and
  column heads stay in sight; its group heads stick under the day header at one height (the tallest), so one stuck
  over another never shows the other beneath it. `tests/test_browser_sticky.py` sweeps every screen for it, and the
  Schedule's Week, Month and People with each docked panel open (Fix list, rules, session).
- Loading, one helper (`loading.ts`; approved mockups L1 light / L1b dark): nothing shows for a wait under 300 ms; once
  shown a state stays at least 400 ms; content fades in (rows ~80 ms apart), never pops; waits are aria-busy and said once
  in one polite live region; reduced motion keeps every state without movement; colours from tokens only. Pages: a 2 px
  accent bar under the top bar (`topProgress`) only for a wait over 300 ms that the person started — opening a page or
  address, their Refresh or Retry, their own save — as a slow, soft sweep (2.4 s, no glow; still under reduced motion).
  Background work never shows it: live updates, polling, the automatic check, the whole-rota read;
  a background redraw keeps the page on screen and usable and swaps it quietly. A skeleton in the page's own layout
  (`skeleton`: table, cards, calendar, panel sections, lines, one line in a row or hover card) — never a blank page or a bare "Loading…"
  (work being done is `working()`'s one spinner line: "Checking the rota…"); a page's
  skeleton fills its content area down to the footer, its rows repeating to the height. A wait is
  only its skeleton: nothing is drawn over it or over the page — no spinner card, no "Still
  loading…" — and its words ("Loading Schedule…") are for screen readers only, said once. Past 15 s one quiet line in
  flow at the top of the skeleton (in a card, panel or dock, inside its body), Still loading · m:ss after 15 s; on
  failure “Couldn’t load <section>.” + Retry; on timeout “Didn’t load in 1:00.” + Retry; Still loading in
  muted text, its clock said once; a failure ending in error tone, a timeout ending in warning tone; it takes its room
  from the skeleton's rows, so nothing moves, and never takes focus. A read the caller can replace offers Retry on the Still loading line (idempotent reads only; a late answer is dropped). The 15 s is one constant (`loading.ts` `RETRY_AFTER`), never a literal at a
  call site. No loading state is positioned over content (`tests/test_web_loading.py`). Header and toolbar stay usable. Start-up: until the account is read, the deployment's emblem (72 px, the configured branding; `index.html` draws the
  generated default before any code runs) and the app's name sit in the middle of the screen on both axes, with one
  thin accent line under them (`skeleton('boot')`, the top bar's indeterminate movement). The screen fades in after
  300 ms (a quick start never shows it) and the emblem breathes slowly (2.8 s, scale 1–1.04); reduced motion keeps it
  all, standing still, the line a full bar. Nothing else shows while it waits; past 15 s one quiet line under the bar,
  Still loading · m:ss after 15 s; on failure “Couldn’t load <section>.” + Retry; on timeout “Didn’t load in 1:00.” + Retry, which moves nothing. The same 15 s applies before the app's code has run
  (`startup.ts`), so a module that never answers ends in the timeout with Retry (a reload); a module that fails, or a failed
  start, keeps the same centred column with "<short name> can’t be reached", the reason and its buttons. The frame
  comes first once signed in and each Home card loads, fails and retries on its own. A live update that changes the
  account's roles waits, like every live update, until no form is open or unsaved before the app starts afresh. A long check or job is one
  footer working item with elapsed time ("Checking the rota · 1:12") and Cancel, never an ETA; a failed one names its
  cause in words ("… stopped after 2:33. <person> added a wish for <date> while it ran.") with Check again, and a
  cancelled check keeps the last results marked "Not checked" (grey). Offline says "Offline · retrying" and "Not
  saved" in words; a send that cannot go is disabled with one line saying why. An idle form shows no red text before
  anything is chosen (Exports: the picker placeholder says what is needed, Download disabled with the reason for
  screen readers). A sign-in from the wrong address redirects to the configured canonical address, else says in plain
  words to sign in at the usual address, with one button to it. Docked panels load their
  sections as skeletons. Buttons doing work (`busy`, built into `button()`): disabled and aria-busy at once, then a
  spinner in place of the icon and "Applying…". Sign-in is always identifier first ("Username or
  email", Remember me, Continue; the server routes to the password step or the provider, an unknown name gets the
  password step). It offers the account last used on this device (account_memory.ts); an invitation or password
  reset finished here replaces it with the account just set up. The password step shows the name with "Not you? Change" and keeps the username in the form, hidden,
  for password managers; a delay (429 `retry_after`) is a calm
  countdown with the button disabled, keeping what was typed. Only the button changes (spinner, "Signing in…", fields
  locked); a refusal says why under the password, keeps the input and focuses the password; success has no splash —
  the app frame appears at once with the page as a skeleton while the sign-in card fades out (~150 ms). A page reload
  takes the same path. Work being done rather than read (rule checks) is one spinner line (`working`).
  Form dialogs' Save is the same busy button ("Saving…" with the spinner after 300 ms).
  Actions apply optimistically where the reply allows (`savingMark` / `savedMark` / `failedMark`): the change shows at
  once, what is still saving carries a small spinner and a dotted underline, ✓ once confirmed; a refusal puts the row
  back with "Couldn't save · Retry" (no Retry when the record changed meanwhile: the conflict toast asks to reload);
  one summary toast with Undo. All checks batches (the Fix list's per-check view), Rules acknowledgements, and Schedule assign / remove (the
  person shows joining or leaving the session at once). Every draft edit, forward or Undo, applies in place from its
  reply (the sessions changed or removed, the issues that came and went, the rota's health): the Schedule reads only
  the draft again — two requests per action, no page refresh, scroll and selection kept. Long jobs (auto-schedule)
  show a real percentage, the step list, time left and Cancel, never block, and say when a run finishes while you
  work elsewhere.
- Auto-schedule live view (approved mockup; `autoschedule_live.ts`, `autoschedule_motion.ts`): while a
  run is shown, People and Week follow the solver's real better schedules (never simulated progress). Hybrid flies the
  big moves (fills from the People view's **Open places** row, swaps, moves of 4+ rows, moves between sessions) within a
  flight cap and rolls the rest in place; FLIP batched to one read/write pass per frame; transform and opacity only
  (Web Animations API); reduced motion settles every change at once, nothing moves. No Pause buttons: the viewer's own **Animate auto-schedule** choice (account menu, a menuitemcheckbox kept with the display
  preferences) turns motion off like the department's Off, and the grid holds still while the solver goes on. The strip is the one live region for the
  step (the panel's stage line is one only on phones, where the strip is hidden); in a full footer the rota's health
  folds after the screen's other items. The footer's run strip is one item: step ("3/5
  Improving", Roll), 120×4 time bar (`--line` track, `--accent` fill, `--ok` at Done), "1:13 left" / "1:44 used"; one
  polite announcement per step. At Done a "Ready for review" card shows only what the footer does not (score gain,
  duties changed, better schedules) when the Auto-schedule panel is closed (an open panel shows its review); changed
  cells keep a quiet accent ring. Settings: Configuration › Auto-schedule & scoring › Animation (labels only, a
  preview that plays the values typed).
- A finished job's result leads with its outcome: one status box in plain words (icon + tone:
  ok / needs you / stopped), then at most three numbers, then short groups that open to compact tables, then one card
  per thing that needs you with one main reason and one fix (details behind "Why"); advisory notes are one line; one
  primary action in the footer (Apply N changes; Discard is a link). No run-on sentences of names, no statistics walls.
- Settings pages have one save control: while there are unsaved changes a save bar (`saveBar` in `dom.ts`: amber dot
  "Unsaved changes", Discard with same-button confirm, Save) is pinned above the status footer, so a long form saves
  from anywhere; Save opens the reason dialog. No second Save in the header. A save patches the session from the
  command's reply and draws the page again (nothing else is read). Each Configuration section has its own address
  (`#configuration/department`).
- A rule just proposed is brought into view (scrolled to, accent-tinted for a moment, focused).
- Rules list (`rule_view.ts`): a row is select · one mark (attention triangle › waiting clock › mandatory shield ›
  advisory dot) · a plain sentence over "where · name" · its numbers (≥ min, ≤ max, → target) · who · N people ·
  actions on hover. Topic heads carry a duty-hue square. A rule opens in its docked side panel (`.dock-panel.rule-sheet`):
  sections Who & when (sentence, names in bold), Numbers (range bar 0 … min … target … max / "no limit" and three
  cards, "—" when unset), lines, "In <draft>" (a bar per day, green met / red short, plus the short days in words), facts;
  actions in a sticky footer.
- Rule numbers, one style everywhere (approved mockups; `numbersEl` / `rangeParts` in `rule_view.ts`, `.rule-num`):
  plain readable text in the UI font, tabular numerals, weight 500 — no pill, no monospace. min – max → target reads
  "≥ 1", "≤ 5", "1 – 2", "2 – 5 → 4"; the strength is a small labelled badge after them ("Mandatory" accent, "Advisory ·
  high" outline; one badge when the numbers agree, one per number when they differ; in plain text " · Mandatory"), never
  a "!" to decode; a number inherited from a broader line is muted with "↰" and its tooltip names
  that line. The Rules list and database, a rule's lines table, the line panel title, Needs
  attention, Which rule applies?, the rule sheet's cards and range bar and the planner's rule list all use it.
- Rules as the rule catalogue (`rules_tree.ts`, `rule_catalogue.ts`): one
  database, families › rules (Show lines, switch, "N lines") › numbered lines. Who and where are pills by kind — everyone grey,
  each rank its own rank tint (`rankColour`), groups violet, people blue, clinics teal, #tags their own colour (`.rp`, `tagRef`) — first N then "+N".
  Tri-state boxes roll up to select-all; one selection bar. A rule's page opens a line in the docked item panel (`dockPanel`) and
  outlines the line it edits. Needs attention: issues grouped by kind, each with a ★ recommended fix and Apply.
- Filters (`conditionBuilder` in `conditions.ts`, the one builder; approved mockups K1f + K1g):
  Notion-style rows `[Where | And/Or menu] [icon field menu] [part menu] [operator menu] [value picker] … [⋯]`. The joiner column is a fixed
  76px (64px inside a group; 56px on a narrow condition box, so "And"/"Or" stay whole); "Where" is plain text. Every chip is bordered, 30px and sized to its content (native
  selects through the shared picker, `field-sizing:content`); the field chip carries its type icon (calendar, people,
  pin, layers, tag…); a property with several dates names its part in its own chip ("Dates · Start / End",
  `ConditionField.parent`). Only the value gives way (its dates first, then its name with "…"); rows wrap only when the
  builder is narrower than 440px. The ⋯ in a fixed right column opens a small menu inside the builder (`rowMenu`:
  Duplicate, Turn into group, Remove; a group's: Duplicate, Remove group), as does the **+ Add filter rule** menu (Add filter
  rule / Add filter group); **Delete filter** (same-button confirm) clears every rule. Labels "Filter on", "Part",
  "Operator", "Joiner" stay visually hidden for assistive technology.
  - **Operators** come from the contract per field type (`CONDITION_OPERATORS`), in words ("Is within" with the hint "a
    moving range"), then a divider and **Is empty / Is not empty** on every type; choosing either replaces the value
    with plain muted "no value needed" (never a button).
  - **One value picker** for dates, numbers and text (`openValue`, on the shared `openPicker`): search on top, a
    **Relative** group first (the viewer's values from GET /api/conditions/catalogue — hidden, not disabled, where they
    mean nothing), then the exact value (the shared date field and its inline calendar, or what is typed). Typing in a
    date's search asks POST /api/conditions/parse-date and lists only the matches, the understood one first and
    highlighted (`.picker-row.best`, Enter picks); a value with an N ("Next N days…") fills the search with an example.
    Footer: "Last / Next N always include today." Rule and audience conditions list only "Relative to the duty" and an
    exact date, the footer saying why. Choice fields keep `multiPick`, with the relative values (Me, My programme,
    Clinics I can work at) first; one stands alone in a row (`Choice.exclusive`).
  - **Relative values** show a clock, the name and today's dates muted ("This month 1–31 Oct"; the whole range and
    "moves with today" in the tooltip); a duty-relative one says "each duty's own day". Chips of relative choices are
    accent with a clock (`.rp.relative`) and carry the server's hint ("Clinics I can work at · 3 + sub-clinics",
    "People I approve · 12 people"). The browser never works out a relative value: every list, chip and saved-view
    menu asks POST /api/conditions/resolve-operands once per change (`resolveOperands`, cached per account and day)
    and filters with the server's dates and record ids; a list's own column offers the viewer's values when it gives its
    record ids (`Column.ids`, e.g. Requests' Person) and the contract names its field (`TABLE_CONDITION_DEFINITIONS`).
  - **Saved views** store each operand as chosen; the saved-view menu marks a view with relative values with a clock
    and today's range or "per viewer".
  - Phone: the picker is the bottom sheet (48px rows, Relative first, Clear / Done). Popovers and pickers fade/slide in
    120ms; reduced motion turns it off. Keys: ↑ ↓ Enter in lists and menus, Esc closes only what is open, Backspace in an
    empty search takes back the last value chosen.
  The first row reads "Where"; later
  rows choose And / Or, one joiner per level — the second row's choice sets the level (as in Notion); choosing the
  other one on a later row splits the level there into groups (each keeps its joiner), so a tree never reads
  ambiguously; groups nest one level (inside a group any row's choice sets the group). A group is a subtle bordered box
  whose header is only "Group" and its count (its ⋯ removes it); only the top level adds groups. Operators follow the field:
  person fields with several values each (rank, skill, programme, rotation, cohort, tag, can work at) offer is any of
  / has all of / is not; a person, clinic, place or duty type offers is (one value) or is any of / is not. "Is not" is
  an ordinary operator (no exception section). Values are the shared multiple-choice field (`multiPick`) opening the
  shared picker. Two or more values get a second row of labelled segmented controls with words: "Match: Any of · All of" where one person can hold several values, and "Count:
  Together · Each" (`countSwitch`), followed by the choice in plain words ("any one of them · each clinic counted
  separately"). Count shows only where it changes the result: over more than one
  value (a #tag counts as many), never on "is not", never on Who for per-person families (workload, patterns, gaps).
  Field, operator and joiner are compact tokens (no native select chrome, a small caret). Read-only roles read the filters as facts
  ("Where Rank is any of [R1] [R2] · each rank counted separately"), never disabled pickers. With no tags yet, the Tag
  field says "No tags yet · Create tags". At desktop every row, group rows included, is one line; on phones rows wrap
  as [field][part][operator] then [value], never clipped. "Not …" pills in tables are muted, never struck through
  (strikethrough means an old value in the approval diff). The lines table marks a who or where counted per value with one small Each icon
  (`eachMark`); nothing marks the default.
- The rule line panel (K1f; 560px by default, resizable): title "who at where · numbers" with a lone green ✓ ("No duplicate or overlap" on hover)
  once checked clean; the family pill with an (i) (the rule's description); "This line says" in a tinted box; Who and
  Where headings carry the reach as links ("WHO · 15 people", "WHERE · 9 clinics": accent, dotted underline), also in
  the sentence. A link opens the reach popover (K1g, `rule_line_reach.ts`): title and period ("this week" with a rota,
  else "today"), search, people grouped by the filter row that matched them (sentence-case headings on one line, cut
  with "…", the whole filter in the tooltip; counts per group, first 5 then "+ N more"), each with initials, name, rank and a load bar "n/limit" (amber at the limit, red over, accent below; the
  number is always written); a person opens Which rule applies? for them; the footer says "1 over · 1 at the limit"
  and "Show on Schedule →". The clinics list shows each clinic with the Each icon where counted per clinic. The data is
  only the server's reach details; roles without them see the plain count. Numbers are one row (Minimum | Maximum |
  Target, or the rule's own numbers), each field with its strength in words beneath it ("Mandatory" solid,
  "Advisory · medium" outline, dashed "↰ …" from the broader line; kept in place but hidden while the field is empty); it opens the shared menu (Mandatory, Advisory · low …
  critical, Inherit where allowed; the current one ticked, the level in its tooltip and name) and an untouched level is
  kept as it is; a new number takes the strength of the line it overrides (the broader line with the same where), else advisory (medium) unless the rule allows only Mandatory.
- Line relation (`.rc-relation`, mockups B3–B5): one row inside "This line says" while a line is edited, proposed or
  approved, from the server's resolution (the same one the checks use): ✓ "Overrides line N for <who> · <its numbers>"
  or, warning-toned (`.rc-says.is-warn`, icon + words), "Does not override line N: <why>. Both apply, …" with its one
  fix as a button ("Make it Mandatory"). "line N" opens that line and tints its table row. Effect (`.rc-effect` rows
  in `.rc-lp-list`): one row per open draft whose dates overlap the line's dates (never other drafts), name and a toned
  badge "Clears N checks" / "Adds N checks" / "No change", or a spinner "Checking · m:ss" (elapsed, never an ETA); its
  (?) says which drafts are checked. Reach counts over the line's own dates when it has them. Pickers of clinics,
  places and duty types hide inactive ones behind a quiet "N inactive … not shown · Show". "Relax for <person>…" (a
  Must-fix person row's ⋯, never for safety kinds) opens the line panel prefilled from the binding line; by default it
  is a rota-scoped adjustment with a required justification, applied to the draft at once and reviewed with the rota; "Change the department rule…" is the secondary, propose → approve → activate path. An approver
  decides a proposed line in the same panel: says + relation, the proposer's reason, Effect, then Decline… and one
  "Approve and activate" that waits while the effect is checking. + New line opens the new
  line in the panel (through the unsaved-changes guard) with no numbers yet, so nothing is typed into the line that was
  open; it is only a draft in the panel (never in the rule's check) until it has a number: Keep change says "Add a
  minimum, maximum or target" until then, and closing drops it without a question. "This line says" follows every edit before Keep change: effective numbers (inherited ones too), their
  strength, days, hours and dates. Settings a line sets for itself show in its panel ("Settings for this line", each
  with the rule's value, Edit… and "Use the rule's"); the rule's Settings say when they are only defaults. The reach
  popover names the rota and week it counts; a line whose numbers or settings changed shows a fresh preview, never the
  old limits. An excluded #tag reads "not #tag"; a who of groups or exclusions groups as "Combined filters". "None" shows only where a number is set or inherited.
- Collections share one toolbar (`collectionToolbar` in `table_view.ts`), always in this
  order: Saved view (a dropdown named after the view shown: views, New view, Update, Duplicate,
  Rename, Delete, Reset to default; a status strip that filters one list becomes built-in views here — each with its
  count, the shown one's count and any warning, e.g. "⚠ 2 waiting for you", on the button; real section navigation
  stays tabs; a built-in view may also be a server scope with no column, read by the server and never a Filter row:
  Requests opens anyone who decides requests on **Waiting for you** (`requests/all?for_me=1`, counted as their queue;
  empty, "Nothing waiting for you" with Show all requests; a link such as `#requests?type=swap` opens the whole list)) → period/navigation where present (period, dates, source) → Search → Filter →
  Group (named "Group: Clinic") → Sort → View settings (an icon with a tooltip: layout, and on the
  Schedule range, properties, Person label, colour, layers and density, each setting on its own page)
  → actions. On a phone (≤ 720 px) the same toolbar is: row 1 the saved view (its name truncates, never
  wraps) and the tools as named icons (Filter, Group, Sort, View settings; a tool in use shows its count or a dot);
  row 2 the period navigation where present (a compound control, never squeezed); then Search alone, full width.
  Same outline popovers; a control is accent (with a
  count) only when it differs from the collection's defaults — a default sort or grouping is
  neither highlighted nor announced as a change. A free-text search is never stored (a reload starts empty); when a
  search or filters other than the view's own hide rows, one strip above the rows says "Filtered: 2 of 91 shown" with
  **Show all** (the collection toolbar's own: each list reports what it drew with `collectionToolbar(…).shown`). Show
  all clears exactly the search and those filters. It stays when nothing is left, so it is the one way back; the empty
  state then only says what happened. Active filters live inside Filter, never in a chip row: the button
  reads "Filter: Unread, Leave" (cut with "…", full text in the tooltip) with a count, and its popover starts with
  "Active · 9 of 43 people", Clear all and × per filter. Lists, the Schedule and dashboards use it — never a
  second filter system on one screen.
- Search options (`search.ts`; labels from the generated `SEARCH_MODES`/`SEARCH_SCOPES`): the search box's own icon
  opens Match (Contains · Exact text), Search in (only where the list tells a record's own text from what it explicitly
  references) and Find a record (a person, programme, tag… picked by name; its id is searched, its name only shown). A
  narrowed option stays written beside the icon in the accent; a picked record is a token before the text with ×
  (search the text instead), and typing lets it go. Defaults send nothing, so a list's older plain search is unchanged.
- Filter, Sort and Properties are Notion's (`conditionFilter`, `sortList` in `table_view.ts`): Filter is the shared
  condition builder over the collection's own properties (+ Add filter rule on any property with its type's operators,
  And / Or, + Add filter group, ⋯ per row), never one dropdown per property; Sort is a list of keys (+ Add sort, each
  with its direction, ↑ ↓ to reorder, × to remove, Delete sort), one key where the server orders the pages; View
  settings › Shown properties shows, hides and moves each column. All are saved with the view (`filters` +
  `conditions`, `sort` + `then_sort`, `hidden`, `order`). On phones these popovers open as a bottom sheet.
- Timeline layout (`timeline.ts`; approved mockup):
  rows of dated bars behind the same toolbar (period ‹ › and Today, Search, Filter, Group, Sort, View settings, Saved
  views). Zoom sets what is shown: Week 13 weeks by week (the department's weekend days shaded), Month 6 months by week, Quarter 9 months by
  month, Year 12 months by month; Today has its own header row. Overlapping bars stack in lanes; a bar shows its name,
  its short name or only its mark as its width allows. Hues are duty-type hues only, never coral; red is only a conflict
  (dashed zone with an alert and words on the row's badge strip), "needs approval" amber with a clock, a waiting change a
  dashed amber ghost with a clock. Time away is a block in the same lane: leave a solid neutral block with a plane,
  academic and protected time an indigo block with a book (no hatch and no dashes: hatching means "not published yet"
  and dashes an open place). View settings hold Zoom, Layers (swatch, count, on/off; conflicts always shown), the name column's properties, row density and weekend shading, all saved with the view
  (`TableViewDefinition.timeline`). **Every timeline shows a visible key**: the
  shared `legend` (dom.ts, the Schedule's and the export's) below the timeline, filled from what is laid out
  (`Bar.keys`, `TimelineRow.keys`), so it lists only the marks this view and range draw, a swatch and a word each, and a
  bare short label (AB, CD, EF) as its code with its full name. One line with "+N" (see Every mark has a
  legend entry), on a phone too; no disclosure arrow; never hidden. People ›
  Timeline, Where is everyone, Programmes › Rotation board and the Planner's Who's available all show it. Hover cards only where a pointer hovers; a bar opens its docked panel
  (conflict card with one recommended fix ★, a waiting change with Approve/Decline, Change dates, Remove armed). Only
  rows near the viewport are drawn (150 people × a year). Phones: an agenda (a person's own: Now with progress, then by
  month; many people: away now, elsewhere now, starting in two weeks, with Show the timeline). Programmes › Rotation
  board is its first user; what blocks duties is in the page's help (?), not a banner.
  People › Timeline is the People list's Timeline layout (`timeline` in `dataTable`; `person_timeline.ts`): offered only
  when `GET /api/timeline` answers; the layers, their words and counts are the server's (`TimelineLayer.style_key` picks
  the drawing), rows, groups, Filter and Sort are the list's, and its shown properties are the name column's second line.
- **People › Where is everyone: one mark language**. One line per person; the server
  owns each stretch (outside the department › leave › academic › protected › away › worked in › home rotation).
  - Marks use the person pill's tokens (`--pill-h`, `--pill-r`, `--pill-fs`, `--pill-pad`), with **no dots and no
    icons**: each mark has its words, so the hue needs no dot here. **Worked in** (where the duties were, never a
    placement) is the area's tint. A **rotation** is its tint, framed in its hue. A **rotation elsewhere** is a
    neutral outline. **Leave** is neutral, **academic or protected** time is indigo, and **not in the department**
    (the server's `outside`: not started, left, a gap) is a faint cross-hatch with muted words.
  - **Diagonal hatching in a hue means only "not published yet"** (a draft or the usual plan), on worked-in stretches.
    A rotation or leave is a record and is never hatched.
  - **Joined**: the server merges stretches that read the same across rotas (`rotas` lists them). A stretch that
    continues the one before it with only published/draft changed joins it, with square edges and no seam, and the
    run says its words once. Different stretches meet at a 1 px surface seam, with no gaps.
  - **Short stretches** (too narrow for any word) fold into the stretch they touch, whose card lists them ("Also: …").
    View settings › Show › Short stretches (off in the built-in view) draws them thin, in Who's available's strip
    marks (`.ra-*`; one mapping, `awayOf`/`AWAY_KINDS` in `rota_availability.ts`, names them on both).
  - **The key is visible** (the shared legend below the timeline; see Timeline layout above), not only in View
    settings. Areas are listed first in the department's area order (name order), then the marks, then codes. View
    settings › Show keeps each switch's swatches; Not in the department is always shown.
  - **Area order**: a period's areas are chosen by share and always written in name order, so the same two areas
    read "AB · CD" every month (server, `project`); the key and the hues use the same order.
  - Rank headings span the row and are never cut. There are no initials discs (their hue said nothing).
  - Rows are one height whatever they hold. **Phone**: the same timeline, one person per row; the view's months swipe
    inside it (84 px a month) under the sticky names, opening near today. The page never scrolls sideways. A name is
    not a button on a phone.
  - Which areas a column names is a versioned department display setting: at least `area_share`% of its duties, at
    most `max_areas` (`WhereaboutsSettings`; defaults 25 and 2). It lives in Configuration › Department › Where is
    everyone, with Undo and history.
- Layouts of the shared collection view (View settings › Layout): Table, Cards, Board (by a grouping) and Calendar,
  offered when a list supplies `calendar` (rows → one calendar per group; groups keep heading, count, box and collapse).
  Recurring sessions is the first: a week grid per clinic (time slots × days, each place with its usual assignees or
  "Assign in the draft", gaps as a faint + that creates a session for that day and time, ended / upcoming / off marked
  in words), its default view grouped by clinic. Its rows are series types: the series that share clinic, duty type,
  times and dates read and edit as one row.
- Separate dates (excluded dates, closed days): one list (`dateList` in `date_picker.ts`): removable chips, Add dates
  (the shared calendar; click toggles a day, Shift+click adds a range), Copy dates and Paste dates (any common format,
  ranges "1 Nov 2026 – 5 Nov 2026"; Ctrl+V on the list adds). Never a textarea of ISO dates.
- Named department dates (Holidays & dates off, `date_sets.ts`): the server resolves every
  date (Hijri included); the client never converts calendars. A set's Dates open docked beside its list (a year, one
  row per occurrence, "Adjusted" badge, Adjust this year / Back to the computed dates under the entry). Day headers
  (Week, Month, Day, the phone agenda) show each named date as a quiet outlined chip with its words (`dateMarks`);
  Year underlines the day and names it in its accessible name.
- A rule the server enforces on a field (a one-year limit) is checked beside that field as it is typed (`inlineCheck`
  in `dom.ts`), never only after Save; a failed impact check is not followed by a second, generic error.
- Selection: one bar for every list (`selectionBar` in `selection_bar.ts`): inverse, floating over the bottom of its
  list, centred on it and never wider, one row — count badge, "selected" over a breakdown, icon + label actions (one
  primary in solid accent), ⋯ More for the rest, the destructive action last in red with its own icon (a bin to remove,
  power to switch off), "Esc ×". Checkboxes over others are tri-state (`check_tree.ts`: ticked, empty or mixed) and
  roll up line → group → select-all. Bulk changes follow "Bulk changes" below.
- Bulk changes (`bulk.ts`, `bulk_change.ts`, `bulk_children.ts`): one pattern in every
  list — select → Change… → Preview → Apply → Results, one Undo.
  - **Selection menu**: a small menu button beside the select-all box (`.dt-check-menu`): Select all N matching, Paste names or
    codes…, Select by condition…, Invert selection, Select none. x ticks the focused row; Ctrl A ticks the rows shown,
    again all matching. A list's selection survives redraws and a trip into its child records.
  - **Select-all banner** (`.dt-select-banner`, accent-soft, `role=status`): "All 50 shown are selected. Select all
    168 matching" / "All 168 matching are selected. Clear selection" — never "select all 0".
  - **Filter = the condition builder** (`conditions.ts`, Where / And-Or / the + Add filter rule menu / ⋯, ~780px) under the
    "Active · N of M · Clear all" head; column-header filters add rows to it; saved views store the tree. Dated lists
    have one **As of** row at the top (shared date field, × back to today); the Filter label names it only when it is
    not today ("Filter: Active · as of 1 Nov 2026").
  - **Show their…** (bar action): a menu of the registry's child collections with counts; opens a route with a
    breadcrumb (`.bulk-crumb`, "‹ People / Clinic permissions") and a parent chip in the toolbar's scope slot
    (`.dt-parent`, "For 42 selected people", a menu: back, remove one).
  - **Universal Change form** (docked panel, 620px, stepper Change → Preview → Results): rows on the condition-row
    pattern — What to change (fields + actions from `BULK_COLLECTIONS`) → How (Set to / Clear / Move by days / Add /
    Remove) → the field's shared input → ×; "+ Add another change"; "Everything else keeps its current value"; Reason
    (required for destructive changes). Change… is always the bar's primary; at most one or two shortcuts; ⋯; the
    destructive action last.
  - **Preview** (820px, Wider to full width): tiles (`.bulk-tile`: ready / no change needed / to resolve here /
    waiting for someone else; text + icon + tone, they filter) with a sum line that always adds up; the shared table
    grouped by Problem (Status badge, Record, What changes in mono `.ba` before → after, Problem, Fix); group header:
    best fix as a split button (`.bulk-split`) + Skip all; a row's Fix menu; selected rows take one fix; Fixed rows have Undo
    fix. Footer: "Apply ready rows (N) → Results"; destructive: the armed same-button confirm ("Confirm end 88?").
    Stale: "Things changed since the preview" + Preview again. Applying >1 s: one indeterminate bar
    (`.bulk-progress`), the panel may close; a toast says when done.
  - **Results**: count filters Changed (ok) · Skipped (neutral) · Refused (err) · Waiting for approval (warn) · No
    change (neutral); before → after; Resolve N (kept rows, `.bulk-staged-note`), Undo the whole change, Audit entry.
    A clean change skips Results: the toast (shared `notify`) carries Undo.
  - **Light path**: no problems, ≤ `bulk_direct_max` rows, not destructive, not governed → applied at once with the
    toast + Undo. In the list, changed rows carry a "Changed" badge (`.dt-changed`), the toolbar offers
    "Show changed · N" and the footer, beside the count, keeps "Undo last change" + History until the next change.
  - **Now** values under a change row (`.now-values`): the spread of that field among the selected rows. Preview and
    Results group by problem / status by default and by person, clinic or tag through the Group control. Child lists
    page on the server (200, "Show N more"); "Select all N matching" there counts on the server ("Counting…").
  - **Staged rows**: held-back rows are kept (14 days) and the list shows "N rows of an earlier change kept to resolve
    · Resolve"; Resolve reopens Preview with just them.
  - Phone: operator bulk is desktop-only; Requests keeps Approve / Decline, and bulk Decline is a bottom sheet
    (`.bulk-sheet`) with the required reason, the schedule impact and the armed confirm.
- Bulk editor and Sheet (`sheet.ts`, `sheet_paste.ts`): System › Bulk editor and
  View settings › Layout › Sheet on registry lists — the same collection (toolbar, Filter with As of, saved views)
  drawn as a grid, never a second engine.
  - **Record type** (`dt-tool` in the toolbar's scope slot): a searchable menu of the registry's types the person may
    read, grouped People & permissions / Rota / Rules / Configuration; the current type shows its count (no request
    per type).
  - **Grid** (`.sheet-grid`, `role=grid`, roving tabindex, `aria-selected` true/false on every cell): editable cells on
    `--surface`, related (read-only) columns on `--subtle` with muted text; the cursor is a 2px accent inset ring, a
    range is `--accent-soft`; Enter/F2 edit, Esc cancels, arrows move, Ctrl C/V copy and paste, Ctrl D fills down;
    the fill handle is a 9px accent square at the cursor's corner.
  - **Cell bar** (`.cellbar`): "<record> · <field>", the value in mono (cut with an ellipsis, its words in the tip) and
    the field's shared input while editing.
  - **Staged and changed cells**: staged = `--warn-soft` with a top-right corner mark and the "was" value struck
    through, the row's first cell a 3px warn edge; changed (after apply) = a top-left ok corner mark. Marks are shapes
    and words (sr-only), never colour alone.
  - **Stage bar** (`.stage-bar`, sticky at the bottom, warn tones): "N changes in M rows · not saved yet", Discard…
    (armed) and "Preview N changes", which opens the shared Preview (Back to sheet).
  - **Read-only**: roles without an edit for the type see the grid with no cell editing, no Paste from Excel and no
    stage bar. At the 5,000-row window a warning note says "Showing the first 5,000 of N … filter to narrow". Phones
    show the "Best on a desktop" notice.
- Shared tables: a column header opens its menu (click, right-click, Enter; `table_menu.ts`): Sort A → Z / Z → A,
  Filter by, Group by, Wrap this column, Hide column, Move left / right; View settings has Wrap all columns. Columns do
  not wrap by default: pills show the first few and "+N", which opens the numbered list (`pillList`). Group by lists the
  collection's own groupable fields, marks the default and offers "Then by". All of it is saved with the view. The Rules database
  (`rules_tree.ts`, a tree) offers the same menu but no Group by — rules group by Family from the toolbar and lines stay
  under their rule: Rule / who and Reach sort the rules (Rule / who also sorts each rule's lines by who), the other
  columns sort each rule's lines; Filter by keeps the lines with those values and opens their rules; Rule / who is
  never hidden or moved.
- Anything that opens is tied to what opened it (`tether()` in dom.ts): popovers, menus, pickers and hover cards open
  against their trigger with a small arrow pointing at it (flipping above or beside when there is no room); the
  trigger keeps an accent ring and tint while open (`.is-open`, `aria-expanded=true`), a hover card rings its element
  (`.is-pointed`), and a side panel keeps the item it explains highlighted (`panelHighlight`, `.is-explained`).
- Dropdowns: one picker behaviour — every list has a search box (a key typed on the
  closed field starts it); a list longer than 7 also leads with recent picks; keyboard per WAI-ARIA APG; popovers open
  on the side with room and scroll inside when taller, never past the status footer, their head and foot staying in view
  (Reset, Save as view) and the wheel or a swipe never scrolling the page behind them; a phone sheet the same. Menus are `role=menu` with `menuitemradio` for single choices.
  A closed dropdown never cuts its label silently: too long for its width, it ends in "…" with the full choice as a
  tooltip.
- Grids (`role=grid`: Schedule Week, Month, People, Timeline, Counts through `roving()` in `planner_week.ts`; the Sheet
  its own): one Tab stop, a cell (the last focused, else today's first session, else the first cell), per the WAI-ARIA
  APG grid pattern. A column's resize grip (APG window splitter, `role=separator`, ← → resize) is no grid item: Tab
  reaches it from its focused heading, and focus stays on it while it resizes. A session item's name says duty, place,
  date and time.
- Multiple choice, one field (`multiPick` in `dom.ts`): a boxed field as tall as any
  dropdown showing the chosen values as chips coloured by kind (`.rp`: people blue, a rank its rank tint, groups violet, clinics,
  places and duty types teal, a #tag its own hue; `Choice.tone`), as many as fit its measured width, then a "+N" chip
  whose tooltip lists the rest, then the caret; never wrapping, clipped or overflowing, refitted when its width changes.
  A first chip that cannot show about 12 characters gives way to a count chip ("2 clinics", the names in its
  tooltip): never a stub. Click, Enter, Space or ↓ opens the shared picker (Search, Chosen · N first, Select all N shown, Clear). A hidden
  `<select multiple>` holds the value; the button's accessible name and tooltip are the whole list. Lists of up to 7
  options in forms stay inline checkboxes (`multi`), which show every option.
  In dense grids (rule lines) choice columns grow to their widest choice, capped at 320px.
- Dates: one picker (`date_picker.ts`), never the browser's. A field you can type in (1 Oct 2026, 01/10/2026,
  2026-10-01) that shows "Thu 1 Oct 2026", with a calendar button (WAI-ARIA APG date picker dialog). A pair of first
  and last days is one range field ("Dates": both days, → between, the day count) whose calendar shows two months,
  presets on the left (Rest of this month, Next week, Next 2 weeks, Next month, Next 3 months, Custom) and "Use 1 – 31
  Oct". Weeks start on the department's week start, the department's weekend days (Department settings) are shaded, today is outlined, days outside min/max are
  struck through. Phone: one month in a full-width sheet. Values stay ISO for forms and the API.
  A date and time is the same date field with a 24-hour time beside it (`dateTimeInput`: type 0830 or 8:30, ↑/↓ step 5
  minutes, PageUp/PageDown an hour; a spinbutton); its value is local ISO yyyy-mm-ddTHH:MM. No browser date or time picker anywhere.
  People always see and pick the last day included ("Until (included)"); an end stored as the day after
  (`ends_on`, not included) is converted in one place, `lastDayIncluded()`.
- Tabs that redraw a page or load data (Schedule views, session editor) activate on
  Enter/Space (arrows move focus); cheap local radios follow the arrow keys.
- One Schedule page (`#schedule/<view>/<day>?edit=&mode=&compare=`) replaces Planner, Rota
  calendar, Team calendar and Published rota; old routes alias to it. One continuous timeline:
  published days around a draft are grey and read-only; the draft starts where the grey ends
  ("Draft starts" on its day in Month). Every day outside the draft's dates, published or not, in Week and Month is the
  one out-of-range grey (`--sub`, as a Month's days of another month) with no day numbers, keyed "Outside this rota";
  its published sessions are the grey read-only chips keyed "Published, read-only". A Month band's count of
  groups with nothing that week names its unit from the contract's dimensions ("3 services without sessions").
  Only one draft is editable at a time; nothing published is edited in place.
- One View picker: views (Year Month Week Day People Coverage List, keys Y M W D P C L),
  Layers (Published always on; one draft or No draft; Show drafts: Edit one / Side by side /
  Overlay; Leave and wishes; My duties), Colour by, Names, Density. One primary in the
  toolbar: Add session with a draft, New draft without. Rota actions live in the top bar:
  Auto-schedule, Submit for review… (a draft) or Publish… (an approved rota), Rota tools ⋯. A button names the step that
  happens. The sidebar logo is the link Home.
- Dots (Year, Month): one per day. Fill = state (solid published, hollow draft, half = two
  rotas on the day); colour = health (green no issues, amber open places, red rule broken,
  grey not checked); no dot = not planned. Colour by never recolours dots. Every dot has an
  accessible name; a month is a group of named day buttons with one tab stop.
- Week groups sessions into collapsible lanes (Group: service, duty type, location or duty time — a session's own
  start–end, never a named part of the day; Then by nests one more level); day headers show sessions and
  open places; hovering a session shows its people and rule checks. Day: lanes × hours,
  published bars solid, draft bars dashed; Overlay badges Changed / New.
- Overlap choice (`overlapChoice` in `schedule.ts`), one component in New draft (under the
  dates, which stay editable), the footer overlap pill and Publish…: a radio group — Start
  after it (default) / Replace from a date (date + mini timeline) / Change the published rota
  — with unavailable options shown disabled with the server's reason; the primary button
  repeats the choice ("Create draft from 2 Nov").
- Submit for review… / Publish… is the one way to staff (one dialog, named after the step it takes): a summary (period, sessions, people notified, rule
  check, open places), then what changed since you started (overlaps, other drafts with
  Compare and an unticked "Discard … after publishing"), then Submit for review or Publish
  (press twice). Staff see the same page, published only, their own duties.
- **People, teams and issues**. Four rules:
  - **One person pill** (`personPill` in `person_pill.ts`) for every person on every surface: Week, Month, Day bar,
    List, session panel, agenda, Home, My duties, the person card and published views. It is sized by the pill tokens
    (`--pill-h` 24 px, `--pill-h-compact` 20 px in Month and Week › Density compact, `--pill-gap` 4 px,
    `--pill-pad` 4 px, `--pill-r` 5 px, `--pill-fs` 11.5 px, `--team-indent` 8 px; `app.css` `:root`), and no
    surface sets its own size. It carries the duty's soft tint, the view's Person label (`personLabel`) and the
    person's own issue marker at its end. The pill is the clean name: a rank shows only where the
    view's or the department's Person label adds it (its Rank part), never as an automatic prefix; a team's
    accessible name still says each trainee's rank. On paper a name is never cut: it wraps only between
    whole words, on as many lines as it needs, with no …, and a pill is never narrower than its longest word. A
    supervisor is weight 600, everyone else 400, with no SUP/TR badge. On screen, one line per person: the name ends in …, and the
    full name is in its tip and accessible name. A Week chip is the pill dressed on the chip's own button (menu, drag,
    hover card).
  - **One team** (`teamView` in `team_view.ts`, one renderer). Pairs use one helper (`pairsOf`/`pairWords` in
    `dom.ts`) and one team order (`teamOrder`/`teamText` in `planner_teams.ts`). The supervisor's pill comes first, then the trainees'
    pills one indent in, then a quiet "+N more" pill after the view's "Trainees per team" (`team_rows`). View ›
    Properties › Supervision teams is **Tree | Box** (the contract's forms; the first, Tree, is the built-in default,
    so a department or person sets theirs through their default view, with no migration):
    - Tree joins the trainees with one straight neutral 1 px guide (`--control-line`, 3:1; not a coloured side bar).
    - Box draws a hairline outline.
    - Side by side (Day bar, List, My duties) the same pills sit on one line.
    - Only recorded pairs form a team, never inferred from rank or team lead. Staff see their own pairs only. View ›
      Supervision teams off draws the people without teams; one-line contexts say "Dr A (supervising B, C)".
  - **One issue marker** (`issueMarker` in `issue_marker.ts`; see Issue marker below).
  - **One colour**: the session's hue is the pills' soft tint plus a dot beside the session's label.
- **Issue marker**. Every entity with an issue shows the same marker: a person (inside their pill), a team (in
  the supervisor's slot, or heading the guide when no supervisor is on the session), a session, a clinic row header and
  a day header.
  - **Shape and colour**: must fix (the finding's `error`) is a red triangle; could improve (`warning`) is an amber
    circle (`notice`). Shape as well as colour, so colour is never the only signal (WCAG 1.4.1). Several issues on one
    entity: one icon with a count. Icons are ≥3:1 on every tint; the count is text in the tone's ink.
  - **Words**: only the contract's (`TEAM_PROBLEM_WORDS`, `FINDING_MESSAGES`, else the server's message). They are the
    marker's accessible name ("Dr A. Must fix: …; Could improve: …"), its hover/focus card and its popover. A click or
    tap pins the popover (the shared `popover`); Esc or a tap outside closes it.
  - **One issue list** (`issueList` in `issue_marker.ts`, guarded by
    `tests/test_issue_list_ssot.py`). Every list of issues uses it:
    - the marker's popover and hover card (day, row, cell, team, person);
    - the session panel's Needs attention, the session form's check and the published-changes check;
    - the footer's health lists, a run's issues and prechecks, the auto-schedule review and a save's new issues.

    An item with card fields (its session, detail or rule lines, the deciding reason, Why, its own actions, a leave pill,
    a proposal's "since") is drawn as a card: the same marker shape, never a colour-only dot. There is one `Issue` type and
    one `findingIssue`.
  - **One session-chip model** (`sessionChips` in `issue_marker.ts`): the Fix list's cause rows and the issue list's
    repeated sentences use the same chips, ✓ with who once fixed, "+N more", and the last two wrap together. No colour
    mark: a chip says its date or place in words; two or more sessions on one day carry their time ("Sun 11 · 08:00",
    `dayLabels`). An issue's identity is its code and words, never its severity: one finding that is
    both must fix and could improve is one item with the worse mark. The footer's Rota health, the published
    rota's could-improve list (no table) and the auto-schedule results' warnings and "Resolve these first" are this
    list too.
  - **One Needs-you row**: its words and count pill come from `needText` and `needCount` in `dom.ts`, on Home, the
    Fix list, drafts, groups, the run card's changes and published changes; its container is `needRow` (count or mark,
    words, then action, in that order by construction). A cause's evidence opens with a worded toggle, **Show
    evidence** / **Hide evidence** (no arrow), its state in `aria-expanded`.
  - **One rota health**: Must fix and Could improve in the footer are `fixPills` for a draft and the published rota
    alike. Could improve is the amber circle on both. Sessions short of people count as Must fix, never as a separate
    "need staff" pill. Absences on duty, extra cover and the score stay beside them.
    - **One item per sentence**: the same words appear once, with their sessions as chips (the Fix list's cause and
      chips model).
    - **Items go to their session**: an item on one session, and each chip, is a button. It shows its session, moving
      the schedule to that week or day when needed (as a `?shift=` link does), focused and highlighted (`markShift`).
    - **Open all**: the list ends with **Open all in Fix list (N)** where the reader has a Fix list (a coordinator on a
      desktop draft). It opens the Fix list panel at exactly those causes ("Showing k of M · Show all").
    - **Other actions**: a must fix's old **Fix** button gives way to Open all; **Why** stays for could improve. Issues
      that are no rota finding (a team's) keep **Fix** to the session.
  - **Roll-ups**: a clinic row and a day header roll up their sessions' issues, each with where it is. A popover lists
    at most `ISSUE_LIST_LIMIT` (contract, 5) items, then "M more"; without a Fix list here, "Show all M in Fix list".
  - **A session's own marker** sits beside its "1 open" place when the issue is that open place, and at the cell's top
    right otherwise. Month shows it through its row and day roll-ups.
  - **Dense views draw the marker only**: no dashed problem outline on a pill or team, and no problem words in a Week
    or Month cell, a Day bar or an agenda item (a double booking there is the triangle too). Dashed means one thing:
    an open place ("1 open"). The words show only where there is room: the session panel, the person card and the Fix
    list (`issueLine`).
  - **Always visible**: must-fix markers always show (an essential conflict stays visible; the marker is
    that signal and the explanation is its contextual help). View › Rule marks adds the could-improve markers.
  - **Target size**: a marker that is its own button is a 24 × 24 px target (WCAG 2.2 SC 2.5.8). One inside a pill is
    drawn only: the pill is the target and says the words, and a click on the mark opens its popover.
  - **Week cell rhythm**: only a cell's first line clears the selection box (and a session marker beside it); the
    lines below take the cell's full width.
  - **Disclosure rule for markers**: a marker is never a disclosure. It has no arrow or chevron, opens no inline
    region, and its popover is a dialog anchored to it (`aria-haspopup="dialog"`, `aria-expanded`). What it reveals
    is said in its accessible name, not by a glyph.
- Schedule View settings, one view model: every grid
  reads the same Properties (the contract's registry, the view's on/off and order) and the same Colour by
  (`colourRule`, each colour in the key). One item renderer (`schedule_item.ts`: `itemParts`, `itemNodes`) draws every
  session or person's duty in every layout (Week pills, Month pills, Day bars, People × Days, the Timeline's chips, the
  phone agenda; print copies them): what an item says is the view's Properties in order and form, the duty code
  (the duty type's configured short code) included, never hidden by a column's width; each kind of item starts from the
  contract's `VIEW_ITEM_SHOWN_BY_DEFAULT`. Staffing numbers is one switch per view (`PlannerViewSettings.staffing`),
  also in the export. The List's columns are saved with the view, and the export's key repeats on every page. A
  setting is offered only where it is drawn.
- Person names: one renderer (`person_label.ts`) and one parser (`nameWords` in
  `dom.ts`, under the department's title and family-name particle lists) name every person; no screen formats a name,
  strips a title or makes initials itself. A name in a chip, cell, row or list item shows the label (the Schedule: its
  view's; elsewhere the person's own choice, else the department's) with the full name as its tip and read out after
  it (`nameText`, `personLink`; WCAG 2.5.3). Headings, panels, hover cards, sentences, stored reasons, Audit/History,
  the person card heading and exports keep the full name.
  The whole pipeline lives in `person_label.ts`: the label, its parts, each person's unique code and current
  rank (`shortCode`, `labelFacts`), and the Schedule's `displayName`. A person is never named as a record: `labelOf` refuses a person's row
  (types.ts `PersonRow`), so names use `personName`/`nameText`/`fullNameOf`. Initials are drawn by the one `avatar()`
  (dom.ts). `tests/test_name_guard.py` keeps both. A name pill in a Week or Month cell too narrow for
  its label (beside a docked panel) shows the person's short form (initial and family name), then their
  code. The cell's own width decides (`@container pill` on `.wk-cell`), never the window's. The full name stays the
  pill's tip and accessible name. Print keeps whole names. Published read-only chips (`.sd-greychip`), proposed
  additions and published-change joiners are the same pill with a modifier class, never a hand-drawn name; published
  chips carry the person (data-person), so a person's highlight rings them too
  (`tests/test_web_ssot_guards.py`).
- Tags (Configuration › Tags): one chip per tag everywhere (`tagRef` in `tags.ts`): "#Name" in the tag's palette hue
  with its member count; hover lists the members, click opens them with "Open tag". Pickers that choose people,
  clinics, places, duty types or programmes offer the tags labelling that kind under "Tags".
- Text never overflows controls: single-line labels truncate with a tooltip.
- Optional explanations use the existing accessible help control beside the thing they explain. Do not put
  migration, storage or implementation notes in permanent panels below working collections. Additional approved
  Rules remain discoverable from a counted context control with searchable rule details; migration stays inside
  that context and follows the normal approval flow. Actual conflicts, failed checks and stale results remain
  visible where they affect an action. Fix implementation defects rather than presenting them as caveats.
- If an application module cannot load, the startup screen offers a focused Reload button. Recovery must work
  without importing application modules and must not change successful startup or service-worker registration.
- Configuration checks show a compact status and contextual help. Unchanged rota comparisons are available on demand;
  new mandatory conflicts and incomplete or failed checks stay visible beside the affected action.
- Displayed dates stay whole. Ranges may wrap between dates, never between a date's day, month and year; narrow tables
  scroll within their own viewport, and pinned names must not cover other columns or their controls.

## Changes to a published rota (mockups B1, B2)

- A coordinator changes a published session from its panel: each person has one ⋯ menu (Replace…, Swap with another
  duty…, Mark absent, Move to another session…); the footer has Add a person and Close the session… (reason, approval
  choice, same-button armed "Confirm close?"). Closing has that one name everywhere: panel, change list ("Close") and
  the approver's "Closes the session". Someone already away (approved leave or Can't make it) gets Replace first
  in the footer and no Mark absent; a staged change shows in the panel in words with its Undo, and a staged closing
  reads "This session is closed". There is no month-wide amendment form for these.
- Replace opens inside the session panel with "‹ Back to session" and no tabs: Reason (Sick · Leave · Other), search,
  the shared candidate list (the first candidate is the only primary; a clash or leave row never offers "Break rule and
  assign"), then Review: WAS / NOW, one line "Passes every check" (its help: only the changed sessions and the changed
  people's other duties are checked; unchanged sessions keep the checks they were published with), Approval (Urgent
  "N approvers notified now" only when an urgent route is configured, else Routine), and one primary named after the
  step ("Send for urgent approval").
- Every change goes into one hidden change list per publication: the top bar shows "N changes · not sent" (amber
  while waiting); it opens "Changes to <month>", a docked list with one row per change (kind chip, what, when and
  where, state badge, its one fix such as "Find someone", Undo). "Send N changes for approval" stays disabled while a
  Must fix remains, the footer saying why; an urgent change may go alone. On the grid the person leaving is struck
  through and the person joining has a dashed ring until it is published.

## Print and export

- One *Print or export* dialog: paged preview left (the pages that print, white paper on `--sub`), settings right in
  labelled groups (Content, Page, Appearance, Include) under the Preset button; summary ("N pages · A4 landscape")
  and *Published sheet…* in the footer. Settings last for the session; closing never asks.
- Paper is always light (`.export-sheet` redeclares the light tokens): title block (view, department · period,
  status; a draft in error red), hairline under it, footer 9 px muted (printed by/date left, page numbers right),
  optional signature line. Greyscale: each duty hue also gets a pattern so types stay apart without colour.

## Constraints

CSP `style-src 'self'`: no inline style attributes; dynamic values use
`element.style.setProperty`. Preferences (theme, pinned sidebar) are per browser and
cleared at sign-out.

## Forms and people

- A person is edited in one place: their side panel (People's row button is **Open**; the panel's sections open the
  forms). Forms have no "Step by step / All on one page" toggle; they show all on one page with the steps as jump links.
- A form's problems show as a message under each field (`aria-invalid`, `aria-describedby`) and the first one gets focus,
  never only a red border or a passing browser bubble. Save also says the first problem politely ("2 fields to fix. …");
  a value left wrong (−5 minutes, "not-an-email") is said on leaving the field; an empty required field waits for Save.
  A schema refusal from the server shows the server's field messages, never "the highlighted fields" when none are.
- Required and optional fields are marked in words, the fewer kind only (`markFields`, `fieldCue` in dom.ts; WCAG
  3.3.2): when most fields are required, the optional ones say "(optional)"; otherwise the required ones say
  "(required)". No asterisk. The words stay out of the field's name (W3C Understanding 2.5.3, text in parentheses): the
  required state is the field's own `required`, "(optional)" is its description. Switches, read-only fields and choice
  groups that always hold a value are neither. A label that already says "(optional)" by hand counts and stays.
- Names are isolated: a shown name or label sits in a `<bdi>` (`bidi` in dom.ts; each " · " part of a label
  in its own), and a name inside an on-screen sentence (a toast) is marked with `isolate` (FSI…PDI), which `el()` draws
  as a `<bdi>`, so an Arabic name never reorders the words, dots or numbers beside it. FSI/PDI never reach page text,
  search, sort, exports, the clipboard or the server; accessible names need none (read in logical order). Sources:
  [W3C i18n, Inline markup and bidirectional text](https://www.w3.org/International/articles/inline-bidi-markup/),
  [UAX #9 isolates](https://www.unicode.org/reports/tr9/#Explicit_Directional_Isolates),
  [MDN `<bdi>`](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/bdi).
- Text cut by `.truncate` or `.clamp-2` shows its full text in the shared hover tip while it is cut (WCAG 1.4.12); no
  screen adds its own title for that.
- Every dropdown looks like the shared picker it opens (the picker's caret, no native chrome).
