# 0092 — Key paths stay in the keys directory

Ticket: issuedb #92.

EARS SPEC:
R1. If the agent name a checkout declares is not a name a key file can have, then `agentbus teardown --purge-key` shall refuse, delete nothing, and exit non-zero.
R2. Without `--purge-key`, teardown shall still unwire the project.
R3. Every path under the keys directory shall derive its filename through sealing.bound_env_filename or agent_slug.

REPRODUCED on published 0.9.105 and 0.9.106 under a throwaway HOME and AGENTBUS_CONFIG_DIR: a checkout declaring `../operator` in .agentbus/agent made `teardown --purge-key` delete operator.env and exit 0.

FOUND WHILE FIXING: the ticket said every other key path was safe. Reading them showed eight more built by hand, in `_provision.py`, `_signin.py`, `_credentials.py` and `_claude_setup.py`: two renamed a file, four wrote one, one checked for one and one printed a path. Each is tested at the helper. A hostile name reaching them through `agentbus setup` was read, not reproduced end to end.

TECHNICAL PROBLEMS:
1. Path confinement of an untrusted name, at every site that builds one.

SOLUTION DOMAINS:
- The concept exists as sealing.bound_env_filename.

ALTERNATIVES:
- Refuse when the sanitiser would change the name [CHOSEN], then build the path through the sanitiser.
  Purge the sanitised name [REJECTED: `a/b` becomes `a_b`, which can be another agent's real key].
  Resolve and compare against the keys directory [REJECTED: a second implementation of the same guard].
