# Delivery

Follows engineering standard v1.22. Owned by the Delivery Lead. Engineering rules are in AGENTS.md and the design
contract is in DESIGN.md.

## Branching and merging
- One trunk, `main`, with temporary release branches. The repository rulesets and workflows protect `main` and
  the release branches. Changes to either land through pull requests.
- A change lands only as a pull request through the merge queue with auto-merge, once its required check is green on
  the exact head; the queue's run on the combined tree squash-merges it. No human approval click, no merge train or
  batch gate. There are no admission caps, drain flags or per-PR holds, except the one fair-share admission rule and
  release holds, and no bypasses.
- Fetch the target branch before cutting a change. Follow-up commits on an open pull request end with a merge
  of the current target tip.
- One pull request per piece of work: its fixes, tests and docs stay on the same branch. Draft until the piece is
  done, then ready once. Batch findings that share a root cause; do not hold an independent fix for a whole audit.
  Every revision keeps working. About 500 changed lines is a review heuristic, not a limit. An unfinished feature
  may sit behind an existing admin feature flag, off by default, when the off state is safe.
- Every pull request names its issue as AGENTS.md § Must-follow coding rules says.
- Every pull request has one owner until it is merged or closed. The owner watches its run and fixes any failure or
  conflict. A pull request dropped from the queue is fixed at its root cause and queued again; never retry an
  unchanged deterministic failure. A cancelled or skipped check is not a pass.
- Issues and pull requests are used normally and are public. Outside issues are answered by role, never with private
  facts. Outside pull requests are closed with: "Thank you. We close outside pull requests: the licence is
  proprietary and there is no contributor agreement."
- Check a sample by content before bulk-closing issues.

