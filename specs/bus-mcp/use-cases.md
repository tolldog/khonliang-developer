# Bus MCP Use Cases and Workflow Definitions

**Source:** Captured from actual Claude ↔ researcher session on 2026-04-09/10.
Each workflow below was executed manually during the session. The tool-call
sequences are real — these are not hypothetical designs but observed
interaction patterns formalized as replayable workflow definitions.

**Purpose:**
- Shape what to build after the bus is in place (use cases first, features second)
- Serve as acceptance criteria for bus migration steps
- Become test fixtures for the flow engine (replay against real data)
- Templates in the developer guide so Claude knows what's available

---

## Workflow Format

```yaml
name: workflow_name
trigger: "natural language description of when this runs"
agents: [which agents are involved]
bus_features_required: [what the bus needs to support this]
steps:
  - tool_name: { args }
output: what the caller gets back
```

Steps can reference prior step outputs via `{step.N.field}`. When a step
references multiple prior steps, the flow is a DAG (not a linear pipeline) —
the bus flow engine must support step references, not just sequential piping.

---

## Use Case 1: Research a Concept

**Observed session pattern:** User pastes a LinkedIn post about AI editing
pause mechanisms. Claude ingests it, backgrounds the research, and reports
back with a claim-by-claim assessment when complete.

**Today's execution:** 3 tool calls + 1 background agent, ~8 minutes wall time.

```yaml
name: research_concept
trigger: "look into {topic}"
agents: [researcher]
bus_features_required: [request_reply]
steps:
  - researcher.ingest_idea:
      text: "{topic}"
      source_label: "{source}"
  - researcher.research_idea:
      idea_id: "{step.1.id}"
      max_papers: 10
      auto_distill: true
  - researcher.brief_idea:
      idea_id: "{step.1.id}"
output:
  idea_id: "{step.1.id}"
  papers_found: "{step.2.papers_found}"
  papers_distilled: "{step.2.papers_distilled}"
  brief: "{step.3}"  # claim-by-claim assessment
```

**Session example:**
- Input: "AI editing pause mechanism — forced breath between seeing the problem and changing the code"
- Output: 15 papers found, 10 distilled. 3 claims evaluated: all mostly unaddressed in literature (novel concept). Recommended empirical studies.

**Variants observed:**
- `research_concept_batch`: User submitted 4 concepts in one session. Each ran as a background agent in parallel. Bus equivalent: 4 parallel workflow instances.
- `research_concept_with_urls`: User provided specific URLs alongside the concept. Additional `fetch_paper` steps inserted before `research_idea`.

---

## Use Case 2: Evaluate a Spec Against the Corpus

**Observed session pattern:** User asks Claude to review `specs/MS-01/spec.md`.
Claude reads the spec, searches the corpus for relevant evidence, cross-
references claims against papers, and produces a structured review with
specific issues and recommendations.

**Today's execution:** 6+ tool calls (read spec, search corpus, paper_context,
synthesize_topic, manual cross-referencing), ~15 minutes of Claude reasoning.
Most of the token cost was Claude composing the review, not fetching data.

```yaml
name: evaluate_spec
trigger: "review {path} against the corpus"
agents: [developer, researcher]
bus_features_required: [request_reply, cross_agent_calls]
steps:
  - developer.read_spec:
      path: "{path}"
      detail: "full"
  - researcher.find_relevant:
      query: "{step.1.title}"
      detail: "brief"
  - researcher.paper_context:
      query: "{step.1.title}"
  - developer.evaluate_with_evidence:  # MS-02 — select_best_of_n
      spec: "{step.1}"
      evidence: "{step.3}"
      papers: "{step.2}"
      method: "best_of_3"
output:
  spec_path: "{path}"
  evidence_count: "{step.2.count}"
  review: "{step.4}"  # structured review with evidence tags
```

**Session example:**
- Input: `specs/MS-01/spec.md`
- Output: 5 technical correctness issues (WAL mode, FR storage API, guides two-step, .format() undefined, relative path resolution), 4 smaller items, 1 architectural pushback (shared storage → independent). All backed by actual code verification.

**Note:** Today this was done entirely by Claude reasoning over tool outputs.
With `developer.evaluate_with_evidence` (MS-02), the local LLM does the
cross-referencing and Claude only reviews the result. Token cost drops from
~50K (Claude reasoning) to ~5K (Claude reading a pre-evaluated review).

---

## Use Case 3: Evaluate an Architecture Document

**Observed session pattern:** User asks Claude to read an architecture doc,
cross-reference it against the full paper corpus, identify what's supported
vs challenged by the literature, and produce a formal review with evidence.

