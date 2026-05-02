"""Tests for developer-owned milestone storage."""

from __future__ import annotations

import pytest

from developer.milestone_store import (
    MILESTONE_STATUS_ABANDONED,
    MILESTONE_STATUS_ARCHIVED,
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


def test_list_status_abandoned_includes_legacy_archived(pipeline):
    """``list(status='abandoned')`` must surface legacy archived rows.

    ``archived`` is the legacy synonym for ``abandoned``; treating the
    filter as literal-match makes legacy on-disk rows invisible to the
    canonical filter. Mirror ``update_status``' write-time normalization
    on the read path so the filter input and stored value don't drift.
    The test plants one ``archived`` row (simulating a legacy milestone
    that pre-dates the ``abandoned`` canonical value) plus one
    ``abandoned`` row, and asserts both surface under
    ``status='abandoned'``. PR #43 Copilot R8 finding 3.
    """
    legacy = pipeline.milestones.propose_from_work_unit(_work_unit())
    # Write-bypass the normalization in update_status to simulate a
    # legacy on-disk row stored with the pre-normalization value.
    legacy.status = MILESTONE_STATUS_ARCHIVED
    pipeline.milestones._store(legacy)

    fresh = pipeline.milestones.propose_from_work_unit(_second_work_unit())
    pipeline.milestones.update_status(
        fresh.id, MILESTONE_STATUS_ABANDONED, notes="wound down",
    )

    abandoned_ids = {
        ms.id
        for ms in pipeline.milestones.list(
            status=MILESTONE_STATUS_ABANDONED, include_archived=True,
        )
    }
    assert legacy.id in abandoned_ids
    assert fresh.id in abandoned_ids

    # Symmetric: ``status='archived'`` returns the same set — the two
    # filter inputs are interchangeable for the legacy alias.
    archived_ids = {
        ms.id
        for ms in pipeline.milestones.list(
            status=MILESTONE_STATUS_ARCHIVED, include_archived=True,
        )
    }
    assert archived_ids == abandoned_ids

    # ``status='abandoned'`` without include_archived still returns
    # both rows — the explicit filter request overrides the default
    # ``hide archived`` flag so the two don't silently contradict.
    default_flag_ids = {
        ms.id
        for ms in pipeline.milestones.list(status=MILESTONE_STATUS_ABANDONED)
    }
    assert legacy.id in default_flag_ids
    assert fresh.id in default_flag_ids


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


def test_propose_from_work_unit_inlines_full_description_in_draft_spec(pipeline):
    """Store-level coverage for the ``full_description`` rendering path
    in ``_draft_spec``. Agent-level enrichment (via
    ``_enrich_work_unit_with_fr_descriptions``) is what populates this
    field in production, but the store accepts pre-populated
    ``full_description`` directly so any future caller (a different
    agent, a migration script, a test harness) gets the same render
    treatment without going through the agent. PR #64 review pass-4
    finding 3.
    """
    work_unit = _work_unit()
    work_unit["frs"][0]["full_description"] = (
        "Multi-paragraph design intent inlined directly at store level.\n"
        "\n"
        "Second paragraph: store-tier callers shouldn't need to route\n"
        "through the agent just to get this rendering."
    )

    milestone = pipeline.milestones.propose_from_work_unit(work_unit)

    draft = milestone.draft_spec
    # First FR's full_description rendered as an indented sub-block
    for substring in [
        "Multi-paragraph design intent inlined directly at store level.",
        "Second paragraph: store-tier callers shouldn't need to route",
        "through the agent just to get this rendering.",
    ]:
        assert f"    {substring}" in draft, (
            f"draft_spec missing inlined description line {substring!r}\n"
            f"---draft---\n{draft}"
        )
    # Second FR (no full_description) renders as the bare bullet only
    assert "fr_developer_22222222" in draft


def test_propose_from_work_unit_preserves_persisted_work_unit_shape(pipeline):
    """The persisted ``work_unit`` (returned via the milestone payload
    and round-tripped by callers) must match the original argument
    shape — bare-string fr items stay bare strings, dict items keep
    their original keys without spurious additions. PR #64 review
    pass-4 finding 1.

    The ``draft_spec_work_unit`` knob exists precisely so the agent
    can pass an enriched render-only view without mutating the
    persisted shape; this test exercises that contract from the
    store's perspective.
    """
    original = {
        "name": "preserve work_unit shape",
        "targets": ["developer"],
        "frs": [
            "fr_developer_aaaaaaaa",  # bare string — must persist as-is
            {
                "fr_id": "fr_developer_bbbbbbbb",
                "description": "second fr",
                "priority": "high",
            },
        ],
    }
    # Simulate an enriched render view (what the agent would build):
    # bare string coerced into a dict, dict gets full_description.
    rendered = {
        **original,
        "frs": [
            {"fr_id": "fr_developer_aaaaaaaa", "full_description": "AAAA description"},
            {
                "fr_id": "fr_developer_bbbbbbbb",
                "description": "second fr",
                "priority": "high",
                "full_description": "BBBB description",
            },
        ],
    }

    milestone = pipeline.milestones.propose_from_work_unit(
        original, draft_spec_work_unit=rendered
    )

    # draft_spec uses the enriched view (full_descriptions appear)
    assert "AAAA description" in milestone.draft_spec
    assert "BBBB description" in milestone.draft_spec
    # …but the persisted work_unit matches the *original* argument
    persisted_frs = milestone.work_unit["frs"]
    assert persisted_frs[0] == "fr_developer_aaaaaaaa", (
        f"bare-string fr was mutated on persistence: {persisted_frs[0]!r}"
    )
    assert persisted_frs[1] == {
        "fr_id": "fr_developer_bbbbbbbb",
        "description": "second fr",
        "priority": "high",
    }, f"dict-shape fr gained spurious keys on persistence: {persisted_frs[1]!r}"


def test_propose_from_work_unit_default_draft_spec_view_falls_back_to_work_unit(pipeline):
    """``draft_spec_work_unit=None`` (the default) must keep the
    pre-existing behavior: ``_draft_spec`` is rendered from the same
    ``work_unit`` dict that gets persisted. Backward-compat guard for
    every store-level caller that doesn't pass the new kwarg.
    """
    work_unit = _work_unit()
    work_unit["frs"][0]["full_description"] = "FROM_WORK_UNIT_DIRECT"

    milestone = pipeline.milestones.propose_from_work_unit(work_unit)

    # full_description from work_unit appears in draft_spec
    assert "FROM_WORK_UNIT_DIRECT" in milestone.draft_spec
    # …and the persisted work_unit still carries it (no shape change)
    assert milestone.work_unit["frs"][0]["full_description"] == "FROM_WORK_UNIT_DIRECT"


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
    assert rolled_back.notes_history[-1].get("force_override") is True
    assert rolled_back.notes_history[-1]["from_status"] == MILESTONE_STATUS_COMPLETED


def test_supersede_milestone_sets_one_way_pointer_on_superseded(pipeline):
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


def test_delete_refusal_message_reports_original_fr_id_on_redirect(pipeline):
    """Delete refusal must surface the bundled fr_id, not just the resolved id.

    When an FR in ``milestone.fr_ids`` has been merge-redirected to a
    different FR, ``fr_store.get(fr_id)`` returns the terminal FR whose
    ``.id`` differs from the bundled id. Reporting only the resolved id
    leaves the caller with an id they cannot find in the milestone's
    bundle and no way to correlate the blocker back to the offending
    bundle entry. The refusal message now includes both sides of the
    redirect so operators can trace the blocker. PR #43 Copilot R8
    finding 1.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())

    # Build a two-source merge so ``fr_x`` becomes a merged-status record
    # whose ``merged_into`` pointer redirects to the newly-created ``fr_y``.
    fr_x = pipeline.frs.promote(
        target="developer",
        title="Legacy A",
        description="source A prior to merge",
        priority="high",
    )
    fr_peer = pipeline.frs.promote(
        target="developer",
        title="Legacy B",
        description="source B prior to merge",
        priority="medium",
    )
    fr_y = pipeline.frs.merge(
        source_ids=[fr_x.id, fr_peer.id],
        title="Unified scope",
        description="post-merge terminal FR",
    )
    pipeline.frs.update_status(fr_y.id, "planned")
    pipeline.frs.update_status(fr_y.id, "in_progress")

    # Rewire the bundle so it carries the ORIGINAL (merged) id. Reading
    # it through fr_store.get() will resolve to fr_y; the error message
    # must still name fr_x so the caller can find it in fr_ids.
    milestone.fr_ids = [fr_x.id]
    pipeline.milestones._store(milestone)

    with pytest.raises(MilestoneError) as excinfo:
        pipeline.milestones.delete(milestone.id, fr_store=pipeline.frs)

    message = str(excinfo.value)
    assert fr_x.id in message, (
        f"error message must contain the original bundled id {fr_x.id!r}, "
        f"got: {message!r}"
    )
    assert fr_y.id in message, (
        f"error message must also surface the resolved id {fr_y.id!r} "
        f"for correlation, got: {message!r}"
    )
    assert "resolved to" in message


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
    assert rolled_back.notes_history[-1].get("force_override") is True


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


def test_update_status_refreshes_draft_spec(pipeline):
    """Status transitions must refresh the cached draft_spec.

    The cached ``draft_spec`` markdown embeds a ``**Status:** ...``
    line; without a recompute on transition, ``draft_spec_from_milestone``
    callers see a stale status. PR #43 Copilot R6 finding 1.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    assert "**Status:** proposed" in milestone.draft_spec

    advanced = pipeline.milestones.update_status(
        milestone.id, MILESTONE_STATUS_IN_PROGRESS, notes="starting",
    )
    assert "**Status:** in_progress" in advanced.draft_spec
    assert "**Status:** proposed" not in advanced.draft_spec

    # Confirm persistence: re-read and the cached draft_spec still reflects
    # the new status (i.e. the refresh wrote through ``_store``).
    reloaded = pipeline.milestones.get(milestone.id)
    assert reloaded is not None
    assert "**Status:** in_progress" in reloaded.draft_spec

    completed = pipeline.milestones.update_status(
        milestone.id, MILESTONE_STATUS_COMPLETED, notes="shipped",
    )
    assert "**Status:** completed" in completed.draft_spec


def test_supersede_refreshes_draft_spec(pipeline):
    """``supersede`` must refresh the cached draft_spec.

    Same invariant as update_status: the embedded ``**Status:**`` line
    would otherwise stay ``proposed`` on a milestone whose status has
    flipped to ``superseded``. PR #43 Copilot R6 finding 1.
    """
    stale = pipeline.milestones.propose_from_work_unit(_work_unit())
    replacement = pipeline.milestones.propose_from_work_unit(_second_work_unit())
    assert "**Status:** proposed" in stale.draft_spec

    superseded = pipeline.milestones.supersede(
        stale.id, replacement.id, rationale="test",
    )
    assert "**Status:** superseded" in superseded.draft_spec

    reloaded = pipeline.milestones.get(stale.id)
    assert reloaded is not None
    assert "**Status:** superseded" in reloaded.draft_spec


def test_update_frs_refreshes_draft_spec_and_work_unit(pipeline):
    """update_frs must sync work_unit["frs"] AND recompute draft_spec.

    Without this, ``review_scope`` and ``draft_spec`` both operate off a
    stale bundle while ``fr_ids`` reports the updated one — classic
    split-brain. PR #43 Copilot R6 finding 2.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    original_ids = list(milestone.fr_ids)
    assert original_ids[0] in milestone.draft_spec
    # Baseline: work_unit["frs"] and fr_ids agree.
    wu_ids_before = [
        (fr.get("fr_id") if isinstance(fr, dict) else str(fr))
        for fr in milestone.work_unit.get("frs") or []
    ]
    assert wu_ids_before == original_ids

    updated = pipeline.milestones.update_frs(
        milestone.id,
        add_fr_ids=["fr_developer_freshadd"],
        remove_fr_ids=[original_ids[0]],
        notes="retarget",
    )

    # fr_ids changed as expected (baseline from earlier tests).
    assert original_ids[0] not in updated.fr_ids
    assert "fr_developer_freshadd" in updated.fr_ids

    # work_unit["frs"] is in lock-step with fr_ids.
    wu_ids_after = [
        (fr.get("fr_id") if isinstance(fr, dict) else str(fr))
        for fr in updated.work_unit.get("frs") or []
    ]
    assert wu_ids_after == updated.fr_ids

    # Surviving entries keep their description/priority metadata.
    surviving = next(
        fr for fr in updated.work_unit["frs"]
        if isinstance(fr, dict) and fr.get("fr_id") == original_ids[1]
    )
    assert surviving.get("priority") == "medium"

    # draft_spec reflects the new bundle: added id present, removed id gone.
    assert "fr_developer_freshadd" in updated.draft_spec
    assert original_ids[0] not in updated.draft_spec

    # Persistence: reload and the cached draft_spec / work_unit are still fresh.
    reloaded = pipeline.milestones.get(milestone.id)
    assert reloaded is not None
    assert "fr_developer_freshadd" in reloaded.draft_spec
    assert original_ids[0] not in reloaded.draft_spec
    reloaded_wu_ids = [
        (fr.get("fr_id") if isinstance(fr, dict) else str(fr))
        for fr in reloaded.work_unit.get("frs") or []
    ]
    assert reloaded_wu_ids == reloaded.fr_ids


def test_update_status_normalizes_archived_to_abandoned(pipeline):
    """``status="archived"`` is normalized to ``abandoned`` on write.

    ``archived`` is the legacy synonym for ``abandoned``; normalizing on
    write gives exactly one terminal-abandon value going forward. The
    audit row records the pre-normalization request via
    ``normalized_from`` so the history still shows what the caller
    asked for. PR #43 Copilot R6 finding 4 (Option A).
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())

    abandoned = pipeline.milestones.update_status(
        milestone.id, MILESTONE_STATUS_ARCHIVED, notes="legacy caller",
    )
    assert abandoned.status == MILESTONE_STATUS_ABANDONED

    audit = abandoned.notes_history[-1]
    assert audit["status"] == MILESTONE_STATUS_ABANDONED
    assert audit["normalized_from"] == MILESTONE_STATUS_ARCHIVED

    # Persistence.
    reloaded = pipeline.milestones.get(milestone.id)
    assert reloaded is not None
    assert reloaded.status == MILESTONE_STATUS_ABANDONED

    # ``list(include_archived=False)`` hides both archived (legacy) and
    # abandoned (new terminal) — they're one category now.
    assert milestone.id not in {m.id for m in pipeline.milestones.list()}
    assert milestone.id in {
        m.id for m in pipeline.milestones.list(include_archived=True)
    }


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
    assert rolled_out.notes_history[-1].get("force_override") is True

    # Confirm persistence too.
    reloaded = pipeline.milestones.get(stale.id)
    assert reloaded.status == MILESTONE_STATUS_IN_PROGRESS
    assert reloaded.superseded_by == ""


