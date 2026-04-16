"""Tests for developer-owned milestone storage."""

from __future__ import annotations

import pytest

from developer.milestone_store import (
    MILESTONE_STATUS_IN_PROGRESS,
    MILESTONE_STATUS_PROPOSED,
    MilestoneError,
)


def _work_unit():
    return {
        "name": "Cluster 1 (2 FRs, targets: developer)",
        "rank": 1,
        "size": 2,
        "targets": ["developer"],
        "max_priority": "high",
        "frs": [
            {
                "fr_id": "fr_developer_11111111",
                "description": "Create milestone records -> developer [high]",
                "priority": "high",
            },
            {
                "fr_id": "fr_developer_22222222",
                "description": "Draft specs from milestones -> developer [medium]",
                "priority": "medium",
            },
        ],
    }


def test_propose_from_work_unit_persists_milestone(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())

    assert milestone.id.startswith("ms_developer_")
    assert milestone.target == "developer"
    assert milestone.status == MILESTONE_STATUS_PROPOSED
    assert milestone.fr_ids == ["fr_developer_11111111", "fr_developer_22222222"]
    assert "fr_developer_11111111" in milestone.draft_spec
    assert "[high] [high]" not in milestone.draft_spec
    assert "[medium] [medium]" not in milestone.draft_spec

    loaded = pipeline.milestones.get(milestone.id)
    assert loaded is not None
    assert loaded.to_public_dict() == milestone.to_public_dict()


def test_propose_from_work_unit_is_idempotent_by_target_title_and_frs(pipeline):
    first = pipeline.milestones.propose_from_work_unit(_work_unit())
    second = pipeline.milestones.propose_from_work_unit(_work_unit())

    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


def test_list_filters_by_target_and_status(pipeline):
    pipeline.milestones.propose_from_work_unit(_work_unit())

    assert pipeline.milestones.list(target="developer")
    assert pipeline.milestones.list(target="bus") == []
    assert pipeline.milestones.list(status=MILESTONE_STATUS_PROPOSED)


def test_propose_requires_frs(pipeline):
    with pytest.raises(MilestoneError, match="frs"):
        pipeline.milestones.propose_from_work_unit({"targets": ["developer"], "frs": []})


def test_propose_requires_every_fr_to_have_id(pipeline):
    work_unit = _work_unit()
    work_unit["frs"].append({"description": "missing id", "priority": "low"})

    with pytest.raises(MilestoneError, match="every work_unit fr"):
        pipeline.milestones.propose_from_work_unit(work_unit)


def test_propose_requires_target_when_not_inferred(pipeline):
    work_unit = _work_unit()
    work_unit["targets"] = ["developer", "bus"]
    with pytest.raises(MilestoneError, match="missing or ambiguous targets"):
        pipeline.milestones.propose_from_work_unit(work_unit)

    milestone = pipeline.milestones.propose_from_work_unit(work_unit, target="developer")
    assert milestone.target == "developer"


def test_reproposal_draft_spec_uses_existing_status(pipeline):
    first = pipeline.milestones.propose_from_work_unit(_work_unit())
    first.status = MILESTONE_STATUS_IN_PROGRESS
    pipeline.milestones._store(first)

    second = pipeline.milestones.propose_from_work_unit(_work_unit())

    assert second.status == MILESTONE_STATUS_IN_PROGRESS
    assert "**Status:** in_progress" in second.draft_spec


def test_draft_spec_appends_priority_when_description_omits_it(pipeline):
    work_unit = _work_unit()
    work_unit["frs"][0]["description"] = "Create milestone records"

    milestone = pipeline.milestones.propose_from_work_unit(work_unit)

    assert "Create milestone records [high]" in milestone.draft_spec


def test_review_scope_flags_duplicates_and_review_terms(pipeline):
    work_unit = {
        "name": "Cluster 1",
        "targets": ["developer"],
        "frs": [
            {
                "fr_id": "fr_developer_11111111",
                "description": "Utilize GRA for Developer-Specific Tasks -> developer [high]",
                "priority": "high",
            },
            {
                "fr_id": "fr_developer_22222222",
                "description": "Utilize GRA for Developer-Specific Tasks -> developer [medium]",
                "priority": "medium",
            },
            {
                "fr_id": "fr_developer_33333333",
                "description": "Create AutoGen template -> developer [medium]",
                "priority": "medium",
            },
        ],
    }
    milestone = pipeline.milestones.propose_from_work_unit(work_unit)

    review = pipeline.milestones.review_scope(milestone.id)

    assert review["recommendation"] == "refine_before_implementation"
    assert review["duplicate_groups"][0]["normalized_description"] == (
        "utilize gra for developer-specific tasks"
    )
    assert {hit["term"] for hit in review["review_term_hits"]} == {"AutoGen", "GRA"}


def test_review_scope_ready_when_no_duplicates_or_review_terms(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())

    review = pipeline.milestones.review_scope(milestone.id, review_terms=["AutoGen"])

    assert review["recommendation"] == "ready_for_spec"
    assert review["duplicate_groups"] == []
    assert review["review_term_hits"] == []


def test_review_scope_empty_review_terms_disables_term_hits(pipeline):
    work_unit = _work_unit()
    work_unit["frs"][0]["description"] = "Create AutoGen template"
    milestone = pipeline.milestones.propose_from_work_unit(work_unit)

    review = pipeline.milestones.review_scope(milestone.id, review_terms=[])

    assert review["recommendation"] == "ready_for_spec"
    assert review["review_term_hits"] == []


def test_review_scope_handles_string_fr_items(pipeline):
    work_unit = {
        "name": "String FR cluster",
        "targets": ["developer"],
        "frs": [
            "fr_developer_autogen1",
            "fr_developer_autogen2",
        ],
    }
    milestone = pipeline.milestones.propose_from_work_unit(work_unit)

    review = pipeline.milestones.review_scope(milestone.id, review_terms=["autogen"])

    assert review["fr_count"] == 2
    assert review["review_term_hits"] == [
        {
            "term": "autogen",
            "frs": [
                {
                    "fr_id": "fr_developer_autogen1",
                    "description": "fr_developer_autogen1",
                },
                {
                    "fr_id": "fr_developer_autogen2",
                    "description": "fr_developer_autogen2",
                },
            ],
        }
    ]
