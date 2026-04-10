# Agent Model Review

**Reviewer:** Claude (developer session)
**Document:** `specs/bus-mcp/agents.md`
**Date:** 2026-04-10

---

## TL;DR

The agent model is clean and buildable. The install/register separation is the standout design choice — persistent startup metadata vs runtime state solves recovery naturally. `BaseAgent` with `@handler` mirrors the existing `@mcp.tool()` pattern so migration is familiar. `from_mcp()` bulk migration makes adoption practical.

One required fix (references to a private application name), five design clarifications, no structural changes needed.

---

## Required fix

Lines 38, 229, and 249-262 reference a private sibling application by name. These must be replaced with generic examples before committing to a public repo. Use `researcher-trading` / `trading` / `myapp` or similar.

---

## Design observations

### 1. Install vs register — excellent

The two-phase lifecycle is the doc's best contribution. `installed_agents` is durable ("how to start me"); `registrations` is ephemeral ("where I am now"). On bus restart: read installed_agents, start processes, wait for registrations. On agent restart: re-register with new PID + port. No orphaned state, no stale routing entries. This pattern handles every recovery scenario cleanly.

### 2. `from_mcp()` — translation gap to address

MCP tools return strings (per the `format_response` / `compact_summary` convention). Bus handlers return dicts (per the interaction protocol's JSON response format). `BaseAgent.from_mcp(existing_server)` needs a thin translation layer between these formats.

Three options:
- **(A)** Bus handlers also return strings. Simplest; bus just passes them through. But loses structure for the flow engine (can't reference `{{step.1.title}}` from a string).
- **(B)** `from_mcp()` wraps each tool: calls the MCP function, gets the string, wraps it as `{"result": string}`. The flow engine treats the result as an opaque blob. Works for solo skills; limits DAG references in flows to the whole result, not subfields.
- **(C)** Agent authors migrate tools to return dicts natively over time. `from_mcp()` is a transitional bridge (option B) that gets replaced as tools are rewritten.

**Recommendation:** (B) for migration, (C) as the end state. Note this in the doc so implementers know `from_mcp()` is a bridge, not the permanent API.

### 3. Multiple instances — routing resolution unclear

The doc shows `researcher-primary` and `researcher-private-app` as two instances with different configs. The routing section shows match rules:

```yaml
routing:
  find_papers:
    - match: { project: private-app } → researcher-private-app
    - match: { default: true }      → researcher-primary
```

Questions:
- Where do these routing rules live? In the bus config? In a flow declaration? In the agent registration?
- Who writes them? The operator? The agent at install time?
- What happens when Claude calls `researcher.find_papers` without specifying a project — does the bus apply the default match, or does Claude see both instances in the catalog and pick?

**Recommendation:** keep routing simple for v1. Claude sees each instance by its agent ID (`researcher-primary.find_papers`, `researcher-trading.find_papers`). Context-based routing (match on args) is a later optimization. Mention this in the doc so the routing snippet doesn't imply it's needed for Step 1.

### 4. Self-managed agents — a good escape hatch

The distinction between installed (bus-managed, restarted on crash) and self-managed (register-only, ephemeral) is useful. Dev workflow: start an agent manually, test skills, kill it. Production: install so the bus keeps it alive.

One addition worth making: can an installed agent also be started externally? E.g., an operator starts researcher manually for debugging while the bus thinks it should manage it. The bus would see a registration from an agent it thinks it should start — does it skip starting it since it's already registered? Or conflict? Suggest: if an installed agent registers before the bus starts it, the bus accepts the registration and skips its own start attempt. Note this edge case.

### 5. `register_skills()` vs `register_collaborations()` — version gates missing

The `BaseAgent` class shows `register_skills()` and `register_collaborations()` as separate methods, but neither the method signatures nor the example show version requirements (`since:` on skills, `requires:` on collaborations). The architecture doc established these as part of the registration schema. They should appear in the `Skill` dataclass and the `BaseAgent` example so the agent model doc is self-consistent with the architecture.

---

## What's right (keep as-is)

- **Port 0 binding** — OS assigns the port, agent reports it at registration. No port config, no conflicts.
- **PID reporting** — enables instant crash detection without waiting for heartbeat timeout.
- **Agent captures its own startup details** — `sys.executable`, module name, cwd. "The agent knows how to describe itself because it IS itself." Eliminates a whole class of path-configuration bugs.
- **`BaseAgent` provides lifecycle, you override skills** — clean separation. Agent authors never see HTTP, heartbeats, or registration. They write `@handler` functions.
- **`from_cli()`** parses `--id`, `--bus`, `--config` — uniform CLI interface for all agents. The bus uses the same args when starting agents on boot.
- **Deregister on clean shutdown** — agent announces departure so the bus doesn't wait for heartbeat timeout to notice.
- **Uninstall is explicit** — `DELETE /v1/install/{id}`. An agent that's stopped isn't removed from the platform; it's just not running. Clean lifecycle.

---

## Verdict

**Approved with the required fix (private name references) and the design clarifications above.** The agent model is well-designed, practical, and aligned with the architecture doc. The install/register split, `BaseAgent` class, and self-managed agent escape hatch are the right abstractions for the platform's needs.

After fixing the private name references, this is ready to commit alongside the architecture doc.
