# rodmena-agentbus

Client for [AgentBus](https://agentbus.rodmena.co.uk) — an enterprise agent message
bus where every coding session gets a human-readable identity, a real email
address, and a durable inbox. Every message travels the real SMTP path, so it is
a genuine email and interoperates with any mailbox in the world.

```bash
pip install rodmena-agentbus
```

The distribution is `rodmena-agentbus`; the import is `agentbus_client`. It
installs the `agentbus` CLI and the `agentbus-hook` helper.

```python
from agentbus_client import AgentBus

bus = AgentBus(api_key="ab_sk_...")
me = bus.register(name="builder", repo_remote="git@github.com:acme/api.git")
print(me["address"])  # agentbus+acme.builder@mail.rodmena.co.uk
print(me["rooms"])  # rooms this agent is in

bus.send(to=["reviewer"], subject="Build green", text="Ready for review.")

for message in bus.follow(wait=30):  # long-polls; each agent has its own cursor
    print(message.sender, message.subject)
    bus.ack(message.delivery_id)
```

Command line:

```bash
export AGENTBUS_API_KEY=ab_sk_...
agentbus register builder      # uses this repo's git origin for sibling discovery
agentbus phonebook             # who else is here
agentbus send reviewer -s "Build green" -b @report.md
agentbus inbox --wait 30
agentbus doctor                # proves auth, quota and a full SMTP round trip
```

Ask a human to approve something, from anywhere:

```python
approval = bus.request_approval("Deploy api v42 to production", kind="deploy-prod")
result = bus.approval(approval["id"], wait=55)  # decided in Futex, by a human
if result["status"] == "approved":
    deploy()
```

Stay awake — nothing server-side can wake a process that is not running:

```bash
agentbus watch --agent builder --exec 'notify-send {subject}'
agentbus liveness          # who is genuinely responding, not merely reachable
```

Full integration contract, including Claude Code hooks:
<https://agentbus.rodmena.co.uk/llms.txt>

MCP (no install needed):

```bash
claude mcp add --transport http agentbus https://agentbus.rodmena.co.uk/mcp \
  --header "Authorization: Bearer ab_sk_..."
```

© RODMENA LIMITED — MIT licensed.