def test_update_status_on_legacy_archived_milestone_accepts_abandoned(pipeline):
    """Legacy ``archived`` rows must accept update_status without a migration.

    Before R7, update_status normalized the *incoming* ``archived`` to
    ``abandoned`` but compared it against the milestone's raw stored
    status, which could still be ``archived``. The transition
    ``archived → abandoned`` isn't in ALLOWED_MILESTONE_TRANSITIONS, so
    legacy rows raised ``illegal transition``. R7's fix (Option A)
    treats stored ``archived`` as ``abandoned`` at read time, without
    writing back. PR #43 Copilot R7 finding 2.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    # Manually rewrite the stored status to the legacy ``archived`` value
    # — simulates a row persisted before R6's normalization landed.
    milestone.status = MILESTONE_STATUS_ARCHIVED
    pipeline.milestones._store(milestone)

    reloaded = pipeline.milestones.get(milestone.id)
    assert reloaded.status == MILESTONE_STATUS_ARCHIVED

    # Incoming 'archived' → normalized to 'abandoned' → current (archived)
    # also treated as 'abandoned' → same-status no-op with a normalized_from
    # audit row. Stored status stays archived (no write-back).
    result = pipeline.milestones.update_status(
        milestone.id, MILESTONE_STATUS_ARCHIVED, notes="touch legacy row",
    )
    assert result.status == MILESTONE_STATUS_ARCHIVED
    last = result.notes_history[-1]
    assert last["notes"] == "touch legacy row"
    assert last.get("normalized_from") == MILESTONE_STATUS_ARCHIVED

    # Incoming 'abandoned' — current (archived) aliased to abandoned →
    # also same-status no-op (with the caller's note recorded).
    result = pipeline.milestones.update_status(
        milestone.id, MILESTONE_STATUS_ABANDONED, notes="treat as abandoned",
    )
    assert result.status == MILESTONE_STATUS_ARCHIVED
    assert result.notes_history[-1]["notes"] == "treat as abandoned"

    # Force-override lets callers jump back to a live status even from
    # legacy-archived — the transition table lookup aliases current to
    # abandoned (terminal, no outgoing edges), so any target requires
    # force. The force_override marker is recorded.
    result = pipeline.milestones.update_status(
        milestone.id,
        MILESTONE_STATUS_COMPLETED,
        notes="archived was wrong; mark complete",
        force=True,
    )
    assert result.status == MILESTONE_STATUS_COMPLETED
    last = result.notes_history[-1]
    assert last.get("force_override") is True
    # from_status records the aliased value, not the stored ``archived``,
    # so the audit row reflects the lifecycle-meaningful source state.
    assert last["from_status"] == MILESTONE_STATUS_ABANDONED


def test_audit_flag_named_force_override_not_force_rollback(pipeline):
    """Forced transitions record ``force_override``, not ``force_rollback``.

    Some forced transitions are forward jumps (planned → completed
    skipping in_progress), not rollbacks; "override" is the accurate
    name. PR #43 Copilot R7 finding 3.
    """
    milestone = pipeline.milestones.propose_from_work_unit(_work_unit())
    pipeline.milestones.update_status(milestone.id, "planned")

    # Forward jump that skips in_progress — requires force, not a rollback.
    jumped = pipeline.milestones.update_status(
        milestone.id,
        MILESTONE_STATUS_COMPLETED,
        notes="skip straight to done",
        force=True,
    )
    assert jumped.status == MILESTONE_STATUS_COMPLETED
    last = jumped.notes_history[-1]
    assert last.get("force_override") is True
    assert "force_rollback" not in last



# ---------------------------------------------------------------------------
# Phase 3 of fr_developer_5d0a8711 — project dimension
# ---------------------------------------------------------------------------


def test_milestone_project_defaults_to_khonliang(pipeline):
    from developer.project_store import DEFAULT_PROJECT
    m = pipeline.milestones.propose_from_work_unit(_work_unit())
    assert m.project == DEFAULT_PROJECT
    raw = pipeline.knowledge.get(m.id)
    assert raw.metadata["project"] == DEFAULT_PROJECT


def test_milestone_project_passes_through_propose(pipeline):
    m = pipeline.milestones.propose_from_work_unit(
        _work_unit(), project="sibling-app",
    )
    assert m.project == "sibling-app"
    raw = pipeline.knowledge.get(m.id)
    assert raw.metadata["project"] == "sibling-app"


def test_milestone_list_filters_by_project(pipeline):
    def _wu(name, fr_ids):
        return {
            "name": name,
            "targets": ["developer"],
            "rank": 1,
            "frs": [{"fr_id": fid, "target": "developer", "title": fid} for fid in fr_ids],
        }
    alpha = pipeline.milestones.propose_from_work_unit(
        _wu("A", ["fr_developer_alpha01"]), project="alpha",
    )
    beta = pipeline.milestones.propose_from_work_unit(
        _wu("B", ["fr_developer_beta01"]), project="beta",
    )
    alpha_only = pipeline.milestones.list(project="alpha")
    ids = {m.id for m in alpha_only}
    assert alpha.id in ids
    assert beta.id not in ids


def test_milestone_migrate_records_to_project_is_idempotent(pipeline):
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    # Legacy milestone missing `project`.
    pipeline.knowledge.add(KnowledgeEntry(
        id="ms_developer_legacy01",
        tier=Tier.DERIVED,
        title="Legacy MS",
        content="body",
        source="developer.milestone_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["milestone", "target:developer", "status:proposed"],
        metadata={
            "milestone_status": "proposed",
            "target": "developer",
            "fr_ids": [],
            "work_unit": {},
            "source": "work_unit",
            "rank": 0,
        },
        created_at=now, updated_at=now,
    ))
    # Current entry already has project.
    pipeline.milestones.propose_from_work_unit(_work_unit())

    assert pipeline.milestones.migrate_records_to_project(DEFAULT_PROJECT) == 1
    assert pipeline.milestones.migrate_records_to_project(DEFAULT_PROJECT) == 0
    raw = pipeline.knowledge.get("ms_developer_legacy01")
    assert raw.metadata["project"] == DEFAULT_PROJECT


def test_milestone_propose_project_none_preserves_existing(pipeline):
    first = pipeline.milestones.propose_from_work_unit(_work_unit(), project="alpha")
    assert first.project == "alpha"
    # Re-proposing with project=None MUST preserve "alpha".
    second = pipeline.milestones.propose_from_work_unit(_work_unit())
    assert second.project == "alpha"


def test_milestone_propose_explicit_default_overrides_existing(pipeline):
    from developer.project_store import DEFAULT_PROJECT
    first = pipeline.milestones.propose_from_work_unit(_work_unit(), project="alpha")
    assert first.project == "alpha"
    # Explicit DEFAULT_PROJECT must actually override — this was the
    # Copilot R1 finding: with project=DEFAULT_PROJECT the old default,
    # there was no way to move a milestone back to the default project.
    second = pipeline.milestones.propose_from_work_unit(
        _work_unit(), project=DEFAULT_PROJECT,
    )
    assert second.project == DEFAULT_PROJECT


def test_migrate_preserves_unknown_metadata_keys(pipeline):
    # Legacy record with a metadata key the dataclass doesn't know about.
    # A round-trip-through-serializer migration would drop it; the in-place
    # patch approach must keep it.
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    pipeline.knowledge.add(KnowledgeEntry(
        id="ms_legacy_with_extra",
        tier=Tier.DERIVED,
        title="with extras",
        content="body",
        source="developer.milestone_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["milestone", "target:developer", "status:proposed", "custom:tag"],
        metadata={
            "milestone_status": "proposed",
            "target": "developer",
            "fr_ids": [],
            "work_unit": {},
            "source": "work_unit",
            "rank": 0,
            # Unknown-to-dataclass keys — must survive the migration.
            "legacy_extra": "preserve_me",
            "legacy_number": 42,
        },
        created_at=now, updated_at=now,
    ))
    assert pipeline.milestones.migrate_records_to_project(DEFAULT_PROJECT) == 1
    raw = pipeline.knowledge.get("ms_legacy_with_extra")
    assert raw.metadata["project"] == DEFAULT_PROJECT
    assert raw.metadata["legacy_extra"] == "preserve_me"
    assert raw.metadata["legacy_number"] == 42
    # Tags also preserved (including the custom one).
    assert "custom:tag" in raw.tags


def test_milestone_legacy_record_reads_as_default_project(pipeline):
    # Legacy milestone written without a `project` key in metadata — reader
    # must surface it as DEFAULT_PROJECT without requiring the migration
    # helper to run first.
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    pipeline.knowledge.add(KnowledgeEntry(
        id="ms_legacy_read",
        tier=Tier.DERIVED,
        title="legacy readback",
        content="body",
        source="developer.milestone_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["milestone", "target:developer", "status:proposed"],
        metadata={
            "milestone_status": "proposed",
            "target": "developer",
            "fr_ids": [],
            "work_unit": {},
            "source": "work_unit",
            "rank": 0,
        },
        created_at=now, updated_at=now,
    ))
    ms = pipeline.milestones.get("ms_legacy_read")
    assert ms is not None
    assert ms.project == DEFAULT_PROJECT
    # list() also surfaces it.
    assert any(m.id == "ms_legacy_read" and m.project == DEFAULT_PROJECT
               for m in pipeline.milestones.list())


def test_milestone_list_empty_string_project_filters_for_default(pipeline):
    default_ms = pipeline.milestones.propose_from_work_unit(_work_unit())
    # Make a second milestone with a distinct fr_ids set so it gets a
    # separate id, and pin it to a non-default project.
    def _wu(fr_id):
        return {
            "name": f"WU for {fr_id}",
            "targets": ["developer"],
            "rank": 1,
            "frs": [{"fr_id": fr_id, "target": "developer", "title": fr_id}],
        }
    alpha_ms = pipeline.milestones.propose_from_work_unit(
        _wu("fr_developer_alphafilter"), project="alpha",
    )
    filtered = pipeline.milestones.list(project="")
    ids = {m.id for m in filtered}
    assert default_ms.id in ids
    assert alpha_ms.id not in ids
