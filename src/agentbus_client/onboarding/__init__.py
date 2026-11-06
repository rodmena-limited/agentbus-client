"""onboarding — split into one module per concern (review #23, file-size cap).

Every name is re-exported here so `agentbus_client.onboarding.<name>` keeps resolving;
tests that patch a helper do so on the module that defines it.
"""

from __future__ import annotations

from . import (
    _claude_setup as _claude_setup,
    _credentials as _credentials,
    _doctor as _doctor,
    _identity as _identity,
    _opencode_setup as _opencode_setup,
    _paths as _paths,
    _provision as _provision,
    _signin as _signin,
    _skill as _skill,
)
from ._agy_setup import (
    _setup_agy as _setup_agy,
    teardown_agy_machine as teardown_agy_machine,
)
from ._claude_setup import (
    _setup_claude as _setup_claude,
)
from ._credentials import (
    _inherited_flag as _inherited_flag,
    _scope_of_bearer as _scope_of_bearer,
    doctor_credential_scope as doctor_credential_scope,
    explain_refusal as explain_refusal,
    resolve_credentials as resolve_credentials,
)
from ._doctor import (
    _finish_wake_report as _finish_wake_report,
    _installed_version as _installed_version,
    _monitor_pids as _monitor_pids,
    _running_watcher_version as _running_watcher_version,
    doctor_wake as doctor_wake,
)
from ._identity import (
    _agent_from_worktree as _agent_from_worktree,
    _agent_key as _agent_key,
    _derived_name as _derived_name,
    _operator_key as _operator_key,
    _project_claude_dir as _project_claude_dir,
    _resolve_agent_name as _resolve_agent_name,
    _session_identity as _session_identity,
    _signed_in_bound_agent as _signed_in_bound_agent,
    _write_worktree_identity as _write_worktree_identity,
)
from ._opencode_setup import (
    _setup_opencode as _setup_opencode,
    cmd_as as cmd_as,
    cmd_setup as cmd_setup,
    cmd_sibling as cmd_sibling,
    cmd_teardown as cmd_teardown,
)
from ._paths import (
    _MARKER_HOOK as _MARKER_HOOK,
    _MARKER_REWAKE as _MARKER_REWAKE,
    _PENDING_CMD as _PENDING_CMD,
    _SESSION_START_CMD as _SESSION_START_CMD,
    _STOP_CMD as _STOP_CMD,
    HARNESS_ALIASES as HARNESS_ALIASES,
    HARNESSES as HARNESSES,
    OPENCODE_PLUGIN_NPM as OPENCODE_PLUGIN_NPM,
    REWAKE_HOOK_TIMEOUT_SEC as REWAKE_HOOK_TIMEOUT_SEC,
    REWAKE_WINDOW_SEC as REWAKE_WINDOW_SEC,
    STOP_REWAKE_SH as STOP_REWAKE_SH,
    STOP_REWAKE_VERSION as STOP_REWAKE_VERSION,
    _config_dir as _config_dir,
    _device_hash as _device_hash,
    _dump_json as _dump_json,
    _ensure_hook_entry as _ensure_hook_entry,
    _git_remote_or_none as _git_remote_or_none,
    _git_root_or_none as _git_root_or_none,
    _key_from_env_file as _key_from_env_file,
    _keys_dir as _keys_dir,
    _load_json as _load_json,
    _plugin_provides_wake as _plugin_provides_wake,
    _remove_hook_entry as _remove_hook_entry,
    _say as _say,
    _signin_state_path as _signin_state_path,
    _write_private as _write_private,
    canonical_harness as canonical_harness,
)
from ._provision import (
    _provision_project_agent as _provision_project_agent,
)
from ._signin import (
    _ensure_sealing_key as _ensure_sealing_key,
    _mint_bound_key as _mint_bound_key,
    _sealing_publish_with_retry as _sealing_publish_with_retry,
    cmd_signin as cmd_signin,
)
from ._skill import (
    refresh_skill as refresh_skill,
    skill_state as skill_state,
)
