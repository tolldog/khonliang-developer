---
kind: code_review
severity: concern
---

# Chained bash `git` pipelines without absolute-cd are a wrong-cwd trap

**Invariant**: any commit/push composition that runs `git` via raw bash MUST be a single invocation that pins the cwd absolutely (`cd /abs/path && git ...`), or use the `developer.git_pr_commit_push` skill which fails fast on branch mismatch. Chained `git add -A && git commit -m '...' && git push` from an ambient cwd is the trap that landed wrong-content commits direct on `main` (Episode 19, 2026-04-23).

**Bad pattern**:
```bash
# Two worktrees in play; this shell happens to be on main's checkout.
git add -A && git commit -m "fix R4 in PR #44" && git push
# Result: 8 unrelated untracked files captured by -A, message says "PR #44" but
# the commit lands on main, push goes direct to main bypassing PR review.
```

**Bad pattern (LLM-composed)**:
```python
await bash("git add -A && git commit -m 'add y.txt for fr_developer_44fc7dde' && git push")
# Same trap. Subagent doesn't know which cwd this shell is on. The message
# names a feature branch; if cwd is on main, work lands wrong.
```

**Good pattern (preferred — uses the safe primitive)**:
```python
await developer.git_pr_commit_push(
    cwd="/abs/path/to/worktree",
    branch="fr/git-guardrails",          # caller's expectation
    message="add y.txt",
    paths="y.txt",                        # explicit, no wildcard
    set_upstream=True,
)
# Refuses if cwd is not on fr/git-guardrails. Refuses wildcards. Refuses
# protected branches. Returns {commit, push} on success.
```

**Good pattern (when bash is unavoidable)**:
```bash
cd /abs/path/to/worktree && git add y.txt && git commit -m "add y.txt" && git push
# Single invocation, absolute cwd, explicit path, no -A. Still bypasses the
# protected-branch guard — prefer the skill.
```

**Rationale**: Convention-only ("never push to main") is a behavioral guideline; nothing structural prevents violation. Primitive-level enforcement is stronger because it doesn't depend on caller discipline. Wildcard staging (`-A`, `.`) compounds the trap by capturing files the author didn't intend to commit. The `git_pr_commit_push` composite gates every step on a single declared `branch` arg matching the cwd's current branch, so a wrong-cwd composition fails before any side effect. Sourced from fr_developer_44fc7dde / Episode 19 in `project_dogfooding_log.md`.
