# Rules for agents

## Project facts
- Repository: https://github.com/ShiftEZ-ai/shiftez-clinical · main branch: main
- Board (private): https://github.com/orgs/ShiftEZ-ai/projects/2 · Project brief: the board's README
- Release plan: https://github.com/ShiftEZ-ai/shiftez-clinical/milestones (the milestones are the plan; never copy
  them here)
- Leads (thread titles): "ShiftEZ Master", "ShiftEZ Delivery", "ShiftEZ QA", "ShiftEZ Design"
- UI project: yes · environments: ci, dev, staging, production (each described in DELIVERY.md § Environments)
- Other repos: none
- Links: DELIVERY.md (how code ships) · DESIGN.md (look and feel) · the portfolio engineering standard and the
  orchestration skill (private; agents receive both through their session hooks)

## Product and current goal
A clinical rota product: planning, rules, auto-scheduling (OR-Tools CP-SAT), review, approval and publication,
fairness, and staff preferences. It serves any clinical department; none is built in. The current release puts one
department of about 150 people on it: the department's administrators manage people, access, requests, rules and
announcements and create schedules by hand and by auto-schedule; every employee can use a phone to see their schedule,
open any duty or colleague for details and contact numbers (within the contact privacy rules), send requests and swap
requests, read announcements and sync their schedule to a calendar. Screens are fast on desktop and phone. Later
releases host many organisations on one deployment, each isolated from the others.

## Commands
- Set up: `npm ci` · `pip install --require-hashes -r requirements/build.lock -r requirements/test.lock -r requirements/postgres.lock`
- Build the web client: `python scripts/build_web.py` · Run locally (API + worker): `python scripts/dev.py`
- Lint: `python -m ruff check --select F821,F822,F823 .` · Typecheck: `npm run typecheck` · Format: no formatter is
  enforced · All fast checks: `npm run check`
- Affected tests: `python scripts/affected_tests.py --run` · Full tests: hosted CI and the merge queue only
- Local services: a disposable PostgreSQL in `TEST_POSTGRES_URL` with `ROTA_ALLOW_DISPOSABLE_TEST_DB=yes` and
  `ROTA_ALLOW_TEST_ROLES=yes`; Chromium for browser tests in `CHROMIUM_EXECUTABLE`
- After a schema change: `python scripts/generate_contracts.py` (CI fails if it leaves a diff)

## Architecture and single sources
FastAPI (`apps/api`), worker (`apps/worker`), domain (`packages/domain`), contracts (`packages/contracts`), vanilla
TypeScript client (`apps/web/src`, built by `scripts/build_web.py`), PostgreSQL, Docker. One definition per concept,
reused everywhere:
- Rule conditions: `packages/domain/conditions.py` (validator, solver, snapshots, UI previews); the one frontend
  builder is `apps/web/src/conditions.ts`.
- Rule catalogue: `packages/contracts/rule_catalogue.py` (`RULE_CATALOGUE_V2`, one entry per idea).
- Rule lines: `packages/domain/rule_lines.py`, called by `conditions.active_rules`; every check reads rules through it.
- Schedule score: `packages/domain/score.py`.
- Fairness: `packages/domain/fairness.py`.
- Golden scenarios: `tests/scenarios/golden/v1/` (manifest, scenarios, known gaps), seeded through public API
  commands by `scripts/golden_scenarios.py` and checked by `tests/golden_checks.py`. It is the one reviewed
  rule-and-scheduling scenario pack; extend it, never start a second one. Department-scale fixtures stay in
  `tests/scheduler_departments.py`.
- Rule reach: `packages/domain/rule_map.py`.
- Tenancy and data ownership: `packages/contracts/tenancy.py` (the design note every tenancy change follows).
- Contracts: `packages/contracts/schemas.py` → `scripts/generate_contracts.py` → `apps/web/src/generated.ts`. Kinds,
  fields, limits and roles (`ROLE_NAMES`) come from the contracts or the API, never copied into the frontend.
- Branding: `BRANDING_DEFAULTS` in `packages/contracts/schemas.py` is the only place a brand string is written.
- Shared UI helpers: `apps/web/src/dom.ts` (one picker/menu, one collection toolbar, one badge/status palette, one
  form/stepper helper).
- Safety rule kinds (restriction, minimum rest, next-day combination, supervision, minimum cover, maximum people)
  are always mandatory: authoring refuses advisory for them, stored advisory ones are enforced as mandatory through
  one effective-severity function, and only a cover target may stay advisory.

## Must-follow coding rules
Definition of done:
- Every finding is an issue first, one per root cause. A review's non-blocking findings share one follow-up issue,
  one checklist line each; a finding gets its own issue when it is High or Blocker or another lead owns it.
  Every PR names its issue on a line of its own: "Closes #n",
  or "Refs #n" for a partial fix. "Part of #n" and "Fixes #n" are refused.
