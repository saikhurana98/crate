# Implementation Strategy — Multi-Agent Build Plan

> Operational playbook for building this project with parallel Claude Code remote agents (CCR routines) coordinated by a conductor agent. Reads alongside [`ARCHITECTURE.md`](ARCHITECTURE.md) — that document defines *what* we're building; this one defines *how it gets built*.

## 1. Operating model

- **Conductor agent (autonomous)**: a long-running routine that reads `WORK.md`, picks the next unblocked unit, spawns a worker routine for it, watches the resulting PR's CI, auto-merges on green, updates `WORK.md`, and loops. Runs from Phase 0 onward.
- **Worker agents (one per unit)**: short-lived routines spawned by the conductor. Each receives a self-contained prompt with: the unit's file ownership boundary, the relevant ADRs, the contract test it must satisfy (where applicable), branch + commit conventions, and a PR template to fill in.
- **Auto-merge**: PRs that pass every required CI check are merged automatically by the conductor (via `gh pr merge --auto --squash`). No human in the loop on merge for normal units. Humans intervene only when a PR fails CI three times or when a worker explicitly asks for guidance via a `BLOCKED:` PR comment.
- **Test gate is the only gate.** Robust unit + integration tests are mandatory in every PR. CI runs them. Auto-merge only fires on full green.

## 2. Decomposition principles

1. **One agent owns one disjoint file set** during its window. Two agents never touch the same file at the same time.
2. **Dependencies flow up the hex.** Lower layers must be merged before higher layers start: `domain → ports → contracts/protocol → application + adapters/infrastructure → composition → integration`.
3. **Contracts are the handoff.** A port-defining agent merges first; adapter agents implement against the merged port + the parametric contract test suite.
4. **One unit = one PR.** Each agent works on `agent/<phase>-<unit>` branched off latest `main`, opens a PR, and exits when the PR is auto-merged. Agents never amend or force-push.
5. **CI is the truth.** Branch protection requires every check below to pass before merge.

## 3. Required CI checks (branch protection on `main`)

| Check | Tool | Pass criterion |
|---|---|---|
| `typecheck` | `pnpm -r typecheck` | exit 0 |
| `lint` | `pnpm -r lint` (biome) | exit 0 |
| `test:unit` | `pnpm -r test:unit` (vitest) | exit 0, coverage ≥ thresholds (see §4) |
| `test:integration` | `pnpm -r test:integration` (vitest, longer) | exit 0 |
| `contract-drift` | regenerate JSON Schema from both `protocol/messages.ts`, diff against `contracts/ws-protocol.schema.json` | identical |
| `dependency-cruiser` | `pnpm dep-rules` | no layer violations |
| `pr-template` | grep PR body for required headings | all headings present, none empty |
| `docs-touched` | for any PR touching a port shape, schema, or invariant, require `ARCHITECTURE.md` or `docs/adr/**` to be in diff | match or comment-justified |

All eight required. No bypass. `--no-verify` and admin-merge are disabled.

## 4. Test bar (the bar for auto-merge)

### Unit tests
- Every public function in `domain/`, `application/`, `infrastructure/`: at least one happy-path + one failure-path test.
- Coverage thresholds (vitest `coverage.thresholds`): **lines ≥ 85, branches ≥ 80, functions ≥ 90**.
- Mocks limited to ports and external IO. Domain types tested with no mocks.

### Contract tests
- Every adapter registers itself in its port's `tests/ports/<port>.contract.ts` parametric suite.
- Contract suite enforces: signal-aborts honoured within 100 ms, errors typed as Result, idempotency where declared, capability-flag honesty.
- An adapter PR is rejected if the contract suite fails for it.