## Review
- [Critical changes](AGENTS.md#critical-changes-one-independent-review-before-landing) get one independent full-diff
  review. Record the verdict on the pull request against its exact head before arming auto-merge. Tests verify
  fixes for review findings. A later critical change requires a new verdict. For other pushes, record the new
  head and the diff covered by the existing verdict before arming again.
- All other changes use auto-merge on green required CI, with no independent review or lead sign-off. Required CI and
  branch protection apply to every change.

## Release branches
- Cut `release/vX.Y.Z` from a release-candidate tag whose commit includes the release-branch workflow. First land
  each fix on `main`, then cherry-pick it through a pull request targeting the release branch. Write a fix directly
  against the release branch only when `main` has moved too far for a cherry-pick. Cut later candidate and final
  tags from the release branch.

## Checks
- A pull request runs static checks plus the tests its diff against the target base can reach, proven by hiding a
  change on purpose. Merge groups and release-branch pushes run the full unit and browser suites. A queue
  push to `main` reuses the successful merge-group reports only after verifying the exact commit, tree, base,
  full test coverage and protected CI controls. A push without that proof runs both suites.
- The required check is `ci-gate`. It checks producer results on every CI event and verifies the selected test
  reports. On a qualifying main push it independently verifies the queue proof, retains the qualification reports
  with their original run identity, and links to that run for the shard reports. If the nightly capacity check
  defers the suites, the gate records the deferral.
- Every check, build, image publish and release job runs on GitHub-hosted runners, never on a machine of ours. A
  guard fails any workflow whose `runs-on` is not a GitHub-hosted label.
- Jobs run in parallel; independent jobs never wait on each other; suites are sharded and shard counts follow
  measured times. Caches hold only dependencies keyed by lock hash, never results.
- Workflows use only GitHub-owned and `docker/*` actions pinned by full commit SHA, with a read-only default token.
  A pull-request job has no secret and no write token; never run pull-request code with either, and never use
  `pull_request_target` on untrusted code. Workflows from forks get no secrets and need approval to run.
- One fast privacy check of secrets, names and hosts runs as a pre-push hook and as a required check. Issue and
  pull-request text gets the same screen before posting. A failed check means nothing is sent; never work around it.
- The policy guard and trusted verdict run from `main`'s copy. They read policy data from the event's target base
  for pull requests and merge groups, and from the previous commit on a branch push. A newly created release
  branch uses its starting commit, and reader changes land before the policy or ruleset changes they support.
- Dependency pins, lock files and the product modules the checks rely on are allowed and get the label
  `changes what the checks run on` for visibility.
- The required check counts collected tests at the base and the head. Each disappearing test is declared in the
  removal ledger at node level; an undeclared disappearance fails, a declared one shows as `collect=+a/-b`.
- Planted canary tests prove in every run that the reporter reports outcomes honestly.
- Migrations are checked append-only against the target branch (`migrations/safety.py`, `tests/test_data_safety.py`).
- `python scripts/generate_contracts.py` must leave no diff.
- Delivery tooling retries every GitHub read and fetch on a transport error, a 5xx or an HTTP 499 with back-off (15 s,
  30 s, 60 s, 60 s, about 3 min, longer than the measured 30-90 s bursts on the GitHub edge), and on a rate limit
  (60 s, 120 s, ...). A write retries only when it is idempotent (a fast-forward push of one fixed commit, disabling
  auto-merge, a dequeue); any other write that fails on a burst is re-read before it is tried again, never repeated
  blindly.
- Pushed commits carry no Co-authored-by trailers for persons and no AI-tool trailers; the pre-push check refuses
  both. Queue squashes carry GitHub's App-bot author trailer; that is expected.

## Test policy
- No retries to get green, no skipped or quietly removed tests. Fix causes; never re-run an unchanged failure.
  Until issue #885 removes the exception, the trusted runner re-runs eligible failed nodes once in the same job on
  pull requests, merge groups and pushes to the default branch (`.github/scripts/trusted_ci.py`).
  On default-branch pushes, a failure followed by a pass is recorded as unstable.
- A failing test is repaired by restoring what it asserts. Never weaken, skip, delete, quarantine or
  re-parameterise away a failing assertion.
- A replaced test keeps every assertion, or says for each retired one which design decision made it untrue.
- A known-broken test sits in a visible quarantine with an owner, a fix link and an expiry. Only demonstrated
  flakiness is quarantined, never a product defect. Quarantined tests are listed in `tests/quarantine.json`, which an
  ordinary pull request never edits; a pull request that fixes a quarantined test appends its node id to
  `tests/quarantine-retired.json`, which is append-only, so the quarantine only shrinks. Only baseline rows are retired
  that way. Retire an unstable row by changing the quarantine registry. New failures block.
- A timing or performance test is never re-run blind: read its recent merge-group history first. If it failed in 3
  or more of the last 10 groups, or misses its budget at its best measurement, it is a real miss: route it to its
  owner and keep it listed.
- Validated correctness, security, privacy, clinical-safety and data-integrity checks block the unsafe change,
  including pull requests already open.
- Regression coverage: every behaviour change is covered at the layers it affects, unit and API tests for logic,
  permissions and contracts, browser tests for affected journeys and shared UI. Use the role list in
  `packages/contracts/schemas.py` (`RoleName`/`ROLE_NAMES`), not a copy. Across the suite, cover empty,
  filtered-empty, loading, failure, read-only, selection, unsaved/saved, populated and department-scale states;
  relevant roles; desktop 1440 and phone 390; light and dark; and the create → save → reload → edit → submit →
  approve → publish journeys. Prefer behavioural assertions over incidental geometry, exact request counts or fixed
  sleeps. An acceptance the owner asks for is run by agents as the critical journeys, with one visual report to the
  owner; automated tests do not replace it.
- After changing a trusted CI script, run every test file that imports or drives it, with the event name unset,
  `pull_request` and `merge_group`, in bounded runs, not only the new behaviour's tests; fake events carry
  `repository.default_branch`.
- Browser tests run only on the hosted runners, never on a local machine, not even one node.
- Local runs: only the affected tests (`python scripts/affected_tests.py --run`), against a disposable local
  PostgreSQL (`TEST_POSTGRES_URL`, `ROTA_ALLOW_DISPOSABLE_TEST_DB=yes`, `ROTA_ALLOW_TEST_ROLES=yes`,
  `CHROMIUM_EXECUTABLE`). Full suites are the hosted runners' job.
- Local runs on a shared machine also follow that machine's private rules (worker cap, time bound, no browser
  tests), kept in the private project brief.
- Critical-change reviews read the code and run short targeted tests, including a fail-before run, each under two
  minutes. Full suites run on hosted runners.
- Wait for states (`expect`, `wait_for`), never fixed times; `tests/test_timing_guard.py` rejects new
  `wait_for_timeout`/`sleep` calls. Browser tests use `sign_in` unless the sign-in form is under test, and create
  data through `h.command(...)` unless the creating UI is under test. To diagnose, set `PW_TRACE_DIR` (a failed test
  keeps its traces); `PW_CPU_THROTTLE=4` and `PW_API_DELAY_MS=800` reproduce a loaded runner.