- Tests at every layer the change touches, by DELIVERY.md § Test policy; no skipped or retried tests.
- A fixed bug in rule resolution, rule checking or auto-scheduling that a small rota reproduces adds or extends a
  golden scenario with its expected outcome and check, in the same PR. When the pack cannot state it (no binding
  rule, outside the CP-SAT path), the PR says so and the regression test sits with the code it fixes.
- Commit messages say what changed and where (no backend change under `fix(web)`). A user-visible change adds one
  `Changelog:` line to the PR description. Decisions and the options considered go in the issue or PR.
- A PR that changes engine, validator, greedy, score or fairness outputs says "outputs change for existing data".
- Before every commit, read `git status --short`, `git diff --cached --stat` and `git diff --cached`; before opening a
  PR, read its full diff. Preserve unrelated work. Work in your own git worktree; never delete through a
  `node_modules` junction (remove the junction first).

How to work:
- Only real blockers block: something that stops a core job, loses or exposes data, or breaks a safety rule.
  Cosmetic findings are recorded and do not hold a merge. Do not stall or over-engineer; work in parallel where the
  work is independent, using subagents with clear file ownership.
- Use the rules of the checkout and revision you work on. Ask only when a real conflict leaves scope or consequences
  unresolved. When unsure about a fact, library or guideline, check the official docs and current standards and cite
  them in design notes (WCAG 2.2 AA, OWASP, PostgreSQL, OR-Tools, TypeScript, Playwright, GitHub).
- Read instructions critically, this file included, and propose fixes. Weigh user, developer and agent experience
  together.
- The smallest correct change; reuse existing helpers; no speculative abstraction. Mark a deliberate shortcut with a
  `ponytail:` comment naming its ceiling and upgrade path.

Product rules:
- General: nothing specific to one institution, department, clinic, rank or person (no name, emblem, roster, rota
  shape or constant). Differences are data and settings; department-specific importers live outside this
  repository, and an install without them works with the generic import.
- Dynamic: rules and settings apply to every dimension (person, rank, skill, programme, rotation type, cohort, tag,
  clinic, location with descendants, duty type, weekday, time), evaluated at the moment of the duty.
- Configurable: every operator setting is editable in the app with defaults, validation, Undo and audit; other
  changeable values live in environment or config files, not in source.
- Scale: ≥150 people, ≥500 sessions a month, ~1,000 rules with history, years of publications; keep processing
  roughly linear. Report CP-SAT status (OPTIMAL, FEASIBLE, INFEASIBLE, UNKNOWN) accurately; feasible is not optimal
  and never weakens a mandatory constraint.
- Rules: one rule per idea (minimum, maximum and target are one rule; scope set once); per-person or per-programme
  differences are lines, and person facts and protected time are data. Retired versions are never counted. Among
  lines with the same *where*, the most specific *who* wins (person › rank/programme/tag/skill › everyone); lines
  with a different *where* all apply.
- Mandatory rules are hard constraints; advisory rules are scored preferences. Only rota adjustments, person
  exceptions, target overrides and approved, more specific lines relax a rule, and none ever weakens a safety kind
  (see Architecture): every matching line applies and the strictest binds.
- A preview or trial write captures its 'after' state inside the savepoint, before the rollback: the snapshot itself, or the complete inputs it is built from, where a missing input fails the preview rather than reading state from after the rollback.
- Rule changes go propose → approve (a different person) → activate, audited and undoable. A department may grant an
  audited, time-bound self-approval permission; every use needs a reason and is announced and marked Self-approved.

UI rules (detail in DESIGN.md, owned by the Design Lead):
- Every screen, control and sentence earns its place; no taglines, helper text or secondary labels (errors,
  empty-state next steps and accessible names are not secondary labels). Never show what is already obvious.
- Collections share one toolbar: Saved view → period → Search → Filter → Group → Sort → View settings. One dropdown
  behaviour. One-line footer (screen status left, general status right, +N overflow). No coloured side or edge bars.
  Configuration lives together; auto-schedule options in one panel.
- Decisions, primary actions, required fields and actionable conflicts stay visible; help never covers a defect.
- Undo for changes, same-button confirm for destructive actions, unsaved-changes guard, keyboard access, visible
  focus, colour never the only signal, light/dark/system themes, desktop and phone, WCAG 2.2 AA.
- CSP is `style-src 'self'`: no inline style attributes; set dynamic values with `element.style.setProperty`.
- A major new screen starts from an approved mockup.

## Who decides
- Only the owner decides: money; access only he can grant (admin access included); secrets; production
  deployments; destructive operations on real data; making private things public; closing the project.
- Safety, approval and exception rule changes: the Master Lead.
- DESIGN.md principles and mockups: the Design Lead.
- Release and data-safety policy: the Delivery Lead (DELIVERY.md).
- Changing AGENTS.md: the Master Lead.

