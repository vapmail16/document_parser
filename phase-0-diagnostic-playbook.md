# Phase 0 — Engineering Health Check: Diagnostic Playbook
### Ready-to-execute. Objectives, methods, Claude Code prompts, and the perception survey.

> **Purpose:** take an honest, evidence-based snapshot of the current delivery system *before* committing to any target design. Output is a scored current-state picture and a shortlist of critical areas — which become the priority order for the stabilization strategy.
>
> **Duration:** 2–3 weeks. **Owner:** you. **Inputs needed:** read access to the repo(s), CI/CD config and logs, the defect/backlog export, and the team for the survey.

---

## The governing principle: two independent data streams

| Stream | Captures | How | Trust it for |
|--------|----------|-----|--------------|
| **Reality** | What the system actually is | Prompts/commands run against repo, CI, backlog | Facts: coverage, branches, gates, hotspots |
| **Perception** | What the team believes & experiences | Anonymous survey + a few interviews | The *why*, the pain, the workarounds |

Run both. **Where they disagree is itself a finding** — "everyone reports they test thoroughly; coverage is 31%" tells you more than either number alone.

---

## The dimensions (what we're assessing)

| # | Dimension | Core question | Primary method |
|---|-----------|---------------|----------------|
| 0 | Stack profile | What are we even looking at? | Prompt (run first) |
| 1 | Source control & branching | How controlled is the flow into main? | Git commands + prompt |
| 2 | Test inventory & coverage | What's tested, what isn't? | Coverage tool + prompt |
| 3 | CI/CD & gates | Can untested code reach UAT? | Prompt on CI config |
| 4 | Code quality & risk hotspots | Where does the code rot / where will it break? | Static analysis + prompt |
| 5 | Review practices | Is review real or rubber-stamp? | Git-host data + prompt |
| 6 | Defect backlog (the 70) | What are the systemic root causes? | Prompt on backlog export |
| 7 | Integration / API seams | Where do the UAT escapes actually originate? | Prompt (API-heavy focus) |
| 8 | Performance readiness | Can we even reproduce the volume issues? | Prompt + interviews |
| 9 | Perception | What does the team experience? | Survey |

---

## How to run the technical diagnostics

**A note on what's deterministic vs what needs judgment.** Some of this is a plain command (counting branches, running the coverage tool) — the number is the number. The rest is where Claude Code earns its place: reading the code and config to *interpret*, classify, and rank. Below, each area gives you both — the hard command where one exists, and the copy-paste prompt for the judgment part. Run these with Claude Code at the repo root.

**Calibrate first.** Run Diagnostic 0, then substitute your real stack (language, test runner, coverage tool, CI system) into the later prompts where noted.

---

### Diagnostic 0 — Stack profile (run this first)

```
Analyze this repository and produce a one-page "stack profile":
- Primary languages and frameworks, with rough % of the codebase each.
- Build tool(s) and how a build is invoked.
- Test framework(s) and the exact command(s) used to run tests and produce coverage.
- Location and type of CI/CD configuration.
- Dependency/package manifests.
- Overall architecture: monolith, modular monolith, or microservices — and list the
  deployable units and the main inter-service / external API boundaries.
Output as a short structured profile I can use to calibrate further analysis.
```
**Capture:** the profile. Use it to fill in `<test command>`, `<coverage tool>`, `<ci system>` below.

---

### Diagnostic 1 — Source control & branching

**Commands (deterministic):**
```bash
# Active remote branches
git branch -r | wc -l
# Branch age — oldest first (stale = risk)
git for-each-ref --sort=committerdate refs/remotes/ \
  --format='%(committerdate:short)  %(refname:short)' | head -50
# Direct commits to main vs merge commits (last 500)
git log origin/main --oneline -500 --merges | wc -l
git log origin/main --oneline -500 --no-merges | wc -l
# Commit size distribution (proxy for PR size)
git log --oneline -200 --shortstat main
```
**Prompt (interpretation):**
```
Using this repo's git history, assess branching health:
- How many active branches, and how many are stale (no commit in 30+ days)?
- Is main protected in practice — how many commits land directly vs via merge?
- What's the distribution of change size per merge (small/large)?
- Are there long-lived divergent branches that will cause painful merges?
Flag the top 3 branching risks and what each implies for merge conflicts and UAT escapes.
```
**Capture:** branch count, stale count, direct-to-main %, change-size distribution, top risks.

