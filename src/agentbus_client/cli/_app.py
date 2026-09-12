from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import click

from ._common import AGENT_NAME

Handler = Callable[[argparse.Namespace], int]

HELP_NAMES = ["-h", "--help"]


class _Verbatim(click.ParamType):
    name = "text"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        return value


VERBATIM = _Verbatim()


def _check_agent_name(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is not None and not AGENT_NAME.match(value):
        raise click.BadParameter(
            "agent names use only letters, digits, '.', '_' and '-'", ctx=ctx, param=param
        )
    return value


class BusArgument(click.Argument):
    def __init__(self, param_decls: Sequence[str], help: str | None = None, **attrs: Any) -> None:
        super().__init__(param_decls, **attrs)
        self.help = help


def argument(*param_decls: str, **attrs: Any) -> Callable[[Any], Any]:
    return click.argument(*param_decls, cls=BusArgument, **attrs)


class Verb(click.Command):
    def __init__(
        self,
        name: str,
        handler: Handler,
        *,
        params: list[click.Parameter],
        help: str | None = None,
        common: bool = False,
        none_when_empty: Iterable[str] = (),
        exclusive: Iterable[tuple[str, ...]] = (),
        remainder: bool = False,
        nested: bool = False,
    ) -> None:
        if common:
            params = [
                *params,
                click.Option(
                    ["--agent", "sub_agent"],
                    default=None,
                    callback=_check_agent_name,
                    metavar="NAME",
                    help="acting agent; defaults to the agent `agentbus whoami` shows",
                ),
                click.Option(
                    ["--json", "sub_json"],
                    is_flag=True,
                    default=False,
                    help="machine-readable output (may also precede the command)",
                ),
            ]
        settings: dict[str, Any] = {}
        if remainder:
            settings = {"ignore_unknown_options": True, "allow_interspersed_args": False}
        super().__init__(name, params=params, help=help, context_settings=settings)
        self.handler = handler
        self.none_when_empty = frozenset(none_when_empty)
        self.exclusive = tuple(exclusive)
        self.remainder = remainder
        self.nested = nested

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if (
            self.remainder
            and args
            and args[0].startswith("-")
            and args[0] != "--"
            and args[0] not in ctx.help_option_names
        ):
            raise click.NoSuchOption(args[0], ctx=ctx)
        rest = super().parse_args(ctx, args)
        for group in self.exclusive:
            given = [n for n in group if ctx.params.get(n)]
            if len(given) > 1:
                flags = " and ".join("--" + n.replace("_", "-") for n in given)
                raise click.UsageError(f"{flags} cannot be used together.", ctx)
        return rest

    def invoke(self, ctx: click.Context) -> argparse.Namespace:
        return namespace_for(ctx)

    def get_help(self, ctx: click.Context) -> str:
        from . import _help

        return _help.verb_help(self, ctx)


class VerbGroup(click.Group):
    def __init__(self, name: str, *, dest: str, help: str | None = None) -> None:
        super().__init__(name, help=help, no_args_is_help=False, invoke_without_command=False)
        self.dest = dest

    def get_help(self, ctx: click.Context) -> str:
        from . import _help

        return _help.group_help(self, ctx)


class UnknownVerb(click.UsageError):
    def __init__(self, typed: str, hint: str | None, ctx: click.Context) -> None:
        super().__init__(f"there is no `{typed}` command.", ctx)
        self.typed = typed
        self.hint = hint


class HelpCommand(click.Command):
    def __init__(self) -> None:
        super().__init__(
            "help",
            params=[click.Argument(["topic"], nargs=-1)],
            add_help_option=False,
            context_settings={"ignore_unknown_options": True},
        )

    def invoke(self, ctx: click.Context) -> None:
        root_ctx = ctx.parent
        assert root_ctx is not None
        root = root_ctx.command
        assert isinstance(root, Root)
        target: click.Command = root
        target_ctx = root_ctx
        for word in ctx.params.get("topic") or ():
            children = target.commands if isinstance(target, click.Group) else {}
            if word not in children:
                raise UnknownVerb(word, root.suggest(word, sorted(root.commands)), root_ctx)
            target = children[word]
            target_ctx = target.make_context(word, [], parent=target_ctx, resilient_parsing=True)
        click.echo(target_ctx.get_help(), color=ctx.color)
        ctx.exit(0)


class Root(click.Group):
    def __init__(self, suggest: Callable[[str, Sequence[str]], str | None], version: str) -> None:
        params: list[click.Parameter] = [
            click.Option(
                ["--version"],
                is_flag=True,
                expose_value=False,
                is_eager=True,
                callback=_print_version,
                help="print the client version and exit",
            ),
            click.Option(
                ["--api-key", "api_key"],
                default=None,
                metavar="KEY",
                help="defaults to $AGENTBUS_API_KEY",
            ),
            click.Option(
                ["--base-url", "base_url"],
                default=None,
                metavar="URL",
                help="defaults to $AGENTBUS_BASE_URL",
            ),
            click.Option(
                ["--agent", "agent"],
                default=None,
                metavar="NAME",
                help="acting agent; defaults to the agent `agentbus whoami` shows",
                callback=_check_agent_name,
            ),
            click.Option(
                ["--json", "json"], is_flag=True, default=False, help="machine-readable output"
            ),
        ]
        super().__init__(
            "agentbus",
            params=params,
            callback=_root_callback,
            invoke_without_command=True,
            no_args_is_help=False,
            context_settings={"help_option_names": HELP_NAMES},
        )
        self.suggest = suggest
        self.version = version
        self.help_command = HelpCommand()

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        name = args[0]
        if name == "help" and self.get_command(ctx, "help") is None:
            return "help", self.help_command, args[1:]
        if (
            self.get_command(ctx, name) is None
            and not name.startswith("-")
            and not ctx.resilient_parsing
        ):
            raise UnknownVerb(name, self.suggest(name, sorted(self.commands)), ctx)
        return super().resolve_command(ctx, args)

    def get_help(self, ctx: click.Context) -> str:
        from . import _help

        return _help.overview(self, ctx)


def _print_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    root = ctx.find_root().command
    click.echo(f"agentbus {getattr(root, 'version', '')}")
    ctx.exit(0)


def _root_callback(**_params: Any) -> None:
    ctx = click.get_current_context()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help(), color=ctx.color)
        ctx.exit(0)