**Today's execution:** 4 tool calls (ingest_file, distill, synergize,
synthesize_topic) + manual corpus searching + manual review composition.
~20 minutes. The synergize step revealed that the pipeline generates FRs
from paper-backed concepts but NOT from internal design doc concepts — a
useful diagnostic about the pipeline's strengths and limitations.

```yaml
name: evaluate_architecture
trigger: "review {path} — what does the literature say"
agents: [researcher, developer]
bus_features_required: [request_reply, cross_agent_calls, session]
steps:
  - researcher.ingest_file:
      path: "{path}"
  - researcher.start_distillation: {}
  - researcher.synergize:
      detail: "full"
  - researcher.synthesize_topic:
      topic: "{step.1.title}"
      detail: "full"
  - researcher.find_relevant:
      query: "multi-agent orchestration event bus distributed coordination"
  - developer.evaluate_with_evidence:  # MS-02
      doc: "{step.1}"
      synergize_output: "{step.3}"
      synthesis: "{step.4}"
      relevant_papers: "{step.5}"
      method: "best_of_3"
output:
  doc_path: "{path}"
  concepts_found: "{step.3.concept_count}"
  frs_generated: "{step.3.fr_count}"
  papers_referenced: "{step.4.paper_count}"
  review: "{step.6}"
```

**Session example:**
- Input: `specs/bus-mcp/architecture.md`
- Output: 7 concepts, 12 FRs from synergize. 5-paper synthesis on the topic. Formal review identifying what's supported (everything-is-an-agent abstraction, session management), what's challenged (static flow declarations, agents-without-containers operational cost), and what the papers add (A2A compatibility, hierarchical agents, proactive agents, bus queue semantics).

**Diagnostic finding:** synergize generates FRs from paper-backed concepts
but not from internal design doc concepts (RemoteRole, BusAgent, flow
orchestration). The spec evaluation pipeline (developer MS-02) is the
right tool for internal-doc assessment. Synergize is for external-evidence
discovery. These are complementary, not competing.

---

## Use Case 4: What Should We Build Next

**Observed session pattern:** User asks "how are we for FRs." Claude checks
the FR list, identifies which are done, which are in progress, and which
are ready to work on. Then presents the prioritized backlog.

**Today's execution:** 2 tool calls (`feature_requests`, `next_fr`).

```yaml
name: whats_next
trigger: "what should we build next for {project}"
agents: [researcher]
bus_features_required: [request_reply]
steps:
  - researcher.feature_requests:
      target: "{project}"
      detail: "brief"
  - researcher.next_fr:
      target: "{project}"
      detail: "brief"
output:
  total_frs: "{step.1.count}"
  fr_list: "{step.1}"
  next: "{step.2}"  # highest priority unblocked FR with details
```

**Session example:**
- Input: target=researcher
- Output: 1 researcher FR (in_progress — the FR refocus work). Led directly to implementation.

**Extended variant observed:** after checking FRs, the session flowed into
implementation → commit → PR → review → merge → status update. This is
the full development cycle that `developer.dispatch_work` (MS-05) would
orchestrate:

```yaml
name: work_cycle
trigger: "let's work on the next FR for {project}"
agents: [researcher, developer]
bus_features_required: [request_reply, cross_agent_calls, session]
steps:
  - researcher.next_fr:
      target: "{project}"
  - developer.create_worktree:  # MS-04
      fr_id: "{step.1.id}"
  - developer.dispatch_work:  # MS-05
      fr_id: "{step.1.id}"
      worktree: "{step.2.path}"
      # Claude receives this as a ready-to-execute briefing
  - # Claude implements (this step is Claude, not bus)
  - researcher.update_fr_status:
      fr_id: "{step.1.id}"
      status: "completed"
output:
  fr_completed: "{step.1.id}"
  worktree: "{step.2.path}"
```

---

## Use Case 5: Batch Paper Ingestion

**Observed session pattern:** User pastes 6 URLs. Claude fetches them all
in parallel, distills, and reports which succeeded and which failed.

**Today's execution:** 6 parallel `fetch_paper` calls + 1 `start_distillation`.

```yaml
name: ingest_papers
trigger: "ingest these: {urls}"
agents: [researcher]
bus_features_required: [request_reply]
steps:
  - researcher.fetch_papers_batch:
      urls: "{urls}"  # comma-separated
  - researcher.start_distillation: {}
output:
  ingested: "{step.1.ingested_count}"
  failed: "{step.1.failed_count}"
  distilled: "{step.2.processed}"
```

**Session example:**
- Input: 6 URLs (Microsoft, Rasa, Openlayer, Yutori, 2 arxiv papers)
- Output: 5 ingested (1 JS-rendered site failed), 11 distilled (including backlog from earlier research runs)