- Tests use local services and mocks only: never a remote or paid service, never a real secret.

## Environments
- **ci**: per change, on the runner; synthetic data only.
- **dev**: deployed automatically on every merge; the fast-moving one. Synthetic data and test accounts only (names
  ending "(test)").
- **staging**: tagged release candidates only; the owner's testing and the speed gates run there. Its own synthetic
  data, identities and keys, never a copy of another environment.
- **production**: only with the owner's yes, after he has tested the release candidate on staging; real accounts
  only. The production administrator account is created by the owner.
- Configuration and secrets are per environment. Secrets live only on the servers, never in the repository, its
  variables or its environments.
- Never copy one environment's database into another, and never copy test identities or rotas into production.

## Releases
- Versions are `vX.Y.Z`; release candidates are `vX.Y.Z-rc.N`, tested on staging.
- Version tags build, scan and publish the image for their exact commit. The published build contains public code only.
- Before tagging, qualify the exact release tree with the full checks and require zero failed or skipped tests.
- Before creating either tag, run `python scripts/check_release_version.py TAG COMMIT` from the repository root and require it to pass.
- Release tags (`v*`) are immutable with no bypass, and only an admin or a bypass actor of the tag-creation ruleset
  may create one. The Delivery Lead names the exact commit. The project's GitHub App creates the tag once it holds
  that bypass; until then the tag is created under the owner's login on the Portfolio Lead's recorded line.
- Before every final tag (not an `-rc.N`), sweep the whole product: every page for every role (`ROLE_NAMES`),
  desktop 1440 and phone 390, light and dark, the states in § Test policy and the critical journeys, looking at the
  screenshots. Fix the real blockers it finds first.
- Engine/compiler, validator, greedy, score and fairness version constants are bumped once per release, in release
  preparation, never per pull request.
- Release notes are compiled from the pull requests' `Changelog:` lines. They carry a Known issues section built from
  the open issues on the release milestone (blockers and test or CI issues excluded) and a Limitations section; an
  open blocker that ships in a candidate by ruling is added by hand.
- The GitHub Release is published from the tag with the release notes above and links back to its milestone; the
  milestone then gets its Release line and is closed.
- Release evidence is cited by sha256 and is append-only. Add a dated correction and disclose any change after
  approval the same day.

## Deploys
- A hosted job builds the image and publishes it by digest. Each commit has a separate image concurrency group.
  No workflow connects to a host, no host is a runner and no host holds a credential for the repository.
  Nothing in the repository names or reveals where the product runs.
- Main images published to the public package carry a provenance attestation signed by `image.yml` on
  `refs/heads/main`. The dev slot verifies it offline and pins the signer workflow and ref. Staging and production
  images do not carry this attestation yet.
- The operator machine pushes the image to each server over a private network by exact digest; the server verifies
  it by image ID and revision label and reports nothing back. Every deploy is recorded. Deploy definitions and host
  facts live outside this repository.
- dev: the operator PC relays every green main build's image digest to the dev slot through a sealed, single-purpose
  gate that accepts only green main digests and records each deploy.
- staging: only tagged release candidates, promoted by digest by hand.
- production: only with the owner's yes, after he has tested the candidate on staging.
- **Gated releases:** a release gate (such as the dev relay gate) gets one independent review
  and gets one Portfolio Lead GO; after that, every change that passes the gate releases automatically, with an audit
  row. A new kind of change needs a new GO. Production keeps the owner's yes for every deploy.
- Staging runs the scanned image digest built for the candidate tag. Production takes exactly the digest staging
  qualified for the same commit. Record the staged and promoted digests.
- **Production approval:** the owner approves production deployments only. Delivery puts a "Deploy vX.Y.Z to
  production" item in Waiting on owner on the board, with the test report; the owner's yes is the approval and
  nothing else is.
- **Every production deploy:** a verified backup first, the previous image kept for rollback, a health check after;
  roll back at once on failure.
- A migration on the reviewed-destructive list runs only with an explicit confirmation naming its id. The Delivery
  Lead confirms on non-production environments after a verified backup; only the owner confirms on production; a new
  list entry needs the Master Lead's word.