### Integration tests
- One end-to-end per high-value scenario, container-built with fakes:
  - `live-set.e2e.ts` — request → rip wins → playable → endgoal wins → upgraded → final
  - `bulk.e2e.ts` — playlist of 10 fake tracks → group lifecycle → group.completed
  - `upgrade-daemon.e2e.ts` — provisional row → daemon picks → endgoal succeeds → final
  - `crash-recovery.e2e.ts` — kill mid-flight → restart → state cleaned, jobs re-flagged
  - `cancellation.e2e.ts` — user cancel mid-rip aborts within 200 ms; group cancel preserves provisionals
  - `verification.e2e.ts` — fingerprint mismatch in live-set → reject; ≥ 0.95 → accept; ≥ 0.75 background → review-pending
  - `ws-protocol.e2e.ts` — subscribe/unsubscribe/verbose toggle + replay/backpressure
- Live-source integration tests gated by `LBMD_TEST_LIVE_SOURCES=1`. CI runs only the fake-backed suite. Manual `LIVE` run required for source-adapter PRs before merge label is added.

### Mutation testing (advisory, not gating in v1)
- `stryker-mutator` runs nightly on `main`, posts a report. Goal: ≥ 70 % mutation score on `domain/` and `application/`. Below threshold → tracked in `WORK.md`.

## 5. Branch + commit + PR conventions

### Branches
- `agent/<phase>-<unit>` — example: `agent/3-source-onthespot`, `agent/4-application-scheduler-race`
- Branched from latest `main`. Never long-lived; rebase on conflict.

