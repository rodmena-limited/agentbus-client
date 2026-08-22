"""onboarding — split into one module per concern (review #23, file-size cap).

Every name is re-exported here so `agentbus_client.onboarding.<name>` keeps resolving;
tests that patch a helper do so on the module that defines it.
"""

from __future__ import annotations

from . import (
    _claude_setup,
    _credentials,
    _doctor,
    _identity,
    _opencode_setup,
    _paths,
    _provision,
    _signin,
    _skill,
)
from ._claude_setup import (
    _setup_claude,
)
from ._credentials import (
    _inherited_flag,
    _scope_of_bearer,
    doctor_credential_scope,
    explain_refusal,
    resolve_credentials,
)
from ._doctor import (
    _finish_wake_report,
    _installed_version,
    _monitor_pids,
    _running_watcher_version,
    doctor_wake,
)
from ._identity import (
    _agent_from_worktree,
    _agent_key,
    _derived_name,
    _operator_key,
    _project_claude_dir,
    _resolve_agent_name,
    _session_identity,
    _signed_in_bound_agent,
    _write_worktree_identity,
)
from ._opencode_setup import (
    _setup_opencode,
    cmd_as,
    cmd_setup,
    cmd_sibling,
    cmd_teardown,
)
from ._paths import (
    _MARKER_HOOK,
    _MARKER_REWAKE,
    _PENDING_CMD,
    _SESSION_START_CMD,
    _STOP_CMD,
    HARNESSES,
    OPENCODE_PLUGIN_NPM,
    REWAKE_HOOK_TIMEOUT_SEC,
    REWAKE_WINDOW_SEC,
    STOP_REWAKE_SH,
    STOP_REWAKE_VERSION,
    _config_dir,
    _device_hash,
    _dump_json,
    _ensure_hook_entry,
    _git_remote_or_none,
    _git_root_or_none,
    _key_from_env_file,
    _keys_dir,
    _load_json,
    _plugin_provides_wake,
    _remove_hook_entry,
    _say,
    _signin_state_path,
    _write_private,
)
from ._provision import (
    _provision_project_agent,
)
from ._signin import (
    _ensure_sealing_key,
    _mint_bound_key,
    _sealing_publish_with_retry,
    cmd_signin,
)
from ._skill import (
    refresh_skill,
    skill_state,
)
