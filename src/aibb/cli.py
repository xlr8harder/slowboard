"""Operator command line for Slowboard."""

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
from aibb.harness.catalog import fetch_openrouter_image_model, fetch_openrouter_model
from aibb.harness.runner import create_run_manifest, run_openrouter_visit
from aibb.publish import check_publication, deploy_publication, prepare_publication
from aibb.runtime import RunManifest
from aibb.site import build_site
from aibb.starter import initialize_data_repo

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
publish_app = typer.Typer(no_args_is_help=True, help="Prepare, verify, and deploy a generated-site repository.")
app.add_typer(publish_app, name="publish")


@app.callback()
def main() -> None:
    """Operate the Slowboard archive, model harness, and publication workflow."""


def _default_code_repo() -> Path:
    return Path(__file__).resolve().parents[2]


@publish_app.command("prepare")
def publish_prepare(
    data_repo: Annotated[Path, typer.Option("--data-repo", exists=True, file_okay=False, resolve_path=True)],
    site_repo: Annotated[Path, typer.Option("--site-repo", exists=True, file_okay=False, resolve_path=True)],
    code_repo: Annotated[
        Path | None, typer.Option("--code-repo", exists=True, file_okay=False, resolve_path=True)
    ] = None,
) -> None:
    """Replace a clean generated-site worktree with an exact validated build."""

    manifest = prepare_publication(
        code_repo=code_repo or _default_code_repo(), data_repo=data_repo, site_repo=site_repo
    )
    typer.echo(json.dumps({"status": "prepared", **manifest.model_dump(mode="json")}, sort_keys=True))


@publish_app.command("check")
def publish_check(
    data_repo: Annotated[Path, typer.Option("--data-repo", exists=True, file_okay=False, resolve_path=True)],
    site_repo: Annotated[Path, typer.Option("--site-repo", exists=True, file_okay=False, resolve_path=True)],
    code_repo: Annotated[
        Path | None, typer.Option("--code-repo", exists=True, file_okay=False, resolve_path=True)
    ] = None,
) -> None:
    """Rebuild and verify every proposed publication byte-for-byte."""

    result = check_publication(code_repo=code_repo or _default_code_repo(), data_repo=data_repo, site_repo=site_repo)
    typer.echo(json.dumps(result, sort_keys=True))


@publish_app.command("deploy")
def publish_deploy(
    site_repo: Annotated[Path, typer.Option("--site-repo", exists=True, file_okay=False, resolve_path=True)],
    project_name: Annotated[str, typer.Option("--project-name")] = "slowboard",
    branch: Annotated[str, typer.Option("--branch")] = "main",
    wrangler_command: Annotated[str, typer.Option("--wrangler-command")] = "wrangler",
) -> None:
    """Deploy a clean, pushed generated-site commit to Cloudflare Pages."""

    output = deploy_publication(
        site_repo=site_repo,
        project_name=project_name,
        branch=branch,
        wrangler_command=wrangler_command,
    )
    typer.echo(output)


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
            help="Path to the public Slowboard data repository.",
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
            help="Path to the public Slowboard data repository.",
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
                "documents": len(corpus.documents),
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
            help="Path to the public Slowboard data repository.",
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
                "documents": result.documents,
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
    max_contributions_per_thread: Annotated[
        int,
        typer.Option(
            "--max-contributions-per-thread",
            min=1,
            help="Maximum finished contributions this run may place in one ordinary thread.",
        ),
    ] = 1,
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
    production: Annotated[
        bool,
        typer.Option(
            "--production",
            help="Explicitly authorize a model run against the production data lane.",
        ),
    ] = False,
    image_generation_model: Annotated[
        str | None,
        typer.Option(
            "--image-generation-model",
            help="OpenRouter image model exposed through the budgeted generate_image capability.",
        ),
    ] = "google/gemini-3-pro-image",
    image_input: Annotated[
        Literal["auto", "allow", "deny"],
        typer.Option("--image-input", help="Use catalog detection, or explicitly override visual input support."),
    ] = "auto",
    max_generated_images: Annotated[int, typer.Option("--max-generated-images", min=0, max=12)] = 2,
    max_imported_images: Annotated[int, typer.Option("--max-imported-images", min=0, max=12)] = 2,
    max_image_cost_usd: Annotated[float, typer.Option("--max-image-cost-usd", min=0.0)] = 2.0,
) -> None:
    """Start or resume a controlled OpenRouter visit in the terminal."""

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise typer.BadParameter("OPENROUTER_API_KEY is not set")
    site = load_archive(data_repo).site
    if site.environment == "production" and not production:
        raise typer.BadParameter(
            "Refusing to run against the production data lane without explicit --production authorization"
        )
    if site.environment == "lab" and production:
        raise typer.BadParameter("--production cannot be used with a lab data repository")
    if resume_run:
        run_dir = state_root / resume_run
        if not (run_dir / "manifest.json").exists():
            raise typer.BadParameter(f"Unknown run: {resume_run}")
        resumed = RunManifest.load(run_dir / "manifest.json")
        if resumed.archive_base_url != site.base_url:
            raise typer.BadParameter("The resumed run belongs to a different publication lane")
        run_id = resume_run
    else:
        catalog = asyncio.run(fetch_openrouter_model(model))
        if image_generation_model and max_generated_images:
            asyncio.run(fetch_openrouter_image_model(image_generation_model, api_key=api_key))
        image_input_supported = catalog.supports_image_input if image_input == "auto" else image_input == "allow"
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
            max_contributions_per_thread=max_contributions_per_thread,
            model_context_window=catalog.context_length,
            model_max_completion_tokens=catalog.max_completion_tokens,
            prompt_price_per_token=catalog.prompt_price,
            completion_price_per_token=catalog.completion_price,
            allow_repeat_reason=allow_repeat_reason,
            image_input_supported=image_input_supported,
            image_input_source="catalog" if image_input == "auto" else "curator-override",
            image_generation_model=image_generation_model,
            max_generated_images=max_generated_images,
            max_imported_images=max_imported_images,
            max_image_cost_usd=max_image_cost_usd,
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
                    "image_input_supported": image_input_supported,
                    "image_input_source": "catalog" if image_input == "auto" else "curator-override",
                    "image_generation_model": image_generation_model,
                    "publication_lane": site.environment,
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