**Variant:** when a fetch fails (JS-rendered site), user pasted the article
text directly. Claude used `ingest_idea` as a fallback ingestion path for
raw text that doesn't have a fetchable URL. The workflow engine should
support this fallback pattern:

```yaml
name: ingest_papers_with_fallback
steps:
  - researcher.fetch_paper:
      url: "{url}"
  - on_failure:
      - researcher.ingest_idea:
          text: "{user_provided_text}"
          source_label: "{url}"
```

This is the first workflow that requires **conditional branching** — a step
that runs only if a prior step fails. The flow engine needs `on_failure`
or equivalent, not just linear sequencing.

---

## Use Case 6: Promote an Idea to an FR

**Observed session pattern:** During conversation, a concept crystallizes
(e.g., "developer MCP server", "khonliang-bus"). Claude captures it as
an idea, optionally researches it, then promotes it to a formal FR with
full description and acceptance criteria.

**Today's execution:** `ingest_idea` → optional `research_idea` + `brief_idea`
→ `promote_fr` with a comprehensive description. 3-5 tool calls.

```yaml
name: idea_to_fr
trigger: "capture this as an FR for {target}"
agents: [researcher]
bus_features_required: [request_reply]
steps:
  - researcher.ingest_idea:
      text: "{concept_description}"
      source_label: "conversation"
  - researcher.research_idea:  # optional
      idea_id: "{step.1.id}"
  - researcher.brief_idea:  # optional
      idea_id: "{step.1.id}"
  - researcher.promote_fr:
      target: "{target}"
      title: "{title}"
      description: "{description}"
      priority: "{priority}"
      concept: "{step.1.title}"
      classification: "{classification}"
      backing_papers: "{step.2.papers}"  # if research was run
output:
  fr_id: "{step.4.id}"
  backed_by: "{step.2.papers_found} papers"
  claim_assessment: "{step.3}"
```

**Session examples:**
- khonliang-bus FR (`fr_khonliang_03f461fa`) — promoted with full architecture description
- developer FR (`fr_developer_28a11ce2`) — promoted with comprehensive spec
- 3 researcher-lib precondition FRs — promoted as a batch

---

## Use Case 7: Review a PR

**Observed session pattern:** User says "check PR." Claude fetches the review
comments via GitHub API, categorizes by severity, addresses each one with
code changes, commits, pushes, and requests re-review.

**Today's execution:** `gh api` calls for PR comments → read affected files →
edit fixes → commit → push → comment requesting re-review. ~20 tool calls
per PR, 3 PRs addressed in parallel.

```yaml
name: address_pr_review
trigger: "check PR {number}"
agents: [developer]  # future: developer handles this
bus_features_required: [request_reply, session]
steps:
  - developer.fetch_pr_review:  # MS-03+
      repo: "{repo}"
      pr_number: "{number}"
  - developer.categorize_comments:
      comments: "{step.1}"
      # returns: required_fixes, optional_polish
  - developer.generate_fixes:  # for each required fix
      comment: "{fix}"
      affected_file: "{file}"
      method: "best_of_3"
  - developer.apply_and_test:
      fixes: "{step.3}"
  - developer.commit_and_push:
      branch: "{step.1.branch}"
      message: "Address review feedback"
  - developer.request_rereview:
      pr_number: "{number}"
      summary: "{step.2.summary}"
output:
  fixes_applied: "{step.3.count}"
  tests_passed: "{step.4.success}"
  pushed: "{step.5.sha}"
```

