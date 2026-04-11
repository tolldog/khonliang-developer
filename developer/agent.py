"""Developer as a bus agent.

Alternative to ``developer.server`` (MCP over stdio). The developer
registers with the bus and Claude talks to the bus.

Usage::

    # Install into the bus
    python -m developer.agent install --id developer-primary --bus http://localhost:8787 --config config.yaml

    # Start (normally done by the bus on boot)
    python -m developer.agent --id developer-primary --bus http://localhost:8787 --config config.yaml

The agent wraps all MCP tools from ``create_developer_server`` as bus
handlers via ``BaseAgent.from_mcp()``. ResearcherClient is wired
through the bus so cross-agent calls (get_fr, paper_context, etc.)
go through the same bus the agent registered with.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from khonliang_bus import BaseAgent

logger = logging.getLogger(__name__)


def create_developer_agent(
    agent_id: str,
    bus_url: str,
    config_path: str,
) -> BaseAgent:
    """Build a developer bus agent wrapping all MCP tools."""
    from developer.config import Config
    from developer.pipeline import Pipeline
    from developer.server import create_developer_server

    config = Config.load(config_path)
    pipeline = Pipeline.from_config(config)
    mcp_server = create_developer_server(pipeline)

    agent = BaseAgent.from_mcp(
        mcp_server,
        agent_type="developer",
        agent_id=agent_id,
        bus_url=bus_url,
        config_path=config_path,
    )

    try:
        from importlib.metadata import version
        agent.version = version("khonliang-developer")
    except Exception:
        agent.version = "0.1.0"

    logger.info("Developer agent %s created", agent_id)

    return agent


def main():
    """CLI entry point for the developer agent."""
    import argparse

    parser = argparse.ArgumentParser(description="khonliang-developer bus agent")
    parser.add_argument("command", nargs="?", choices=["install", "uninstall"])
    parser.add_argument("--id", default="developer-primary")
    parser.add_argument("--bus", default="http://localhost:8787")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.command in ("install", "uninstall"):
        BaseAgent.from_cli([
            args.command,
            "--id", args.id,
            "--bus", args.bus,
            "--config", args.config,
        ])
        return

    agent = create_developer_agent(
        agent_id=args.id,
        bus_url=args.bus,
        config_path=args.config,
    )
    asyncio.run(agent.start())


if __name__ == "__main__":
    main()
