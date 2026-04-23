"""Tests for developer-owned milestone storage."""

from __future__ import annotations

import pytest

from developer.milestone_store import (
    MILESTONE_STATUS_ABANDONED,
    MILESTONE_STATUS_COMPLETED,
    MILESTONE_STATUS_IN_PROGRESS,
    MILESTONE_STATUS_PROPOSED,
    MILESTONE_STATUS_SUPERSEDED,
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


# ---------------------------------------------------------------------------
# Lifecycle mutations (fr_developer_91a5a072)
# ---------------------------------------------------------------------------


def _second_work_unit():
    """A second, distinct work unit — used as the superseder target."""
    return {
        "name": "Cluster 2 (replacement)",
        "rank": 2,
        "size": 1,
        "targets": ["developer"],
        "max_priority": "high",
        "frs": [
            {
                "fr_id": "fr_developer_replace1",
                "description": "Replacement scope -> developer [high]",
                "priority": "high",
            },
        ],
    }


def test_update_milestone_status_transitions(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())

    advanced = pipeline.milestones.update_status(
        milestone.id, MILESTONE_STATUS_IN_PROGRESS, notes="starting work",
    )
    assert advanced.status == MILESTONE_STATUS_IN_PROGRESS

    completed = pipeline.milestones.update_status(
        milestone.id, MILESTONE_STATUS_COMPLETED, notes="shipped",
    )
    assert completed.status == MILESTONE_STATUS_COMPLETED

    # Audit trail: seed + two transitions.
    statuses = [entry["status"] for entry in completed.notes_history]
    assert statuses == [
        MILESTONE_STATUS_PROPOSED,
        MILESTONE_STATUS_IN_PROGRESS,
        MILESTONE_STATUS_COMPLETED,
    ]
    assert completed.notes_history[-1]["notes"] == "shipped"
    assert completed.notes_history[-1]["from_status"] == MILESTONE_STATUS_IN_PROGRESS


def test_update_milestone_status_rejects_rollback_from_terminal(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    pipeline.milestones.update_status(milestone.id, MILESTONE_STATUS_IN_PROGRESS)
    pipeline.milestones.update_status(milestone.id, MILESTONE_STATUS_COMPLETED)

    with pytest.raises(MilestoneError, match="illegal transition"):
        pipeline.milestones.update_status(
            milestone.id, MILESTONE_STATUS_IN_PROGRESS,
        )


def test_update_milestone_status_force_allows_rollback(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    pipeline.milestones.update_status(milestone.id, MILESTONE_STATUS_IN_PROGRESS)
    pipeline.milestones.update_status(milestone.id, MILESTONE_STATUS_COMPLETED)

    rolled_back = pipeline.milestones.update_status(
        milestone.id,
        MILESTONE_STATUS_IN_PROGRESS,
        notes="accidental completion; reopening",
        force=True,
    )
    assert rolled_back.status == MILESTONE_STATUS_IN_PROGRESS
    assert rolled_back.notes_history[-1].get("force_rollback") is True
    assert rolled_back.notes_history[-1]["from_status"] == MILESTONE_STATUS_COMPLETED


def test_supersede_milestone_sets_pointer_bidirectionally(pipeline):
    stale = pipeline.milestones.propose_from_work_unit(_work_unit())
    replacement = pipeline.milestones.propose_from_work_unit(_second_work_unit())

    superseded = pipeline.milestones.supersede(
        stale.id, replacement.id, rationale="auto-selection bug",
    )
    assert superseded.status == MILESTONE_STATUS_SUPERSEDED
    assert superseded.superseded_by == replacement.id
    assert superseded.notes_history[-1]["superseded_by"] == replacement.id
    assert "auto-selection bug" in superseded.notes_history[-1]["notes"]

    # Re-fetch to confirm persistence.
    reloaded = pipeline.milestones.get(stale.id)
    assert reloaded.status == MILESTONE_STATUS_SUPERSEDED
    assert reloaded.superseded_by == replacement.id

    # Superseder is unaffected — no cascade.
    reloaded_replacement = pipeline.milestones.get(replacement.id)
    assert reloaded_replacement.status == MILESTONE_STATUS_PROPOSED
    assert reloaded_replacement.superseded_by == ""


def test_delete_milestone_refuses_if_fr_in_progress(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    fr = pipeline.frs.promote(
        target="developer",
        title="Active work",
        description="In-progress work inside milestone bundle",
        priority="high",
    )
    pipeline.frs.update_status(fr.id, "planned")
    pipeline.frs.update_status(fr.id, "in_progress")
    # Rewire the milestone bundle to include the live FR so the guard fires.
    milestone.fr_ids = [fr.id]
    pipeline.milestones._store(milestone)

    with pytest.raises(MilestoneError, match="in progress"):
        pipeline.milestones.delete(milestone.id, fr_store=pipeline.frs)


def test_delete_milestone_refuses_if_notes_history_nontrivial(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    pipeline.milestones.update_status(
        milestone.id, MILESTONE_STATUS_IN_PROGRESS, notes="started",
    )

    with pytest.raises(MilestoneError, match="supersede"):
        pipeline.milestones.delete(milestone.id, fr_store=pipeline.frs)


def test_delete_milestone_allows_clean_milestone(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())

    result = pipeline.milestones.delete(
        milestone.id, reason="test cleanup", fr_store=pipeline.frs,
    )
    assert result["removed"] is True
    assert result["reason"] == "test cleanup"
    assert pipeline.milestones.get(milestone.id) is None


def test_delete_milestone_requires_fr_store_parameter(pipeline):
    """fr_store is now a required keyword arg — calling without it must raise.

    Previously fr_store defaulted to None and the in-progress guard was
    silently skipped with a ``skipped_fr_check`` marker. REPL / direct-
    pipeline callers that forgot to wire it got a hard-delete that
    bypassed the "refuse if bundled FR in_progress" safety promise.
    Making fr_store required turns that footgun into a TypeError at the
    call site. PR #43 Copilot R5 finding 1.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())

    # No fr_store kwarg → TypeError from the function signature.
    with pytest.raises(TypeError, match="fr_store"):
        pipeline.milestones.delete(milestone.id)

    # Explicit fr_store=None is also rejected — the parameter is
    # required, there is no opt-in to skip the check.
    with pytest.raises(TypeError, match="fr_store"):
        pipeline.milestones.delete(milestone.id, fr_store=None)  # type: ignore[arg-type]


def test_update_milestone_frs_add_remove(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    original = list(milestone.fr_ids)

    updated = pipeline.milestones.update_frs(
        milestone.id,
        add_fr_ids=["fr_developer_extra1"],
        remove_fr_ids=[original[0]],
        notes="retargeted bundle",
    )
    assert original[0] not in updated.fr_ids
    assert "fr_developer_extra1" in updated.fr_ids
    # Audit row captures the bundle delta.
    last = updated.notes_history[-1]
    assert last["added_fr_ids"] == ["fr_developer_extra1"]
    assert last["removed_fr_ids"] == [original[0]]
    assert last["notes"] == "retargeted bundle"


def test_update_milestone_frs_refuses_on_non_proposed_milestone(pipeline):
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    pipeline.milestones.update_status(milestone.id, MILESTONE_STATUS_IN_PROGRESS)

    with pytest.raises(MilestoneError, match="only 'proposed' is mutable"):
        pipeline.milestones.update_frs(
            milestone.id, add_fr_ids=["fr_developer_lateadd"],
        )


def test_update_milestone_frs_accepts_single_str_as_one_id(pipeline):
    """Bare-str fr-ids arg must normalize to a one-element list.

    A plain ``str`` is technically ``Iterable[str]`` (of its own
    characters); without early normalization the comprehension would
    iterate ``"fr_developer_solo"`` into 17 bogus single-char fr ids.
    PR #43 Copilot R3 finding 1.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    original = list(milestone.fr_ids)

    # add_fr_ids passed as a bare string → single-id add, not per-char.
    updated = pipeline.milestones.update_frs(
        milestone.id,
        add_fr_ids="fr_developer_solo",
        notes="single-id add via bare str",
    )
    assert "fr_developer_solo" in updated.fr_ids
    # No per-character id leakage.
    assert not any(len(fid) == 1 for fid in updated.fr_ids)
    last = updated.notes_history[-1]
    assert last["added_fr_ids"] == ["fr_developer_solo"]
    assert last["removed_fr_ids"] == []

    # remove_fr_ids passed as a bare string → single-id remove.
    updated2 = pipeline.milestones.update_frs(
        milestone.id,
        remove_fr_ids=original[0],
        notes="single-id remove via bare str",
    )
    assert original[0] not in updated2.fr_ids
    last2 = updated2.notes_history[-1]
    assert last2["removed_fr_ids"] == [original[0]]
    assert last2["added_fr_ids"] == []


def test_update_milestone_status_rejects_backward_transition_without_force(pipeline):
    """Monotonic-forward: planned → proposed requires force.

    Guards against the permissive-table regression where backward
    edges (planned→proposed, in_progress→planned) were allowed
    without force. PR #43 Copilot R1 finding 2.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    pipeline.milestones.update_status(milestone.id, "planned")

    with pytest.raises(MilestoneError, match="illegal transition"):
        pipeline.milestones.update_status(milestone.id, MILESTONE_STATUS_PROPOSED)

    # And in_progress → planned is also a backward edge now.
    pipeline.milestones.update_status(milestone.id, MILESTONE_STATUS_IN_PROGRESS)
    with pytest.raises(MilestoneError, match="illegal transition"):
        pipeline.milestones.update_status(milestone.id, "planned")

    # Force=True permits the rollback and records the audit marker.
    rolled_back = pipeline.milestones.update_status(
        milestone.id, "planned", notes="reopen for rescoping", force=True,
    )
    assert rolled_back.status == "planned"
    assert rolled_back.notes_history[-1].get("force_rollback") is True


def test_delete_milestone_refuses_when_fr_store_lookup_raises(pipeline):
    """fr_store.get() raising must surface as a MilestoneError.

    Previously the exception was silently swallowed, bypassing the
    "refuse deletion when any bundled FR is in_progress" guard. PR #43
    Copilot R1 finding 3.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    # Rewire the bundle to a synthetic id that our broken fr_store will
    # raise on — keeps the test hermetic.
    milestone.fr_ids = ["fr_developer_lookup_boom"]
    pipeline.milestones._store(milestone)

    class BrokenFRStore:
        def get(self, fr_id):
            raise RuntimeError(f"simulated FR store failure for {fr_id}")

    with pytest.raises(MilestoneError, match="failed to verify FR state"):
        pipeline.milestones.delete(milestone.id, fr_store=BrokenFRStore())

    # Milestone is still present — delete was refused, not partially applied.
    assert pipeline.milestones.get(milestone.id) is not None


def test_update_status_rejects_superseded_transition(pipeline):
    """update_status cannot set 'superseded' — the skill has no superseded_by parameter.

    Letting it through produced invalid milestones
    (status=superseded, superseded_by=""). PR #43 Copilot R4 finding 1.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())

    with pytest.raises(MilestoneError, match="supersede_milestone"):
        pipeline.milestones.update_status(milestone.id, MILESTONE_STATUS_SUPERSEDED)

    # Milestone is untouched — rejection happened before any state change.
    reloaded = pipeline.milestones.get(milestone.id)
    assert reloaded.status == MILESTONE_STATUS_PROPOSED
    assert reloaded.superseded_by == ""


def test_update_status_force_out_of_superseded_clears_pointer(pipeline):
    """Force-rolling OUT of 'superseded' must clear the stale superseded_by pointer.

    Without this, force-rollback could leave an ``in_progress`` milestone
    still carrying a superseded_by id that references an unrelated
    replacement. PR #43 Copilot R4 finding 1 (invariant follow-up).
    """
    stale = pipeline.milestones.propose_from_work_unit(_work_unit())
    replacement = pipeline.milestones.propose_from_work_unit(_second_work_unit())

    # Go through the proper skill to land a real superseded milestone.
    superseded = pipeline.milestones.supersede(
        stale.id, replacement.id, rationale="test rollout",
    )
    assert superseded.status == MILESTONE_STATUS_SUPERSEDED
    assert superseded.superseded_by == replacement.id

    # Force-roll back to in_progress — the superseded_by pointer must clear.
    rolled_out = pipeline.milestones.update_status(
        stale.id,
        MILESTONE_STATUS_IN_PROGRESS,
        notes="supersede was wrong, reopening",
        force=True,
    )
    assert rolled_out.status == MILESTONE_STATUS_IN_PROGRESS
    assert rolled_out.superseded_by == ""
    assert rolled_out.notes_history[-1].get("force_rollback") is True

    # Confirm persistence too.
    reloaded = pipeline.milestones.get(stale.id)
    assert reloaded.status == MILESTONE_STATUS_IN_PROGRESS
    assert reloaded.superseded_by == ""
