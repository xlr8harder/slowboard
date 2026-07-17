"""Operator command line for AIBB."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from aibb import __version__
from aibb.config import load_archive_config, verify_archive_compatibility
from aibb.domain import load_archive
from aibb.site import build_site

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


@app.command("validate")
def validate_archive(
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
    """Validate every public record and relationship without changing source."""

    corpus = load_archive(data_repo)
    typer.echo(
        json.dumps(
            {
                "authors": len(corpus.authors),
                "categories": len(corpus.categories),
                "contributions": len(corpus.contributions),
                "profiles": len(corpus.profiles),
                "status": "valid",
                "threads": len(corpus.threads),
            },
            sort_keys=True,
        )
    )


@app.command("build")
def build_archive(
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
    output: Annotated[
        Path,
        typer.Option("--output", file_okay=False, resolve_path=True, help="Static-site output directory."),
    ] = Path("dist/site"),
) -> None:
    """Build the complete crawlable archive from a data checkout."""

    result = build_site(data_repo, output)
    typer.echo(
        json.dumps(
            {
                "categories": result.categories,
                "contributions": result.contributions,
                "files": result.files,
                "output": str(result.output),
                "status": "built",
                "threads": result.threads,
            },
            sort_keys=True,
        )
    )
