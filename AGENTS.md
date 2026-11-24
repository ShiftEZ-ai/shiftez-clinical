# Rules for agents

## Project facts
- Repository: https://github.com/ShiftEZ-ai/shiftez-clinical · main branch: main
- Board (private): https://github.com/orgs/ShiftEZ-ai/projects/2 · Project brief: the board's README
- Release plan: https://github.com/ShiftEZ-ai/shiftez-clinical/milestones (the milestones are the plan; never copy
  them here)
- Leads (thread titles): "ShiftEZ Master", "ShiftEZ Delivery", "ShiftEZ Design"
- UI project: yes · environments: ci, dev, staging, production (each described in DELIVERY.md § Environments)
- Other repos: none
- Links: DELIVERY.md (how code ships) · DESIGN.md (look and feel) · the portfolio engineering standard and the
  orchestration skill (private; agents receive both through their session hooks)

## Product and current goal
A clinical rota product: planning, rules, auto-scheduling (OR-Tools CP-SAT), review, approval and publication,
fairness, and staff preferences. It serves any clinical department; none is built in. The current release serves one
user, who manages people, requests and rules, and creates schedules by hand and by auto-schedule, with fast screens.

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
- Rule reach: `packages/domain/rule_map.py`.
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
- Every finding is an issue first, one per root cause. Every PR names its issue on a line of its own: "Closes #n",
  or "Refs #n" for a partial fix. "Part of #n" and "Fixes #n" are refused.
- Tests at every layer the change touches, by DELIVERY.md § Test policy; no skipped or retried tests.
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
- Safety, approval and exception rule changes: the Master Lead, with one independent full-diff review (a risk path).
- DESIGN.md principles and mockups: the Design Lead.
- Release and data-safety policy: the Delivery Lead (DELIVERY.md).
- Changing AGENTS.md: the Master Lead.

## Risk paths (need one independent review before landing)
A risk-path change gets one independent review of the full PR diff (merge base to exact head), recorded as a verdict
that names the head. A re-cut that only merges `main` keeps the earlier verdict only when the Delivery Lead confirms a
clean merge. No review for a same-tree re-cut, a rebase, or a test-only change that keeps every assertion. Everything
else lands on green checks. Risk paths are rule resolution and safety rule kinds, sign-in, permissions, privacy,
migrations and anything that deletes or rewrites data, the pipeline's own gates, production configuration and
policy files:
- Safety rules and rule resolution: `packages/domain/conditions.py`, `packages/domain/rule_lines.py`,
  `packages/domain/solver_rules.py`, `packages/contracts/rule_catalogue.py`, `SAFETY_*` in
  `packages/contracts/schemas.py`. Changing safety, approval or exception rules goes to the Master Lead.
- Sign-in and permissions: `apps/api/auth.py`, `apps/api/oidc.py`, `apps/api/services/access.py`,
  `apps/api/services/workspace_access.py`, `apps/api/services/auth_configuration.py`, `PERMISSIONS` in
  `packages/contracts/schemas.py`.
- Privacy: `apps/api/data_crypto.py`, `packages/contracts/audit.py`, read filtering of personal data.
- Migrations and data deletion: `migrations/`, any command that deletes or rewrites data.
- Pipeline gates and policy files: `.github/`, `ci/`, `tests/quarantine.json`, test-runner and build configuration,
  `package.json`, `AGENTS.md`, `DELIVERY.md`, `DESIGN.md`.
- Production configuration: `infra/`, `apps/api/settings.py`.

## Data safety
- Migrations only add; never edit, rename or delete a landed migration (`migrations/safety.py`,
  `tests/test_data_safety.py`). A release never erases production data.
- Every production deploy takes a verified backup first; never copy one environment's data into another.
- Publication and audit evidence is never deleted or rewritten. Deleting or rewriting data needs the owner's
  separate, explicit yes.

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
