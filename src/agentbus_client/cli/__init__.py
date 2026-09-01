"""agentbus CLI — one module per command family (review #23, file-size cap).

Every helper and command is re-exported here so `agentbus_client.cli.<name>` keeps
resolving; tests that patch a helper do so on the module that DEFINES it
(`cli._common._bus`, `cli._keys._local_signing_fingerprint`, ...).
"""

from __future__ import annotations

from . import (
    _common,
    _compose,
    _diag,
    _directory,
    _forward,
    _identities,
    _keys,
    _memory,
    _parser,
    _read,
    _register,
    _service,
    _setup,
    _threads,
    _verify,
    _watch_run,
    _watch_runtime,
    _watch_status,
)
from ._common import *  # the module-level imports the flat cli.py exposed (cli.AgentBus, ...)
from ._common import (
    _accept_common_flags_after_subcommand,
    _as_message_id,
    _bus,
    _cfg_dir,
    _client_version,
    _git_remote,
    _harden_if_possible,
    _key_for_agent,
    _parse_duration,
    _print,
    _print_qr,
    _read_body,
    _resolve_env_agent,
)
from ._compose import (
    _print_batch_error,
    cmd_reply,
    cmd_send,
    cmd_send_batch,
)
from ._diag import (
    cmd_doctor,
    cmd_quickref,
    cmd_refresh_skill,
)
from ._directory import (
    _format_tags,
    cmd_busy,
    cmd_liveness,
    cmd_phonebook,
    cmd_status,
    cmd_tag,
    cmd_whoami,
)
from ._forward import (
    cmd_approve,
    cmd_draft,
    cmd_draft_send,
    cmd_drafts,
    cmd_forward,
    cmd_undeliverable,
)
from ._identities import (
    cmd_health,
    cmd_identities,
)
from ._keys import (
    _key_algorithm,
    _keys_list,
    _keys_revoke,
    _keys_rotate,
    _keys_sign,
    _local_signing_fingerprint,
    _sealing_hostname,
    _superseded_fingerprints,
    _this_machines_fingerprint,
    cmd_keys,
)
from ._parser import build_parser, main
from ._read import (
    _safe_attachment_name,
    cmd_ack,
    cmd_attachment,
    cmd_inbox,
    cmd_labels,
    cmd_show,
)
from ._register import (
    cmd_device_id,
    cmd_identity,
    cmd_invite,
    cmd_join,
    cmd_qr,
    cmd_register,
)
from ._sent import cmd_sent
from ._service import (
    _plist_key_line,
    cmd_retire,
    cmd_service,
)
from ._threads import (
    _render_thread,
    cmd_history,
    cmd_reminders,
    cmd_schema,
    cmd_thread,
    cmd_usage,
)
from ._verify import (
    _verify_exit_code,
    cmd_verify,
    cmd_verify_signature,
)
from ._watch_run import (
    cmd_watch,
)
from ._watch_runtime import (
    _agent_flag_re,
    _existing_logfile,
    _pid_cmdline,
    _pid_is_watcher,
    _scan_watch_process,
    _scope_pids_by_state,
    _slot_state,
    _state_key_for,
    _watch_logfile,
    _watch_pid,
    _watch_pidfile,
    _watch_pids,
    _watch_runtime_dir,
)
from ._watch_status import (
    _read_running_client_version,
    cmd_watch_status,
    cmd_watch_stop,
)

__all__ = ["build_parser", "main"]
