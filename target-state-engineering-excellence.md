# Target State — Engineering Excellence Reference
### What "good" looks like for a high-throughput, API/middleware-heavy application

> **Status: reference ideal, not a committed design.**
> This is the north star you measure the current state *against*. It is deliberately generic — the industry's best-understood picture of a healthy delivery system. Phase 0 diagnosis tells you *how far* the real application sits from this, and the **gap** — not this document — decides which parts are worth adopting, in what order, for this specific app. Do not treat any line here as a decision. Treat it as the benchmark.

---

## The governing idea

A healthy delivery system has one property above all: **the speed of creation and the speed of safe validation are matched.** When creation outruns validation, defects escape downstream (UAT, production). When validation outruns creation, people wait. "Good" is equilibrium — and the gate that holds quality is *automated and mandatory*, never dependent on individual discipline.

As AI raises creation throughput, the only way to stay in equilibrium is to raise validation throughput by the same multiple — which means automation and agentic assistance on the *validation* side, not just the authoring side.

---

## Target state by SDLC touchpoint

Each phase below states the **principle** (what good *is*), the **enforced gate** (what makes it non-optional), and the **measurable marker** (how you'd know). The markers draw on DORA and widely-used engineering benchmarks.

### 1. Plan / Design
- **Principle:** work is small, testable, and has acceptance criteria *before* code is written. Untestable stories produce untestable code.
- **Gate:** a story doesn't enter development without acceptance criteria and a test approach.
- **Marker:** stories sized to < 3 days; every story has explicit acceptance criteria.

### 2. Code
- **Principle:** small, single-purpose changes; code and its tests authored together; nothing lands directly on main.
- **Gate:** branch protection — no direct push to main; feature branches short-lived.
- **Marker:** median PR < 200 lines; branch lifetime < 2 days; trunk-based or short-lived-branch flow.

### 3. Review
- **Principle:** every change gets a second pair of eyes; review is fast enough not to become the bottleneck.
- **Gate:** ≥ 1 approval required; no self-merge.
- **Marker:** time-to-first-review < 4h; PR cycle time (open → merge) < 24h.

### 4. Build
- **Principle:** deterministic, reproducible builds; a red build blocks everything.
- **Gate:** build must be green to proceed.
- **Marker:** build < 15 min; high green-build rate on main.

### 5. Test (the heart of it)
- **Principle:** a layered, automated test pyramid — fast unit tests at the base, integration and contract tests in the middle, a focused regression and performance tier on top. Mandatory, not advisory.
- **Gate:** all tiers pass **and** coverage meets threshold before merge is possible.
- **Marker:** coverage at/above the agreed bar on changed code; flaky-test rate < 2%; regression suite automated and trusted; **for API/middleware-heavy systems, contract tests cover every integration seam** — this is where most UAT escapes originate.

### 6. Merge / Integration
- **Principle:** changes integrate against the *latest* main, in order, automatically — no manual races.
- **Gate:** a merge queue tests each change against current main before it lands.
- **Marker:** no "green-when-I-opened-it, red-when-I-merged" churn; queue clears same day.

### 7. Deploy / Release
- **Principle:** deployment is automated, repeatable, and reversible; **merging is decoupled from releasing** via feature flags, so unfinished work is safe and releasing is a business decision, not an engineering event.
- **Gate:** no manual pushes — ever. Deploy is one automated, auditable action.
- **Marker:** deployment frequency on-demand / daily; lead time commit → prod < 1 day; rollback is a routine, tested action.

### 8. Operate
- **Principle:** production is observed; regressions are caught by the system, not by users; recovery is fast.
- **Gate:** alerting and rollback wired in before release.
- **Marker:** change-failure rate < 15%; MTTR < 1h.

---

## The headline scorecard — DORA + one

The numbers an executive will recognise, plus the single KPI that captures "defects escaping to UAT."

| Metric | Good | Elite |
|--------|------|-------|
| **Defect Removal Efficiency** (caught before release ÷ total) | > 85% | > 95% |
| Deployment frequency | On-demand / daily | Multiple per day |
| Lead time for change (commit → prod) | < 1 day | < 1h |
| Change-failure rate | < 15% | < 5% |
| MTTR (restore service) | < 1 day | < 1h |

**DRE is the one that maps to the actual complaint.** Everything else is in service of moving it.

---

## Where the agentic layer sits in the target state

Good practice separates two things that are easy to conflate:

- **Deterministic gates** (tests, coverage checks, linters, security scans, the merge gate) — these must be reproducible and auditable. A gate nobody can trust is a gate everybody bypasses, which is the failure mode that creates UAT escapes. **These are never agentic.**
- **The agentic layer wraps *around* the gates**, where judgment adds value: generating tests and raising coverage, triaging failures (flaky vs real), root-causing and *proposing* fixes, pre-reviewing PRs for a human, clustering a defect backlog into root causes.

**Autonomy boundary:** agents *propose*; humans *approve* the high-risk actions (auto-fix, auto-merge), especially during a stabilization period. Define that boundary explicitly — it's the same governance posture any agentic system needs.

---

## The one-line definition of done

> A change from any developer can travel from **written → production** *safely, same-day, without a human being the bottleneck and without a defect reaching UAT.*

That is the target. The next document — the Phase 0 diagnostic — measures how far today's reality is from it.
