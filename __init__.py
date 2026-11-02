"""UK company sheet split by official source."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True

_SKILL_DESCRIPTION = "Official UK company registers. Call uk_check."


def register(ctx) -> None:
    if __package__:
        from .cli import handle, setup_parser
        from .handlers import TOOLS, invoke
        from .prefix import remember_profile_name
        from .schemas import ALL, TOOLSET
    else:
        from cli import handle, setup_parser
        from handlers import TOOLS, invoke
        from prefix import remember_profile_name
        from schemas import ALL, TOOLSET

    try:
        reported = ctx.profile_name
    except Exception:
        reported = None
    remember_profile_name(reported)

    def _handler(name):
        def _run(args, **_kwargs):
            return invoke(name, args or {})
        return _run

    for schema in ALL:
        if schema["name"] not in TOOLS:
            continue
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=_handler(schema["name"]),
        )
    ctx.register_cli_command(
        name="uk-companies",
        help="Look up a UK company and watch one morning snapshot",
        setup_fn=setup_parser,
        handler_fn=handle,
        description="Local commands for the UK company sheet and the morning watch.",
    )
    try:
        ctx.register_skill(
            "uk-companies",
            Path(__file__).resolve().parent / "skills" / "uk-companies" / "SKILL.md",
            description=_SKILL_DESCRIPTION,
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("uk-companies skill was not registered", exc_info=True)
