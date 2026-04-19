# Repo Hygiene Audit

Generated: 1776579547.950239
Repo: `/mnt/dev/ttoll/dev/khonliang-developer`

## Summary

- 0 docs drift findings, 25 stale/deprecated findings, 2 proposed actions, 0 applied changes
- Python files: 34
- Test files: 17
- Docs files: 18

## Cleanup Plan

- **review-stale-references** [low] Review stale wording in docs and source comments (`CLAUDE.md`, `config.yaml`, `developer/agent.py`, `developer/repo_hygiene.py`, `developer/specs.py`, `milestones/MS-01/code_review.md`, `milestones/MS-01/milestone.md`, `milestones/MS-01/review.md`, `prompts/developer_guide.md`, `pyproject.toml`, `specs/MS-01/review.md`, `specs/MS-01/spec.md`, `specs/bus-mcp/agents-review.md`, `specs/bus-mcp/agents.md`, `specs/bus-mcp/architecture.md`, `specs/bus-mcp/bus-agent-interaction.md`, `specs/bus-mcp/design.md`, `specs/bus-mcp/review.md`, `specs/bus-mcp/use-cases.md`, `tests/conftest.py`, `tests/test_agent.py`, `tests/test_config.py`, `tests/test_pipeline.py`, `tests/test_repo_hygiene.py`, `tests/test_specs.py`)
  - Stale terms may be historical, but current guidance should not point at retired milestones or runtimes.
- **write-hygiene-artifact** [low] Write compact repo hygiene artifact (`docs/repo-hygiene-audit.md`)
  - Persist the audit so future sessions can resume without rereading raw files.

## Docs Drift

- None found.

## Deprecated Or Stale Paths

- [low] `CLAUDE.md`: Found stale marker 'direct sibling MCP'. Action: review whether this is historical context or current guidance
- [low] `config.yaml`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `developer/agent.py`: Found stale marker 'from_mcp'. Action: review whether this is historical context or current guidance
- [low] `developer/repo_hygiene.py`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `developer/specs.py`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `milestones/MS-01/code_review.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `milestones/MS-01/milestone.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `milestones/MS-01/review.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `prompts/developer_guide.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `pyproject.toml`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `specs/MS-01/review.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `specs/MS-01/spec.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `specs/bus-mcp/agents-review.md`: Found stale marker 'from_mcp'. Action: review whether this is historical context or current guidance
- [low] `specs/bus-mcp/agents.md`: Found stale marker 'from_mcp'. Action: review whether this is historical context or current guidance
- [low] `specs/bus-mcp/architecture.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `specs/bus-mcp/bus-agent-interaction.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `specs/bus-mcp/design.md`: Found stale marker 'from_mcp'. Action: review whether this is historical context or current guidance
- [low] `specs/bus-mcp/review.md`: Found stale marker 'MS-02'. Action: review whether this is historical context or current guidance
- [low] `specs/bus-mcp/use-cases.md`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `tests/conftest.py`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `tests/test_agent.py`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `tests/test_config.py`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `tests/test_pipeline.py`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `tests/test_repo_hygiene.py`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance
- [low] `tests/test_specs.py`: Found stale marker 'MS-01'. Action: review whether this is historical context or current guidance

## Test Plan

- `.venv/bin/python -m pytest -q`
- `.venv/bin/python -m compileall .`
