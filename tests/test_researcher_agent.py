"""Tests for the developer-researcher agent (evidence subset filter)."""

from __future__ import annotations

import pytest

from developer.researcher_agent import DeveloperResearcher, INCLUDED_SKILLS


def _call_register_skills(cls=DeveloperResearcher):
    """Invoke register_skills without running __init__ (avoids bus setup)."""
    return cls.register_skills(cls.__new__(cls))


def test_agent_metadata():
    assert DeveloperResearcher.agent_type == "developer-researcher"
    assert DeveloperResearcher.module_name == "developer.researcher_agent"


def test_domain_config():
    d = DeveloperResearcher.domain
    assert d.name == "platform-development"
    assert "web_search" in d.engines
    # Relevance keywords cover the multi-agent / bus topic area
    assert any("agent" in k.lower() for k in d.relevance_keywords)


def test_filter_returns_only_included_skills():
    skills = _call_register_skills()
    names = {s.name for s in skills}
    assert names == INCLUDED_SKILLS, (
        f"Filter output drifted from INCLUDED_SKILLS. "
        f"Extra: {names - INCLUDED_SKILLS}. Missing: {INCLUDED_SKILLS - names}."
    )


def test_filter_fails_fast_on_unknown_skill(monkeypatch):
    """If INCLUDED_SKILLS references a name the base doesn't provide, raise."""
    from developer import researcher_agent

    bad = INCLUDED_SKILLS | {"nonexistent_skill_xyz"}
    monkeypatch.setattr(researcher_agent, "INCLUDED_SKILLS", bad)

    with pytest.raises(RuntimeError, match="nonexistent_skill_xyz"):
        _call_register_skills()


def test_health_check_not_in_filter():
    """health_check is intentionally excluded here; bus-lib should provide
    it as a BaseAgent built-in so subclasses don't whitelist it."""
    assert "health_check" not in INCLUDED_SKILLS
