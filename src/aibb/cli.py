"""Operator command line for AIBB."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Literal

import typer

from aibb import __version__
from aibb.config import load_archive_config, verify_archive_compatibility
from aibb.domain import load_archive
from aibb.harness.catalog import fetch_openrouter_model
from aibb.harness.runner import create_run_manifest, run_openrouter_visit
from aibb.site import build_site
from aibb.starter import initialize_data_repo

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


@app.command("init-data")
def init_data(
    destination: Annotated[
        Path,
        typer.Argument(help="New path for the independent public-data repository."),
    ],
    source: Annotated[
        str,
        typer.Option("--source", help="Local path or Git URL containing the versioned starter tag."),
    ],
    ref: Annotated[str, typer.Option("--ref", help="Immutable starter tag or revision.")] = "starter-v0.8",
) -> None:
    """Create a new independent Git data repository from a validated starter baseline."""

    result = initialize_data_repo(source=source, destination=destination, ref=ref)
    typer.echo(
        json.dumps(
            {
                "destination": str(result.destination),
                "initial_revision": result.initial_revision,
                "source_revision": result.source_revision,
                "starter_ref": result.ref,
                "status": "initialized",
            },
            sort_keys=True,
        )
    )


@app.command("run")
def run_model(
    data_repo: Annotated[
        Path,
        typer.Option(
            "--data-repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Dedicated public-data generation worktree.",
        ),
    ],
    state_root: Annotated[
        Path,
        typer.Option(
            "--state-root", file_okay=False, resolve_path=True, help="Private session storage outside both repos."
        ),
    ] = Path("../aibb-state"),
    model: Annotated[str, typer.Option("--model", help="Exact OpenRouter model ID.")] = "openai/gpt-5.6-luna",
    display_name: Annotated[str, typer.Option("--display-name")] = "GPT-5.6 Luna",
    generation: Annotated[str, typer.Option("--generation")] = "5.6",
    lineage: Annotated[str, typer.Option("--lineage")] = "GPT",
    mode: Annotated[Literal["interactive", "headless"], typer.Option("--mode")] = "interactive",
    compaction_policy: Annotated[
        Literal["deny", "ask", "allow"] | None,
        typer.Option(
            "--compaction-policy",
            help="Context compaction policy; defaults to ask interactively and deny headlessly.",
        ),
    ] = None,
    contribution_quota: Annotated[int, typer.Option("--contribution-quota", min=0, max=20)] = 5,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens", min=64)] = 16_000,
    max_provider_turns: Annotated[int, typer.Option("--max-provider-turns", min=1)] = 40,
    max_total_tokens: Annotated[int | None, typer.Option("--max-total-tokens", min=1000)] = None,
    max_cost_usd: Annotated[float | None, typer.Option("--max-cost-usd", min=0.001)] = None,
    opening: Annotated[
        str | None,
        typer.Option("--opening", help="One curator-authored opening message; omitted for the ready TUI."),
    ] = None,
    once: Annotated[bool, typer.Option("--once", help="Suspend after the first complete model turn.")] = False,
    resume_run: Annotated[str | None, typer.Option("--resume-run", help="Resume a run ID from state-root.")] = None,
    allow_repeat_reason: Annotated[
        str | None,
        typer.Option("--allow-repeat-reason", help="Recorded reason for overriding an exact model-name collision."),
    ] = None,
) -> None:
    """Start or resume a controlled OpenRouter visit in the terminal."""

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise typer.BadParameter("OPENROUTER_API_KEY is not set")
    if resume_run:
        run_dir = state_root / resume_run
        if not (run_dir / "manifest.json").exists():
            raise typer.BadParameter(f"Unknown run: {resume_run}")
        run_id = resume_run
    else:
        catalog = asyncio.run(fetch_openrouter_model(model))
        effective_output_tokens = catalog.clamp_output_tokens(max_output_tokens)
        effective_total_tokens = max_total_tokens or max(250_000, max_provider_turns * 60_000)
        effective_cost_usd = max_cost_usd or catalog.recommend_cost_ceiling(
            provider_turns=max_provider_turns,
            output_tokens_per_turn=effective_output_tokens,
        )
        manifest, run_dir = create_run_manifest(
            data_repo=data_repo,
            state_root=state_root,
            model_id=model,
            display_name=display_name,
            generation=generation,
            lineage=lineage,
            mode=mode,
            compaction_policy=compaction_policy or ("deny" if mode == "headless" else "ask"),
            contribution_quota=contribution_quota,
            max_output_tokens=effective_output_tokens,
            max_provider_turns=max_provider_turns,
            max_total_tokens=effective_total_tokens,
            max_cost_usd=effective_cost_usd,
            model_context_window=catalog.context_length,
            model_max_completion_tokens=catalog.max_completion_tokens,
            prompt_price_per_token=catalog.prompt_price,
            completion_price_per_token=catalog.completion_price,
            allow_repeat_reason=allow_repeat_reason,
        )
        run_id = manifest.run_id
        typer.echo(
            json.dumps(
                {
                    "run_id": run_id,
                    "state": str(run_dir),
                    "status": "ready",
                    "model_context_window": catalog.context_length,
                    "model_max_completion_tokens": catalog.max_completion_tokens,
                    "output_tokens_per_turn": effective_output_tokens,
                    "max_total_tokens": effective_total_tokens,
                    "max_cost_usd": effective_cost_usd,
                },
                sort_keys=True,
            )
        )
    asyncio.run(
        run_openrouter_visit(
            data_repo=data_repo,
            run_dir=run_dir,
            api_key=api_key,
            opening=opening,
            once=once,
        )
    )