---

### Diagnostic 2 — Test inventory & coverage

**Command (deterministic):** run the project's own coverage, e.g. `<coverage tool>` from Diagnostic 0.
**Prompt (interpretation):**
```
Inventory and assess this codebase's tests:
- Count test files and individual test cases.
- Classify each test as unit / integration / contract / e2e / performance, based on
  what it actually exercises (not just its folder name).
- Produce a coverage-by-module table; identify modules with little or no test coverage.
- Flag low-value tests: skipped tests, tests with no meaningful assertions, tautological tests.
- Cross-reference with git churn: list the top 10 HIGH-CHURN, LOW-COVERAGE modules —
  these are the highest-risk areas (changed often, barely tested).
Output: the coverage table, the test-type breakdown, and the top-10 risk list.
```
**Capture:** total coverage %, coverage by module, test-type mix, top-10 high-churn/low-coverage modules.

---

### Diagnostic 3 — CI/CD & gates

**Prompt:**
```
Read the CI/CD configuration (<ci system>) and the deployment process end to end:
- Document every pipeline stage in order.
- For each quality check (tests, coverage, lint, security), state whether it BLOCKS the
  merge/deploy or is merely advisory.
- Identify every manual step, especially any manual deploy or manual push to an environment.
- Trace every path by which a change could reach UAT or production WITHOUT passing the
  automated tests. List each such path explicitly — these are the escape routes.
- Note pipeline duration if logs/config reveal it.
Output: the stage map, a blocking-vs-advisory table, and the ranked list of escape routes.
```
**Capture:** stage map, which gates block vs advise, manual steps, **the escape routes** (this directly answers your manager's complaint).

---

### Diagnostic 4 — Code quality & risk hotspots

**Command (deterministic):** run your static analyser (SonarQube / Semgrep / language linter).
**Prompt (interpretation):**
```
Assess code-level risk:
- Identify the 15 highest-complexity functions/modules and the most-duplicated blocks.
- Aggregate static-analysis findings; summarize the critical security issues by type.
- Combine complexity + duplication + git churn + low coverage into a single
  "risk hotspot" ranking. Rank modules by likelihood-of-defect × blast-radius.
Output: the hotspot ranking with a one-line reason per entry, and a security summary.
```
**Capture:** hotspot ranking, critical security findings, complexity/duplication summary.

---

### Diagnostic 5 — Review practices

> PR size, review latency and approvals live in the git host (GitHub/GitLab/Azure DevOps), not plain git. Pull via the platform's API/MCP connector; use the git proxies from Diagnostic 1 if the host data isn't reachable.

**Prompt:**
```
From the git host's pull-request data (last 90 days), assess review discipline:
- Median PR size (lines, files) and time-to-first-review and time-to-merge.
- % of PRs merged with zero review or self-approved.
- Reviewer concentration: is review spread across the team or bottlenecked on a few people?
- Any correlation between large PRs / fast merges and later defects?
Output: the review metrics, the bottleneck finding, and the top 3 review risks.
```
**Capture:** PR size, review latency, self-merge %, reviewer concentration.

---

### Diagnostic 6 — Defect backlog root-cause clustering (the 70 items)

**Prompt (attach the backlog export):**
```
Here is the current defect/feature backlog export [attached]. Analyze all items:
- Cluster them by (a) functional theme, (b) suspected root-cause area in the codebase,
  (c) severity.
- Identify the 5–8 SYSTEMIC root causes that likely explain the majority of items —
  i.e. the few causes behind many symptoms.
- Map each cluster to the module(s) most likely involved.
- Separate genuine defects from disguised feature requests.
Output: a cluster table, the root-cause hypotheses ranked by how many items each explains,
and the "fix these modules first" shortlist.
```
**Capture:** clusters, 5–8 systemic root causes, module shortlist. *(This converts 70 tickets into a handful of fixes.)*

---

### Diagnostic 7 — Integration / API seams  *(critical for this app)*

**Prompt:**
```
This application is API/middleware-heavy. Map its integration surface:
- Every API it EXPOSES and every external service / queue / middleware it CONSUMES.
- For each integration point: is there a contract test, schema validation, and a
  versioning strategy? Mark present/absent.
- Produce a dependency & coupling map between internal components and external systems.
- Flag every seam with NO automated test — rank by how central it is to core flows.
These untested seams are the most likely source of defects that unit tests miss and that
only surface in UAT or under integration load.
Output: the integration inventory, the coupling map, and the ranked list of untested seams.
```
**Capture:** integration inventory, coupling map, **ranked untested seams** (your most probable UAT-escape source).

---

### Diagnostic 8 — Performance / volume readiness

**Prompt (+ interviews with whoever's seen the issues):**
```
Assess readiness to diagnose the reported volume-based performance issues:
- Find any existing load/performance tests and any defined SLOs (latency/throughput).
- Identify endpoints and data paths most exposed to volume: high call frequency, large
  payloads, N+1 query patterns, unbounded result sets, synchronous calls that should be async.
- Rank suspected volume bottlenecks by exposure.
- Propose a minimal load-test scenario set that would reproduce the reported issues
  against production-like volumes.
Output: existing-coverage gap, ranked bottleneck suspects, and the proposed load scenarios.
```
**Capture:** perf-test gap, SLO presence, ranked bottleneck suspects, load scenarios to build.

---

## The perception survey  (Dimension 9)

Anonymous. ~5 minutes. Likert scale **1 (strongly disagree) → 5 (strongly agree)** unless noted. Run across all ~50 developers; a handful of 30-min interviews with leads adds depth. The goal is to surface what the metrics can't: workarounds, fear, and where people *believe* the pain is.

**A. Process clarity**
1. I know exactly what steps my change must pass before it reaches production.
2. The branching and merge process is clear and consistently followed by everyone.
3. When something goes to UAT with a defect, we understand why it got through.

**B. Testing confidence**
4. I trust the test suite to catch a regression before it reaches UAT.
5. I know what level of testing is expected of me before I open a PR.
6. Writing tests is treated as part of the work, not an optional extra.
7. Tests fail randomly (flakiness) often enough that I sometimes ignore failures. *(reverse-scored)*

**C. CI/CD & merge**
8. Our pipeline is fast enough that it doesn't slow me down.
9. Code cannot reach an environment without passing automated checks. *(if they disagree, that's your escape route, confirmed from the floor)*
10. Deployments are smooth and low-stress.
11. Manual steps are required to get a change deployed. *(reverse-scored)*

**D. Review**
12. My PRs get reviewed quickly enough.
13. Reviews are substantive, not rubber-stamps.

**E. The honest ones (open text)**
14. What is the single biggest thing that slows down getting a change safely to production?
15. Where do defects most often escape, in your experience, and why?
16. What workaround or shortcut do you take under deadline pressure that you wouldn't recommend?
17. When Claude Code lands, what worries you most about quality at higher volume?

**F. AI-readiness (1–5)**
18. I would trust an automated gate to block my merge if coverage or tests failed.
19. Our current process is ready to absorb a big increase in the volume of changes.

> Scoring: average each section; anything < 3.5 is a perception red flag. Compare against the technical findings — matches confirm, mismatches are the richest findings.

---

## Synthesis — turning findings into a decision

1. **Score each dimension R / A / G** from the evidence (not opinion).

   | Dimension | Evidence summary | R/A/G | Gap vs target state |
   |-----------|------------------|-------|---------------------|
   | 1 Branching | | | |
   | 2 Testing & coverage | | | |
   | 3 CI/CD & gates | | | |
   | 4 Code quality | | | |
   | 5 Review | | | |
   | 6 Backlog root causes | | | |
   | 7 Integration seams | | | |
   | 8 Performance | | | |
   | 9 Perception | | | |

2. **Gap analysis** — for each dimension, distance from the *Target State* reference. The gap, not the ideal, is what you act on.

3. **Critical areas** = dimensions that are **Red AND high-impact on the 6-month stabilization goal.** That intersection — usually 3–4 items — is the shortlist.

4. **Feed the strategy.** The critical areas become the priority order in the release-management strategy and the sequencing of the roadmap. You're no longer guessing where to start; the evidence points.

> **Deliverable out of Phase 0:** a 1-page scorecard, the critical-areas shortlist with evidence, and the gap-to-target summary. That is what you take into the strategy conversation — diagnosis first, target second, plan third.