## Critical changes (one independent review before landing)
Only a critical change gets a review, meaning a change whose failure would be irreversible, silent or public. Everything else is opened and merged by the thread that made it once the required checks are green, with no second review or sign-off. Critical paths in this repository:
- Data loss: `migrations/`, any command that deletes or rewrites data, retention, audit and history tables (`packages/contracts/audit.py`, `apps/api/services/audit_log.py`), backups and restore.
- Data exposure: sign-in and permissions (`apps/api/auth.py`, `apps/api/oidc.py`, `apps/api/services/access.py`, `apps/api/services/workspace_access.py`, `apps/api/services/auth_configuration.py`, `PERMISSIONS` in `packages/contracts/schemas.py`); privacy and secrets (`apps/api/data_crypto.py`, read filtering of personal data); API response shapes and queries that change which personal data a role sees; exports, prints and outbound messages that carry personal data; new dependencies or install scripts; making anything public; network or access-control changes.
- Approval separation: `apps/api/services/independence.py`, `apps/api/services/reviews.py` (who may approve, and when a second person is required).
- Clinical safety rules: `packages/domain/conditions.py`, `packages/domain/rule_lines.py`, `packages/domain/solver_rules.py`, `packages/contracts/rule_catalogue.py`, `SAFETY_*` in `packages/contracts/schemas.py`. These become CI-only once the golden scenarios cover every safety rule kind and option (#743).
- Merge gates: `.github/`, `ci/`, `tests/quarantine.json`, test-runner and build configuration, the scripts in `package.json`, and deleting, skipping or weakening a test.
- Production: `infra/`, `apps/api/settings.py`, production deploys.

A critical change gets one independent review of the full PR diff (merge base to exact head), recorded as a verdict that names the head. A re-cut that only merges `main` keeps the earlier verdict when the publisher confirms a clean merge (same tree replay, same patch). No review for a same-tree re-cut, a rebase, or a test-only change that keeps every assertion.

## Data safety
- Migrations only add; never edit, rename or delete a landed migration (`migrations/safety.py`,
  `tests/test_data_safety.py`). A release never erases production data.
- Personal data includes staff directory and contact details, accounts and access records, rota assignments,
  training, requests, availability, preferences, notification and calendar records, and feedback. Records without
  an expiry are kept for scheduling and history. Archiving a draft preserves its contents.
- The application offers administrators an audited leaver workflow. After the person is marked inactive,
  `person.anonymise` clears identifying directory and account fields, private notes, copied delivery addresses
  and unapplied staged payloads. It disables sign-in and revokes active sessions and calendar feeds. Rotas and
  fairness keep a former-staff reference. See `apps/api/services/leavers.py` and `tests/test_leavers.py`.
- Publications, audit records and definition history can retain personal references indefinitely. The leaver
  workflow removes private leave reasons from availability history. Deleting or rewriting real data requires
  the owner's separate, explicit yes.
- Worker cleanup deletes expired sessions after `EXPIRED_SESSION_DAYS`, sign-in attempts after the sign-in rate
  window plus one day, rate-limit rows at their expiry and authentication events after
  `password_login_audit_retention_days`. Mailbox consent expires at `oauth_flow_minutes`. Cleanup removes the flow
  after use, expiry or loss of an active session or eligible access. It removes mailbox connections when access ends.
- The worker clears screenshots at the contract's `screenshot_days` limit and staged edits at the department's
  `bulk_staged_days` limit. The report or lifecycle record remains. Solver resume state expires under
  `generation_resume_days`. See `apps/worker/main.py` for cleanup.
- Take a verified backup before every production deploy. Backups age out under the rotation in
  [Backup and restore](DELIVERY.md#backup-and-restore). Never copy one environment's data into another.

## Privacy
- The repository is public under a proprietary licence (source-visible only; no open licence). Public text cannot be reliably erased:
  treat every write as permanent.
- Never in public text (code, tests, fixtures, commits, branch names, issues, PRs, comments): personal names, any
  institution or department, hosts, addresses, ports, providers, host-shaped names, internal paths, private
  repository names, secrets, real data, or anything derived from them (digests, prefixes, re-encodings).
- People are written by role. Sample people come only from the project's screened name generator; test data is
  synthetic, never derived from a real roster. When you change a value, update every line that asserts it and every
  derived form (initials, codes, sort keys, slugs, digests).
- Every README stays empty (a test enforces it); the repository carries no product description, screenshots,
  roadmap, guides or marketing text. Nothing names or reveals where the product runs.
- AGENTS.md, DELIVERY.md and DESIGN.md are public, scrubbed copies; every change to them passes the privacy check.
- Never print or commit secrets or credential files; never type passwords in automation.
- If something private is found in the repository, stop and report it; do not fix it with a new public commit.
