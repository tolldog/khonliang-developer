"""Developer-researcher: evidence subset for the developer agent.

Extends BaseResearchAgent with no additional skills — just filters the
base set to the ~13 skills developer needs for evidence gathering,
synthesis, and concept bundling. No code scanning, no pipeline management,
no FR lifecycle tools — those belong in developer itself.

Lives in the developer repo because developer owns what evidence skills
it needs. Changes to the filter don't require a PR to the researcher repo.

Usage::

    python -m developer.researcher_agent --id developer-researcher --bus http://localhost:8787 --config config.yaml
"""

from __future__ import annotations

import asyncio
import logging
import sys

from khonliang_bus import Skill
from khonliang_researcher import BaseResearchAgent, DomainConfig

logger = logging.getLogger(__name__)

# Developer-researcher exposes only the skills developer needs.
# Everything else stays in the full researcher agent.
INCLUDED_SKILLS = {
    # Evidence
    "find_relevant",
    "paper_context",
    "score_relevance",

    # Synthesis
    "synthesize_topic",
    "synthesize_project",

    # Concept bundling (not FR generation)
    "synergize_concepts",

    # Ideas pipeline
    "ingest_idea",
    "research_idea",
    "brief_idea",

    # Exploration
    "knowledge_search",
    "concepts_for_project",

    # On-demand ingestion (single items, not pipeline)
    "fetch_paper",
    "ingest_file",
}
# Note: health_check is intentionally NOT in this set. It belongs in bus-lib
# as a built-in skill on BaseAgent (tracked separately) so every agent gets
# it for free without per-subclass whitelisting.


class DeveloperResearcher(BaseResearchAgent):
    """Evidence-focused researcher for the developer domain.

    Inherits all generic research skills from BaseResearchAgent,
    then filters to the subset developer actually needs.
    """

    agent_type = "developer-researcher"
    module_name = "developer.researcher_agent"

    domain = DomainConfig(
        name="platform-development",
        engines=["web_search"],
        relevance_keywords=[
            "multi-agent", "MCP", "orchestration", "bus",
            "RAG", "LLM agent", "distributed agent",
        ],
    )

    def register_skills(self) -> list[Skill]:
        """Filter base skills to the evidence subset.

        Fails fast at startup if `INCLUDED_SKILLS` references a name the
        base agent no longer provides — a silent drop would leave the
        agent partially functional with no obvious signal.
        """
        all_skills = super().register_skills()
        available = {s.name for s in all_skills}
        missing = INCLUDED_SKILLS - available
        if missing:
            raise RuntimeError(
                "developer-researcher INCLUDED_SKILLS references skills "
                f"not provided by BaseResearchAgent: {sorted(missing)}. "
                "Either the base renamed/removed a skill, or the filter "
                "has a typo. Fix the filter to re-sync."
            )
        filtered = [s for s in all_skills if s.name in INCLUDED_SKILLS]
        logger.info(
            "developer-researcher: %d/%d skills (filtered to evidence subset)",
            len(filtered),
            len(all_skills),
        )
        return filtered


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Developer-researcher bus agent (evidence subset)"
    )
    parser.add_argument("command", nargs="?", choices=["install", "uninstall"])
    parser.add_argument("--id", default="developer-researcher")
    parser.add_argument("--bus", default="http://localhost:8787")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.command in ("install", "uninstall"):
        from khonliang_bus import BaseAgent
        BaseAgent.from_cli([
            args.command,
            "--id", args.id,
            "--bus", args.bus,
            "--config", args.config,
        ])
        return

    agent = DeveloperResearcher(
        agent_id=args.id,
        bus_url=args.bus,
        config_path=args.config,
    )
    asyncio.run(agent.start())


if __name__ == "__main__":
    main()
