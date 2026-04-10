# Agent Model

How agents work in the khonliang platform. What they are, how they start,
how they're installed, and how they run.

---

## What Is an Agent

An agent is a persistent service that:
- Has an identity (unique ID, type, version)
- Has skills (operations it can handle)
- Runs as its own process
- Manages its own port (binds to port 0, OS assigns)
- Manages its own state (own DB, own config)
- Reports its own PID for health monitoring
- Registers with the bus on startup ("calls home")
- Sends heartbeats to prove it's alive
- Handles requests the bus routes to it

An agent is NOT:
- An ephemeral function (it persists between calls)
- A thread in the bus process (it's a separate process)
- Started by Claude (it's started by the bus or by an operator)
- Dependent on Claude being connected (it runs whether Claude is there or not)

## Agent Identity

Each running agent has a unique ID that distinguishes it from other
instances — even instances of the same agent type.

```
agent_type: researcher
agent_id:   researcher-primary        ← unique across the platform

agent_type: researcher
agent_id:   researcher-domain         ← same code, different context
```

The agent type determines what code runs. The agent ID determines which
instance it is. Multiple instances of the same type run with different
configs, different DBs, different contexts — but the same skills.

**Why multiple instances:** same agent code can serve different purposes
depending on configuration. A researcher scoped to a domain-specific
corpus and a researcher scoped to the general corpus run the same binary
but produce different results. A specialist agent configured for one
context and one configured for another have different behavior profiles
but identical skill interfaces.

## Agent Lifecycle

### Installation

An agent installs itself into the bus's persistent registry via API call.
Installation tells the bus "I exist, here's how to start me." The bus
stores this in its own DB — no config files to hand-edit.

```bash
$ python -m researcher.agent install \
    --bus http://localhost:8787 \
    --id researcher-primary \
    --config config.yaml
```

Under the hood:

```
POST http://localhost:8787/v1/install
{
  "agent_type": "researcher",
  "id": "researcher-primary",
  "command": "/path/to/.venv/bin/python",
  "args": ["-m", "researcher.agent"],
  "cwd": "/path/to/khonliang-researcher",
  "config": "/path/to/config.yaml"
}
```

The agent captures its own startup details automatically:
- `command` — `sys.executable` (the Python that ran the install)
- `args` — `["-m", cls.module_name]` (the module entry point)
- `cwd` — current working directory at install time
- `config` — resolved absolute path

No guessing. No manual path construction. The agent knows how to
describe itself because it IS itself.

### Startup

The bus starts installed agents on boot. For each installed agent:

```
1. Bus reads installed_agents from its DB
2. Bus runs the command:
     /path/to/.venv/bin/python -m researcher.agent \
       --id researcher-primary \
       --bus http://localhost:8787 \
       --config /path/to/config.yaml
3. Agent process starts
4. Agent binds to port 0 (OS assigns next available port)
5. Agent initializes (loads config, opens DB, builds pipeline)
6. Agent calls home — see §Registration below
```

The bus passes three args: `--id`, `--bus`, `--config`. The agent handles
everything else. The bus doesn't assign ports, doesn't manage the agent's
internal state, doesn't know what the agent does until it registers.

### Registration ("calling home")

Once the agent is ready to handle requests, it registers with the bus:

```
POST http://localhost:8787/v1/register
{
  "id": "researcher-primary",
  "callback": "http://localhost:9247",
  "pid": 48291,
  "version": "0.6.4",
  "skills": [
    { "name": "find_papers", "description": "Search arxiv + semantic scholar", "parameters": {...} },
    { "name": "synergize", "description": "Classify concepts, generate FRs", "parameters": {...} }
  ],
  "collaborations": [...]
}
```

Registration tells the bus:
- **Where I am** — `callback` URL (agent's self-assigned port)
- **Who I am** — `pid` for process-level health checks
- **What I can do** — `skills` and `collaborations`

The bus stores this as runtime state. If the bus restarts, registrations
are rebuilt when agents re-register (the bus re-starts agents from the
installed_agents table, and each agent calls home again).

**Install vs Register:**

| | Install | Register |
|---|---|---|
| When | One-time setup | Every startup |
| Persists | In bus DB forever (until uninstalled) | Runtime only (rebuilt each boot) |
| Contains | How to start the agent | Where the agent is right now |
| Survives bus restart | Yes | No (rebuilt from re-registration) |
| Survives agent restart | Yes | No (agent re-registers on startup) |

An installed agent that isn't registered is one the bus should start.
A registered agent that isn't installed is a self-managed agent that
started independently — both are valid.

### Heartbeat

After registration, the agent sends periodic heartbeats:

```
POST http://localhost:8787/v1/heartbeat
{ "id": "researcher-primary" }
```

The bus tracks last-seen time per agent. If heartbeats stop:

1. **PID check** — `os.kill(pid, 0)` — is the process alive?
   - If PID is dead → agent crashed. Bus can restart immediately
     (no need to wait for heartbeat timeout).
   - If PID is alive → agent is hung or overloaded. Bus waits for
     heartbeat timeout before marking unhealthy.

2. **After timeout** — bus marks agent as unhealthy, removes its skills
   from the routing table, attempts restart from installed_agents entry.

3. **On restart** — agent gets a new port, new PID, re-registers.
   Bus updates its routing table. Existing sessions for that agent
   are resumed if the agent supports session restore.

### Shutdown

Agent deregisters on clean shutdown:

```
POST http://localhost:8787/v1/deregister
{ "id": "researcher-primary" }
```

Bus removes the agent from the runtime registry. The installed_agents
entry stays — the bus can restart it later. A deregistered agent is
"installed but not running."

### Uninstall

Removes the agent entirely:

```bash
$ python -m researcher.agent uninstall \
    --bus http://localhost:8787 \
    --id researcher-primary
```

```
DELETE http://localhost:8787/v1/install/researcher-primary
```

Bus stops the agent (if running), removes the installed_agents entry.
The agent is gone from the platform until re-installed.

## Multiple Instances

Same code, different contexts. Each instance gets:
- Its own agent ID
- Its own config file
- Its own DB (isolated state)
- Its own port (OS-assigned)
- Its own PID
- Its own registration (skills may be identical but the bus treats
  each instance as a separate agent)

```bash
# Install two researchers with different scopes
$ cd /path/to/khonliang-researcher

$ python -m researcher.agent install \
    --bus http://localhost:8787 \
    --id researcher-primary \
    --config config.yaml

$ python -m researcher.agent install \
    --bus http://localhost:8787 \
    --id researcher-domain \
    --config config-domain.yaml
```

The bus sees two agents. Claude sees two sets of skills in the catalog
(namespaced by agent ID). The bus can route by context:

```yaml
# bus routing (optional — agents can also be called directly by ID)
routing:
  find_papers:
    - match: { project: private-app } → researcher-domain
    - match: { default: true }          → researcher-primary
```

## Agent Configuration

Each agent has its own config file, independent of the bus. The config
contains everything the agent needs to run:

```yaml
# researcher agent config (config-domain.yaml)
db_path: data/researcher-domain.db
corpus_scope: domain
relevance_threshold: 0.7
ollama_url: http://localhost:11434
models:
  summarizer: qwen2.5:7b
  extractor: llama3.2:3b
projects:
  private-app:
    repo: /path/to/private-app
    description: "Domain-specific application with multiple specialist agents..."
```

The bus doesn't read agent configs. Agents own their own configuration.
The only config the bus stores is startup metadata (command, args, cwd,
config path) — enough to start the process, not to understand it.

## BaseAgent (in khonliang-bus-lib)

The base class every agent inherits from. Handles the lifecycle
boilerplate so agent authors focus on skills, not plumbing.

```python
from khonliang_bus import BaseAgent, handler, Skill

class ResearcherAgent(BaseAgent):
    agent_type = "researcher"
    module_name = "researcher.agent"
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pipeline = create_pipeline(self.config_path)
    
    def register_skills(self) -> list[Skill]:
        return [
            Skill("find_papers", "Search arxiv + semantic scholar", params={...}),
            Skill("synergize", "Classify concepts, generate FRs", params={...}),
        ]
    
    @handler("find_papers")
    async def find_papers(self, args):
        return self.pipeline.search(args["query"])
    
    @handler("synergize")
    async def synergize(self, args):
        return await self.pipeline.synergize(**args)

if __name__ == "__main__":
    agent = ResearcherAgent.from_cli()  # parses --id, --bus, --config
    asyncio.run(agent.start())
```

**What BaseAgent provides (don't override):**
- `from_cli()` — parse `--id`, `--bus`, `--config` from command line
- `install()` — `POST /v1/install` with self-described startup metadata
- `uninstall()` — `DELETE /v1/install/{id}`
- `start()` — bind port 0, initialize, register, heartbeat loop, serve
- `shutdown()` — deregister, close server
- Port binding (port 0, OS assigns)
- PID reporting
- Heartbeat loop
- Request dispatch to `@handler` functions
- Graceful shutdown on SIGTERM/SIGINT

**What you override:**
- `register_skills()` — what this agent can do
- `register_collaborations()` — what this agent can do with others
- `@handler("skill_name")` — the actual skill implementation
- `__init__` — load your own pipeline, config, state

**Bulk migration from existing MCP server:**

```python
# Existing MCP tools become bus handlers in one call
agent = BaseAgent.from_mcp(
    existing_fastmcp_server,
    agent_type="researcher",
)
```

Introspects the FastMCP server's registered tools, wraps each as a
`@handler`, generates the skill list from tool schemas. Existing tool
code stays identical — the transport changes from stdio to bus HTTP.

## Self-Managed Agents

Not every agent needs to be installed in the bus. An agent can start
independently and register:

```python
agent = ResearcherAgent(
    agent_id="researcher-adhoc",
    bus_url="http://localhost:8787",
    config_path="config.yaml",
)
asyncio.run(agent.start())
```

The bus accepts the registration and routes to it like any other agent.
But since it's not in the installed_agents table:
- The bus won't restart it if it crashes
- The bus won't start it on boot
- `GET /v1/install` won't list it
- `GET /v1/services` WILL list it (it's registered and running)

This is useful for development (start an agent manually, test it, kill
it) and for ephemeral agents (spin up a specialist for one task, tear
it down when done).