def verb(
    name: str,
    handler: Handler,
    *,
    parent: VerbGroup | None = None,
    **options: Any,
) -> Callable[[Callable[..., Any]], Verb]:
    def decorate(f: Callable[..., Any]) -> Verb:
        params = list(reversed(getattr(f, "__click_params__", [])))
        cmd = Verb(name, handler, params=params, nested=parent is not None, **options)
        if parent is not None:
            parent.add_command(cmd)
        return cmd

    return decorate


def namespace_for(ctx: click.Context) -> argparse.Namespace:
    chain: list[click.Context] = []
    node: click.Context | None = ctx
    while node is not None:
        chain.append(node)
        node = node.parent
    chain.reverse()
    root, leaf = chain[0], chain[-1]
    command = leaf.command
    assert isinstance(command, Verb)
    values: dict[str, Any] = dict(root.params)
    values["command"] = chain[1].info_name
    if len(chain) == 3:
        group = chain[1].command
        assert isinstance(group, VerbGroup)
        values[group.dest] = leaf.info_name
    for param in command.params:
        if not param.expose_value or param.name is None:
            continue
        value = leaf.params.get(param.name)
        if param.name == "sub_agent":
            if value is not None:
                values["agent"] = value
            continue
        if param.name == "sub_json":
            values["json"] = bool(values.get("json")) or bool(value)
            continue
        if isinstance(value, tuple):
            items = list(value)
            if command.remainder and items[:1] == ["--"]:
                items = items[1:]
            value = None if not items and param.name in command.none_when_empty else items
        values[param.name] = value
    values["func"] = command.handler
    return argparse.Namespace(**values)


def parse(root: Root, argv: Sequence[str] | None) -> argparse.Namespace:
    from . import _help

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        result = root.main(args, prog_name="agentbus", standalone_mode=False)
    except click.UsageError as exc:
        _help.usage_error(exc)
        raise SystemExit(exc.exit_code) from None
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from None
    except click.Abort:
        raise SystemExit(1) from None
    if isinstance(result, argparse.Namespace):
        return result
    raise SystemExit(result if isinstance(result, int) else 0)
