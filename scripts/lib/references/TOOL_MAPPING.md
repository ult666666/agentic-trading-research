# Tool Mapping

At the start of a broker-connected session, create a temporary tool map from actual tool descriptions.

| Capability | Tool | Read/Write | Required inputs | Important warnings |
|---|---|---|---|---|
| Account snapshot | | Read | | |
| Positions | | Read | | |
| Orders | | Read | | |
| Quote | | Read | | |
| Tradability | | Read | | |
| Order review | | Potentially stateful; verify | | |
| Place order | | Write | | |
| Modify order | | Write | | |
| Cancel order | | Write | | |

Rules:

- Do not install a duplicate broker MCP.
- Do not call write tools during setup, dry runs, or research.
- Do not use a generic shell or browser to bypass broker confirmations.
- Do not delegate execution to a subagent.
- When a tool description is ambiguous, treat it as write-capable.
