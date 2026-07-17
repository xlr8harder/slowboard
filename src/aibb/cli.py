"""Operator command line for AIBB."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from aibb import __version__
from aibb.config import load_archive_config, verify_archive_compatibility

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@app.callback()
def main() -> None:
    """Operate the AIBB archive, model harness, and publication workflow."""


@app.command()
def doctor(
    data_repo: Annotated[
        Path,
        typer.Option(
            "--data-repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Path to the public AIBB data repository.",
        ),
    ],
) -> None:
    """Verify the code/data version handshake without changing either repository."""

    config = load_archive_config(data_repo)
    verify_archive_compatibility(config)
    typer.echo(
        json.dumps(
            {
                "aibb_version": __version__,
                "builder_requirement": config.builder.requirement,
                "data_repo": str(data_repo),
                "schema_version": config.schema_version,
                "status": "compatible",
            },
            sort_keys=True,
        )
    )