- Before redeploying an environment while a diagnosis is open, save every api and worker container log first: a
  redeploy recreates the containers and their logs are lost.
- What only the owner decides is listed once, in AGENTS.md § Who decides; an ordinary release approval never covers
  a destructive data operation.

## Deviations

- **One artifact per commit.** Main builds a second image for the public package with Buildx provenance disabled.
  The private build's embedded provenance contains non-public build metadata. Review on 2026-12-23.

## Hotfixes
- A hotfix is its own board item. Use the release-branch flow for the patch release, test the new candidate on
  staging, and get the owner's yes before deploying to production.

## Security
- Dependabot, code scanning and secret scanning alerts belong to the Delivery Lead. An alert raised by a pull request
  is that pull request owner's to resolve before merge.
- Code-scanning dismissals: record one independent review verdict on the reason, then dismiss with the reason
  recorded on the alert. The project's GitHub App performs the dismissal once it holds security-events write; until
  then it is done under the owner's login on the Portfolio Lead's recorded line.
- The privacy check's inputs are pinned by digest: one changes only through a coordinated pin move by the Delivery
  Lead, never in place, because any change stops all publishing.
- A ruleset change is an approved packet: the change, the body saved before and the readback after, applied only on
  the Portfolio Lead's recorded approve-settings line; a change that grants admin or bypass access needs the owner's
  yes.
- Every free GitHub security feature is on and stays on; nothing paid is enabled without the owner's yes.
- Secrets never enter the repository, its variables or its environments.
- Before publication, the public main image and every version-tag image go through the privacy scan. The scan
  checks the config and all added layers. It verifies pinned base layer digests and hashes from installed wheel RECORDs. A finding in
  package-manager metadata is allowed when its line is byte-identical to a line at the same path in the verified
  base layers. The exception table binds historical wheel bytecode findings to a path, class and SHA-256.
  New public builds disable wheel bytecode generation. Any other finding blocks the public push.
- Security reports go through GitHub's private vulnerability reporting.

## Speed and quality gates
- Measure before setting any limit (timeout, deadline, budget, cap). Run first with generous limits and timing
  instrumentation to see every failure and the real timings, then set the limit from measured data plus a generous
  margin. Safety bounds (security, data protection, resource caps, fail-closed checks) always stay. A run killed by a
  timeout rather than a real error means the limit was wrong: relax it.
- Speed is measured in the browser, on production-class hardware, to a written definition of "usable". Every gate
  number comes with an agreed tolerance, recorded with the gate.
- Before tuning a slow action, get its waterfall (every request, with server and client time).
- **Go-live gate (v0.1.1), on the production server:** pages ≤ 3 s at 150 people; a 150-person publish succeeds; a
  hand-edit save ≤ 5 s in the browser until the screen is editable again (v0.1.2 target: 3 s). The save number counts
  only when "Saving…" shows at once and the page stays usable, the next edit is possible during the background check,
  the save reply carries the edit's own findings (safety included), and the release notes carry a known-limitation
  line.
- Performance work is measured before and after; the regression budgets in `tests/test_rule_map.py` and
  `tests/test_scheduler_performance.py` hold. CP-SAT is measured on representative workloads with bounded time and
  resources.
- Pull-request runs (static checks plus affected tests) target a result in under 1 minute at p50 and under 2 minutes
  at p95, without letting CI lie: the full suite still runs in the merge queue, failures are never hidden, and no
  check is weakened. The pull-request check's wall time is a measured objective, and the merge-queue run is not
  slower than the pull-request run.
- **The one admission rule (fair share):** hosted runners have a shared concurrency ceiling (its measured number is
  in the private project brief). Every launch path (pull-request runs and merge-queue enqueues, which start merge groups) goes
  through one serialised admission decision that counts this repository's in-flight test jobs of all events and
  keeps room for the next merge group. The cap is set from measured numbers plus a margin, after duplicate runs and
  missing cancel-in-progress are fixed, so that this repository's job pickup p95 stays within 30 s and the other users
  of the shared ceiling keep their agreed share. Re-measure and reset it when the shared ceiling, the jobs per run or the pickup p95
  changes, and at least once per release.
- Moving to a queue-free trunk is considered only after two weeks with the full suite under 10 minutes, the queue
  green over 90% of the time and interaction failures under 1 in 20.