**Session example:**
- 3 PRs reviewed in sequence (lib #4: 7 comments, researcher #7: 4 comments, researcher #6: 6 comments)
- All comments addressed, tests re-run, pushed, Copilot re-review requested
- Copilot confirmed all fixes on all 3 PRs

**Note:** Today Claude did all the reasoning (categorize, generate fix, test).
With the developer agent handling categorization and fix generation via
local LLMs, Claude would only review the proposed fixes and approve/reject.

---

## Use Case 8: Spec → Milestone → Implementation Cycle

**Observed session pattern:** The full lifecycle we ran today for MS-01:
spec draft → spec review → spec revision → milestone draft → milestone
review → milestone revision → code implementation → code review → fix →
merge → FR status update.

**Today's execution:** ~100+ tool calls across the full session. Most of
the cost was Claude reasoning and composing reviews/code.

```yaml
name: full_development_cycle
trigger: "let's build {fr_id}"
agents: [researcher, developer]
bus_features_required: [request_reply, cross_agent_calls, session, flow_orchestration]
steps:
  # Phase 1: Spec
  - researcher.next_fr:
      target: "{project}"
  - developer.read_spec:
      path: "{spec_path}"
  - workflow.evaluate_spec:  # nested workflow (use case 2)
      path: "{spec_path}"
  # ... spec revision cycle (human-in-loop)

  # Phase 2: Milestone
  - developer.read_milestone:
      path: "{milestone_path}"
  - workflow.evaluate_spec:  # reuse for milestone review
      path: "{milestone_path}"
  # ... milestone revision cycle (human-in-loop)

  # Phase 3: Implementation
  - developer.create_worktree:
      fr_id: "{step.1.id}"
  - developer.dispatch_work:
      fr_id: "{step.1.id}"
      spec: "{step.2}"
      milestone: "{step.4}"
      evidence: "{step.3.review}"
  # ... Claude implements (human-in-loop)

  # Phase 4: Review + Merge
  - workflow.address_pr_review:  # nested workflow (use case 7)
      pr_number: "{pr}"
  - researcher.update_fr_status:
      fr_id: "{step.1.id}"
      status: "completed"
output:
  fr_completed: "{step.1.id}"
  spec_reviewed: true
  milestone_reviewed: true
  pr_merged: true
```

**This is the most complex workflow observed.** It nests other workflows
(evaluate_spec, address_pr_review), has multiple human-in-loop breakpoints,
and spans hours. The bus session mechanism is essential here — the session
accumulates context across all phases so later steps have access to earlier
decisions without re-fetching everything.

---

## Bus Feature Requirements by Use Case

| Use Case | request/reply | cross_agent | session | flow_engine | conditional | nested_flows |
|---|---|---|---|---|---|---|
| 1. Research concept | ✓ | | | | | |
| 2. Evaluate spec | ✓ | ✓ | | | | |
| 3. Evaluate architecture | ✓ | ✓ | ✓ | | | |
| 4. What's next | ✓ | | | | | |
| 5. Batch paper ingestion | ✓ | | | | ✓ (on_failure) | |
| 6. Idea → FR | ✓ | | | | | |
| 7. PR review | ✓ | | ✓ | | | |
| 8. Full dev cycle | ✓ | ✓ | ✓ | ✓ | | ✓ |

**Observations from the matrix:**

1. **request/reply is universal** — every use case needs it. Confirms it's the right Step 1.
2. **4 of 8 use cases work with just request/reply** (1, 4, 5, 6). Step 1 alone delivers half the value.
3. **Cross-agent calls (2 agents coordinating) are needed for 3 use cases** (2, 3, 8). Step 3 (BusAgent adapter) unblocks these.
4. **Sessions are needed for 3 use cases** (3, 7, 8) — the ones that span multiple interactions or accumulate context. Step 5.
5. **The full flow engine + nested flows is only needed for use case 8** — the most complex workflow. Step 6. Don't build this until the simpler use cases are working.
6. **Conditional branching (on_failure) appears in use case 5.** Simple enough to add alongside the flow engine, but worth noting as a requirement.

**This matrix should drive the migration step priorities.** Steps 1-3 cover 7 of 8 use cases. Steps 4-6 are needed for the full development cycle (use case 8) — which is the highest-value but also the most complex workflow.

---

## Workflow Patterns Observed

### Pattern: Background + Notify
Used in use cases 1, 3, 5. Long-running research tasks are kicked off
in the background; the caller (Claude or another agent) is notified when
complete. Today this was done via Claude's Agent tool with `run_in_background`.
The bus equivalent is a pub/sub notification on a `workflow.completed` topic.

### Pattern: Parallel Fan-Out
Used in use cases 1 (4 research agents in parallel), 5 (6 fetch_paper
calls in parallel). Independent subtasks run concurrently. The bus flow
engine needs a `parallel` step type that fans out and waits for all to
complete before proceeding.

### Pattern: Human-in-Loop Breakpoint
Used in use cases 2, 3, 7, 8. The workflow pauses for human review/decision
before continuing. The bus session suspends, preserving context; resumes
when the human (via Claude) sends a continue signal. This is the session
suspend/resume mechanism from the architecture doc.

### Pattern: Nested Workflow
Used in use case 8. A workflow step is itself a complete workflow (e.g.,
`workflow.evaluate_spec` inside the full dev cycle). The bus flow engine
needs to support workflow-as-step composition. Each nested workflow gets
its own sub-session but shares the parent session's public context.

### Pattern: Fallback on Failure
Used in use case 5. A step fails (JS-rendered page can't be fetched);
an alternative step runs instead (user pastes text, ingested via
`ingest_idea`). The flow engine needs `on_failure` handlers per step.

### Pattern: DAG (Non-Linear Step References)
Used in use case 2. Step 4 (`evaluate_with_evidence`) takes output from
steps 1 AND 3, not just the previous step. The flow engine must support
arbitrary step references (`{step.N.field}`), making flows directed
acyclic graphs rather than linear pipelines. Microsoft and Rasa both
found that DAG-capable orchestration outperforms linear pipelines for
real-world agent tasks.
