# Reviewer framework expansion — cross-repo proposal

A design proposal that split into **four milestones**:

- **Milestone A** — Versioning primitives in `khonliang-bus-lib` (supersedes `tolldog/khonliang-bus-lib#12`). Ship immediately.
- **Milestone B** — `.reviewer/` directory + deterministic check framework in `khonliang-reviewer`.
- **Milestone C** — Reusable GH Action for CI hard-gate on version bumps.
- **Milestone D** — Benchmark harness with cross-source scoring (ground truth / local / external).

## Files

- `proposal.md` — v3 design doc, ready for FR authoring. Incorporates 23 review findings across two review passes.
- `v1-review.json` — first review (Claude sonnet-4-6, 15 findings on v1).
- `v2-review.json` — second review (Claude sonnet-4-6, 8 findings on v2; all addressed in v3).
- `v2-review-ollama.json` — local-LLM comparison (qwen2.5:32b, 2 surface findings on v2; calibration data for the routing policy).

## Provenance

Drafted by session Claude on 2026-04-21 during reviewer-dogfood work.
`proposal.md` is v3; v1 and v2 drafts live in the session transcript, not here.
The review JSONs are kept as audit trail for the proposal's iteration and as
seed corpus candidates for Milestone D's benchmark suite.

## FRs authored

**Milestone A** (versioning primitives):

- `fr_khonliang_4e60ffaa` — `khonliang_bus.versioning` module: `resolve_version()` with pyproject-walk + BaseAgent integration. Priority: high. **Status: COMPLETED** (`tolldog/khonliang-bus-lib#13` merged).
- `fr_khonliang_31b750d7` — `add_version_flag()` CLI helper + adoption across agent `main()` entry points. Priority: medium. Depends on: `fr_khonliang_4e60ffaa`. **Part 1 merged via `tolldog/khonliang-bus-lib#14`**; part 2 (per-agent-repo adoption) pending.

**Milestone B** (reviewer output quality — derived from external-research distillation; see below):

- `fr_reviewer_dfd27582` — severity_floor post-filter (noise control). Priority: high. Sourced from Greptile v4 A/B results + "make LLMs shut up" post.
- `fr_reviewer_cb081fa8` — multi-reviewer consensus mode (parallel local + external, merge by similarity). Priority: medium. Sourced from Greptile "Two Reviewers" post + our local-first-iterate memory.
- `fr_reviewer_eeebcba2` — finding-address-rate tracking (scrape GitHub reactions/replies per finding_id). Priority: medium. Sourced from Greptile v4 headline metric.

**Milestones C, D** — not yet authored as FRs. Draft after B starts landing.

## External research corpus

Ingested 2026-04-21 via researcher.fetch_paper(s):
- CodeGPT blog: "Choosing the Best Ollama Model for Coding (2025)" — `1478ad79d94dd814`
- DEV.to: "Ollama Cloud Models for Code Review — Honest Comparison" — `2e51a600bc6e480f`
- Local AI Master: "Best Local AI Coding Models for Ollama (2026)" — `a90b0a21f4bf2fd5`
- Greptile blog index + 15 individual posts — `f703c271ba19beb0` + batch of 15 entry IDs

Distilled subset (sufficient for the FRs above): "Two Reviewers", "Sandboxing agents at the kernel level", "Greptile v4", "Content-ification of Software", "Best Local AI Coding Models". Remaining 10 Greptile posts are pending distillation — pick up when Milestones C/D FRs need the signal.

## Status as of 2026-04-23

Milestone A has **landed**: `tolldog/khonliang-bus-lib#13` merged `fr_khonliang_4e60ffaa`
(versioning primitives) and `tolldog/khonliang-bus-lib#14` merged part 1 of
`fr_khonliang_31b750d7` (`add_version_flag()` CLI helper). Per-agent
adoption of `add_version_flag()` (part 2 of `fr_khonliang_31b750d7`)
is still pending.

Milestones B/C/D are partially in flight — the Milestone B FRs listed
above have been authored and their first implementations are landing
in parallel PRs. Refer to the live FR store for current status.
