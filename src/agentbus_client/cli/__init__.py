"""agentbus CLI — one module per command family (review #23, file-size cap).

Every helper and command is re-exported here so `agentbus_client.cli.<name>` keeps
resolving; tests that patch a helper do so on the module that DEFINES it
(`cli._common._bus`, `cli._keys._local_signing_fingerprint`, ...).
"""

from __future__ import annotations

from agentbus_client.client import AgentBus as AgentBus, AgentBusError as AgentBusError
from agentbus_client.client.errors import TransportError as TransportError

from . import (
    _common as _common,
    _compose as _compose,
    _diag as _diag,
    _directory as _directory,
    _forward as _forward,
    _identities as _identities,
    _keys as _keys,
    _memory as _memory,
    _parser as _parser,
    _read as _read,
    _register as _register,
    _service as _service,
    _threads as _threads,
    _verify as _verify,
    _watch_run as _watch_run,
    _watch_runtime as _watch_runtime,
    _watch_status as _watch_status,
)
from ._common import (
    AGENT_NAME as AGENT_NAME,
    NO_ACTING_AGENT as NO_ACTING_AGENT,
    InputError as InputError,
    _as_message_id as _as_message_id,
    _bus as _bus,
    _cfg_dir as _cfg_dir,
    _client_version as _client_version,
    _git_remote as _git_remote,
    _harden_if_possible as _harden_if_possible,
    _key_for_agent as _key_for_agent,
    _parse_duration as _parse_duration,
    _print as _print,
    _print_qr as _print_qr,
    _read_body as _read_body,
    _resolve_env_agent as _resolve_env_agent,
    acting_agent as acting_agent,
    checked_agent_name as checked_agent_name,
    require_acting_agent as require_acting_agent,
)
from ._compose import (
    _print_batch_error as _print_batch_error,
    cmd_reply as cmd_reply,
    cmd_send as cmd_send,
    cmd_send_batch as cmd_send_batch,
)
from ._diag import (
    cmd_doctor as cmd_doctor,
    cmd_quickref as cmd_quickref,
    cmd_refresh_skill as cmd_refresh_skill,
)
from ._directory import (
    _format_tags as _format_tags,
    cmd_busy as cmd_busy,
    cmd_liveness as cmd_liveness,
    cmd_phonebook as cmd_phonebook,
    cmd_status as cmd_status,
    cmd_tag as cmd_tag,
    cmd_whoami as cmd_whoami,
)
from ._forward import (
    cmd_approve as cmd_approve,
    cmd_draft as cmd_draft,
    cmd_draft_send as cmd_draft_send,
    cmd_drafts as cmd_drafts,
    cmd_forward as cmd_forward,
    cmd_undeliverable as cmd_undeliverable,
)
from ._identities import (
    cmd_health as cmd_health,
    cmd_identities as cmd_identities,
)
from ._identity_cmd import (
    cmd_identity as cmd_identity,
)
from ._keys import (
    _key_algorithm as _key_algorithm,
    _keys_list as _keys_list,
    _keys_revoke as _keys_revoke,
    _keys_rotate as _keys_rotate,
    _keys_sign as _keys_sign,
    _local_signing_fingerprint as _local_signing_fingerprint,
    _sealing_hostname as _sealing_hostname,
    _superseded_fingerprints as _superseded_fingerprints,
    _this_machines_fingerprint as _this_machines_fingerprint,
    cmd_keys as cmd_keys,
)
from ._parser import (
    build_parser as build_parser,
    main as main,
)
from ._read import (
    _safe_attachment_name as _safe_attachment_name,
    cmd_ack as cmd_ack,
    cmd_attachment as cmd_attachment,
    cmd_inbox as cmd_inbox,
    cmd_labels as cmd_labels,
    cmd_show as cmd_show,
)
from ._register import (
    cmd_device_id as cmd_device_id,
    cmd_invite as cmd_invite,
    cmd_join as cmd_join,
    cmd_qr as cmd_qr,
    cmd_register as cmd_register,
)
from ._sent import (
    cmd_sent as cmd_sent,
)
from ._service import (
    _plist_key_line as _plist_key_line,
    cmd_retire as cmd_retire,
    cmd_service as cmd_service,
)
from ._threads import (
    _render_thread as _render_thread,
    cmd_history as cmd_history,
    cmd_reminders as cmd_reminders,
    cmd_schema as cmd_schema,
    cmd_thread as cmd_thread,
    cmd_usage as cmd_usage,
)
from ._verify import (
    _verify_exit_code as _verify_exit_code,
    cmd_verify as cmd_verify,
    cmd_verify_signature as cmd_verify_signature,
)
from ._watch_run import (
    cmd_watch as cmd_watch,
)
from ._watch_runtime import (
    _agent_flag_re as _agent_flag_re,
    _existing_logfile as _existing_logfile,
    _pid_cmdline as _pid_cmdline,
    _pid_is_watcher as _pid_is_watcher,
    _scan_watch_process as _scan_watch_process,
    _scope_pids_by_state as _scope_pids_by_state,
    _slot_state as _slot_state,
    _state_key_for as _state_key_for,
    _watch_logfile as _watch_logfile,
    _watch_pid as _watch_pid,
    _watch_pidfile as _watch_pidfile,
    _watch_pids as _watch_pids,
    _watch_runtime_dir as _watch_runtime_dir,
)
from ._watch_status import (
    _read_running_client_version as _read_running_client_version,
    cmd_watch_status as cmd_watch_status,
    cmd_watch_stop as cmd_watch_stop,
)

__all__ = ["build_parser", "main"]
