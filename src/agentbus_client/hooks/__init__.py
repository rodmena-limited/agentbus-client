"""Session-side integrations. The bus can push; something local must listen.

`claude_code` is re-exported so `from agentbus_client.hooks import claude_code`
resolves for a TYPE CHECKER as well as at runtime. Without it mypy reports
"Module agentbus_client.hooks has no attribute claude_code" against perfectly
working code — and a checker that cries wolf on valid imports is one people stop
reading, which is how the 181 real errors in src/ went unnoticed for months.
"""

from . import claude_code

__all__ = ["claude_code"]