### Commits
- [Conventional Commits](https://www.conventionalcommits.org/). Allowed types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`, `ci`, `perf`.
- Scope = the unit (the unit name from `WORK.md`).
- Subject ≤ 72 chars, imperative mood.
- Body explains the *why* if non-obvious.
- Trailer: `Co-Authored-By: <agent-id>`.

### PR template (enforced by `pr-template` check)

```markdown
## Unit
<unit-name from WORK.md>

## Summary
<1-3 sentences>

## Files added/changed
- `path/...`

## Tests added
- unit: <list>
- integration: <list or 'n/a'>
- contract: <self-registration in tests/ports/...>

## Documentation updates
- ARCHITECTURE.md: <section touched, or 'no semantic change'>
- ADRs: <added/updated, or 'none — no new decision'>
- WORK.md: <unit marked complete>

## Decisions made within ADR freedom
<list any micro-decisions not captured by ADRs; justify each>

## Deferred
<anything left as TODO with reason; references issue # if applicable>

## Risk / blast radius
<low | medium | high — if medium/high, explain>
```

Empty sections fail `pr-template` check.

## 6. Conductor agent — operating contract

The conductor is a single autonomous remote routine, scheduled to run continuously (re-arms itself via `ScheduleWakeup` while `WORK.md` has unmerged units).

### Loop

```
1. git pull --rebase main
2. parse WORK.md
3. find next unit U where:
     U.status == "ready"
     all of U.dependencies have status == "merged"
4. if none found AND no units in "in-progress":
     all done — exit
5. if none found AND some units in "in-progress":
     wait 5 min, re-check (sleep via ScheduleWakeup, do not poll)
6. mark U "in-progress" in WORK.md, commit + push to main directly
   (WORK.md updates by conductor are exempt from PR flow — single-writer)
7. spawn worker remote routine with self-contained prompt for U
8. watch the worker's PR via gh pr checks every 60s (sleep via ScheduleWakeup):
     - if all required checks pass: gh pr merge --auto --squash
     - if any check fails: post comment with logs, increment U.failure_count
       - failure_count == 3 → mark "blocked-needs-human" + slack/notify
       - failure_count < 3 → close PR, retry: spawn fresh worker for U
9. once PR merged: mark U "merged" in WORK.md, set merged_at timestamp
10. goto 1
```

### Worker prompt structure (template the conductor uses)

```
You are a worker agent for the lbmd project. Your unit is <unit-name>.

Repo: https://github.com/saikhurana98/load-balanced-music-downloader
Branch (create from latest main): agent/<phase>-<unit>

Required reading (read in full before writing any code):
- ARCHITECTURE.md
- IMPLEMENTATION.md
- WORK.md (for context — your unit and its dependencies are listed there)
- The following ADRs: <list of relevant ADR paths>
- The following ports/contracts: <list of paths>

Your file ownership (you may create/edit ONLY these paths):
<exact list>

Files you MUST NOT touch (will be enforced by CI):
<exact list of off-limits paths>

Deliverable:
1. Implement the unit per the architecture and ADRs.
2. Write robust unit tests (coverage ≥ thresholds) AND integration tests where applicable.
3. Update ARCHITECTURE.md if your work changes a documented invariant.
4. Add an ADR under docs/adr/ if you made a non-trivial decision not already captured.
5. Update WORK.md: mark your unit complete (status: "merged" — set by conductor on merge).
6. Open a PR using the template; fill every section.
7. If blocked or uncertain, post a PR comment starting with "BLOCKED:" and exit. Do not guess at out-of-scope decisions.

Conventions:
- Conventional Commits (feat/fix/refactor/test/docs/chore/build/ci/perf)
- Branch: agent/<phase>-<unit>
- Co-Authored-By trailer with your agent ID
- Run `pnpm install && pnpm typecheck && pnpm lint && pnpm test` locally before pushing — your PR will be auto-merged if CI is green.
- Do not amend or force-push. Open new commits for fixes.
```

### Conductor's WORK.md edit rights

The conductor is the **only** writer of `WORK.md` status fields. Workers may append to a `Notes:` section under their unit but never edit status. This avoids a merge conflict on every PR.

## 7. Phase plan (locked)

| Phase | Units | Parallelism | Gate |
|---|---|---|---|
| 0. Bootstrap | 1 | sequential | merged before Phase 1 |
| 1. Domain + ports + protocol | 3 (1A domain, 1B ports, 1C protocol) | parallel after 0; 1A merges first | all three merged before Phase 2 |
| 2. Contract test suites + fakes | 1 | sequential | merged before Phase 3 |
| 3. Adapters + infrastructure | 14 (3A–3R) | full parallel after 2; 3D waits for 3P | each merged independently |
| 4. Application use cases | 7 (4A–4G) | parallel after Phase 1 + Phase 2 | each merged independently |
| 5. Composition + entry point | 1 | sequential after Phase 3 + Phase 4 | merged before Phase 7 |
| 6. Debug UI | 2 (6A, 6B) | parallel from Phase 1C onward | merged before Phase 7 |
| 7. Integration + smoke | 1 | sequential after Phase 5 + Phase 6 | merged → project ready |

Critical path: 0 → 1A → 2 → (3 + 4 in parallel) → 5 → 7. With 5-way parallelism in Phase 3 + 4, wall-clock target ~25 hours.

## 8. Unit catalogue (the work breakdown)

| ID | Phase | Owns (paths under `packages/core/src/` unless noted) | Deps |
|---|---|---|---|
| **0-bootstrap** | 0 | repo root files, `pnpm-workspace.yaml`, both `package.json`, both `tsconfig.json`, `biome.json`, `vitest.config.ts`, `vite.config.ts`, `.github/workflows/ci.yml`, `.github/PULL_REQUEST_TEMPLATE.md`, `dependency-cruiser.config.cjs`, root `.gitignore`, empty `contracts/ws-protocol.schema.json` skeleton | — |
| **1A-domain** | 1 | `domain/{result,error,track,job,quality,candidate,group,fingerprint,cover-art,external-ref}.ts` + tests | 0 |
| **1B-ports** | 1 | `ports/{source-adapter,metadata-resolver,audio-verifier,track-store,file-writer,input-parser,bulk-expander,cover-art-fetcher,browser-session,clock,logger}.ts` | 0, 1A |
| **1C-protocol** | 1 | `protocol/messages.ts` (core), `apps/debug-ui/src/protocol/messages.ts`, `contracts/ws-protocol.schema.json`, `.github/workflows/contract-drift.yml` | 0, 1A |
| **2-contracts-fakes** | 2 | `tests/ports/<port>.contract.ts` (each port), `tests/fakes/{source,metadata,verifier,store,writer,browser,bulk-expander,cover-art}.ts` | 1A, 1B, 1C |
| **3A-onthespot** | 3 | `adapters/sources/onthespot.ts` + tests | 2 |
| **3B-ytdlp-ytmusic** | 3 | `adapters/sources/ytdlp-ytmusic.ts` + tests | 2 |
| **3C-squid-qobuz** | 3 | `adapters/sources/squid-qobuz.ts` + tests | 2 |
| **3D-lucida** | 3 | `adapters/sources/lucida.ts` + tests | 2, 3P |
| **3E-monochrome** | 3 | `adapters/sources/monochrome.ts` + tests | 2 |
| **3F-soulseek** | 3 | `adapters/sources/soulseek.ts` + tests | 2 |
| **3G-musicbrainz** | 3 | `adapters/metadata/musicbrainz.ts` + tests | 2 |
| **3H-spotify** | 3 | `adapters/metadata/spotify.ts`, `adapters/cover-art/spotify-art.ts`, `adapters/bulk/spotify.ts`, `adapters/parsers/spotify-link.ts` + tests | 2 |
| **3I-apple** | 3 | `adapters/metadata/apple.ts`, `adapters/cover-art/apple-art.ts`, `adapters/bulk/apple.ts`, `adapters/parsers/apple-link.ts` + tests | 2 |
| **3J-ytmusic** | 3 | `adapters/metadata/ytmusic.ts`, `adapters/bulk/ytmusic.ts`, `adapters/parsers/ytmusic-link.ts` + tests | 2 |
| **3K-coverart-misc** | 3 | `adapters/cover-art/{coverartarchive,lastfm-art}.ts` + tests | 2 |
| **3L-chromaprint** | 3 | `adapters/fingerprint/chromaprint-acoustid.ts` + tests | 2 |
| **3M-parsers** | 3 | `adapters/parsers/free-text.ts` + parser composition + tests | 2 |
| **3N-sqlite** | 3 | `infrastructure/sqlite/{store.ts, migration-runner.ts, migrations/001_init.sql}` + tests | 2 |
| **3O-nas-writer** | 3 | `infrastructure/nas/writer.ts` + tests | 2 |
| **3P-browser-pool** | 3 | `infrastructure/browser/playwright-pool.ts` + tests | 2 |
| **3Q-ws-server** | 3 | `infrastructure/ws/{server.ts, protocol-validator.ts, subscription-registry.ts}` + tests | 1C, 2 |
| **3R-ops** | 3 | `infrastructure/ops/{metrics.ts, health.ts, logger-pino.ts}` + tests | 2 |
| **4A-scheduler** | 4 | `application/scheduler/{race.ts, eligibility.ts, health.ts}` + tests | 1A, 1B, 2 |
| **4B-resolver-chains** | 4 | `application/{resolver-chain,cover-art-chain}.ts` + tests | 1A, 1B, 2 |
| **4C-download-live-set** | 4 | `application/download-for-live-set.ts` + tests | 4A, 4B |
| **4D-download-bulk** | 4 | `application/download-bulk.ts` + tests | 4A, 4B |
| **4E-upgrade** | 4 | `application/upgrade-quality.ts`, `application/upgrade-daemon.ts` + tests | 4A |
| **4F-verification** | 4 | `application/verify-track.ts`, `application/verification/decide.ts` + tests | 1A, 1B, 2 |
| **4G-purge-daemon** | 4 | `application/purge-daemon.ts` + tests | 1A, 1B, 2 |
| **5-composition** | 5 | `composition/{container.ts, config.ts}`, `index.ts`, `config/lbmd.config.example.json` + container tests | all of 3, all of 4 |
| **6A-debug-ui-shell** | 6 | `apps/debug-ui/src/{main.ts, ws-client.ts, app.tsx}` + tests | 1C |
| **6B-debug-ui-components** | 6 | `apps/debug-ui/src/components/**`, styles + tests | 6A |
| **7-integration** | 7 | `tests/integration/**`, `apps/debug-ui/tests/e2e.spec.ts` | 5, 6B |

## 9. Repo-wide ground rules for every agent

1. **Read before write.** First action of every worker: read the listed required files in full. No assumptions from training data.
2. **Stay in your lane.** File-ownership boundary is hard. Use `git diff --name-only` before push; if any out-of-scope path is touched, abort and revert.
3. **Doc updates inline.** If your unit changes a port shape, schema, invariant, or introduces a library not yet in any ADR, update `ARCHITECTURE.md` and add or amend an ADR in the same PR.
4. **No silent decisions.** Every micro-decision not covered by ADRs is listed in the PR's "Decisions made within ADR freedom" section. Reviewers (and future agents) read this.
5. **Tests are not optional.** A PR without tests fails the `test:unit` coverage gate automatically. Don't ship "TODO test" — block on it instead.
6. **Conventional Commits, no force-push.** Fixes are new commits. The squash-merge consolidates them.
7. **`BLOCKED:` is a respected signal.** A worker that posts `BLOCKED: <reason>` and exits is not punished — it triggers a human escalation. Better than a guess that auto-merges.
8. **Secrets stay out of code.** All third-party credentials referenced via `process.env.LBMD_*` and documented in `SECRETS.md`. Worker creating a new secret-using adapter must update `SECRETS.md` in the same PR.

## 10. Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| Worker writes outside its lane | `dependency-cruiser` + `git diff` lane check in CI | PR fails, conductor closes, retries fresh worker (failure_count++) |
| CI flake | retry once via `gh pr rerun` | if still failing, treat as real failure |
| Two agents racing on the same unit | conductor is the sole writer of WORK.md status; no race possible by construction | n/a |
| `WORK.md` corrupted by an errant worker | CI rejects PRs that modify `WORK.md` status fields (only conductor's commits to main allowed) | revert offending commit |
| Conductor crashes / routine ends | re-arm via `/schedule` (one-time or recurring) | manual restart |
| All units done but integration tests fail | Phase 7 worker posts `BLOCKED:` with failing test list | human triage, possibly issues opened on individual units |
| ADR conflict (two PRs both edit same ADR) | git merge conflict on rebase | conductor closes the later PR, requires the worker to rebase |

## 11. Definition of done (project level)

The project is "v1 implementation complete" when:

- All units in `WORK.md` are `merged`.
- `pnpm install && pnpm -r typecheck && pnpm -r lint && pnpm -r test` passes on a fresh clone.
- All integration scenarios from §4 pass with fakes.
- A live smoke run against at least Squid/Qobuz + OnTheSpot succeeds end-to-end (single track + small playlist).
- The debug UI connects, submits a request, and renders both opaque and verbose modes correctly.
- `ARCHITECTURE.md` matches the implementation (no documented invariants violated).
- `SECRETS.md` lists every required credential with acquisition instructions.

## 12. Prerequisites before kicking off the conductor

1. **GitHub app access**: install Claude Code GitHub app on the repo so remote routines can clone + push. Without this, the routines auto-disable on first run (we hit this earlier).
2. **Branch protection** configured on `main`: required checks listed in §3, no force-push, auto-merge enabled.
3. **CI workflows** committed (handled by Phase 0).
4. **`SECRETS.md` skeleton** committed (placeholder; actual creds added by adapter PRs).
5. **Conductor routine created** via `/schedule` — recurring (e.g., `*/30 * * * *`) or one-shot self-rearming. Prompt linked to this `IMPLEMENTATION.md`.

## 13. References

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — what we're building
- [`docs/adr/`](docs/adr/) — every locked decision and why
- [`WORK.md`](WORK.md) — live state of which unit is where
- [`SECRETS.md`](SECRETS.md) — credentials map
- [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) — what every PR must include
