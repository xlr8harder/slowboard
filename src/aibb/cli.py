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
from aibb.harness.watch import latest_run_directory, watch_event_stream
from aibb.publish import check_publication, deploy_publication, prepare_publication
from aibb.runtime import BudgetLedger, RunManifest
from aibb.runtime.models import BudgetLimits
from aibb.sessions import SessionStore
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


def _resolve_image_policy(policy: Literal["auto", "enable", "disable"], image_input_supported: bool) -> bool:
    if policy == "enable" and not image_input_supported:
        raise typer.BadParameter(
            "--images enable requires catalog-advertised image input or an explicit --image-input allow override"
        )
    return image_input_supported and policy != "disable"


@app.command("watch-run")
def watch_run(
    state_root: Annotated[
        Path,
        typer.Option("--state-root", exists=True, file_okay=False, resolve_path=True),
    ] = Path("../aibb-state"),
    run_id: Annotated[str | None, typer.Option("--run-id", help="Run ID; defaults to the newest run.")] = None,
    follow: Annotated[bool, typer.Option("--follow/--no-follow")] = True,
    from_start: Annotated[bool, typer.Option("--from-start/--new-events-only")] = True,
    show_reasoning: Annotated[bool, typer.Option("--show-reasoning/--hide-reasoning")] = True,
) -> None:
    """Watch a private run as a readable local transcript of reasoning, tools, and usage."""

    run_dir = state_root / run_id if run_id else latest_run_directory(state_root)
    if not (run_dir / "manifest.json").exists():
        raise typer.BadParameter(f"Unknown run: {run_dir.name}")
    typer.echo(f"Watching {run_dir.name} from {run_dir / 'session/events.jsonl'}")
    try:
        watch_event_stream(
            run_dir,
            follow=follow,
            from_start=from_start,
            show_reasoning=show_reasoning,
        )
    except KeyboardInterrupt:
        typer.echo("Stopped watching; the model run was not interrupted.")


@app.command("extend-inference-budget")
def extend_inference_budget(
    run_id: Annotated[str, typer.Option("--run-id", help="Suspended run ID to extend.")],
    max_total_tokens: Annotated[
        int,
        typer.Option(
            "--max-total-tokens",
            min=1_000,
            help="New cumulative input and total-token ceilings; must exceed both existing ceilings.",
        ),
    ],
    reason: Annotated[
        str,
        typer.Option("--reason", min=8, help="Curator reason recorded in the append-only private session stream."),
    ],
    state_root: Annotated[
        Path,
        typer.Option("--state-root", exists=True, file_okay=False, resolve_path=True),
    ] = Path("../aibb-state"),
) -> None:
    """Extend only a suspended run's operational inference-token ceiling."""

    run_dir = state_root / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise typer.BadParameter(f"Unknown run: {run_id}")
    if (run_dir / "mcp/visit-conclusion.json").exists():
        raise typer.BadParameter("A concluded visit cannot receive an inference-budget extension")
    manifest = RunManifest.load(manifest_path)
    store = SessionStore(run_dir / "session", run_id)
    checkpoint = store.read_checkpoint()
    ledger = BudgetLedger(run_dir / "mcp/budgets.json", manifest)
    previous, updated = ledger.extend_limits(
        "inference",
        BudgetLimits(max_input_tokens=max_total_tokens, max_total_tokens=max_total_tokens),
    )
    event = store.append(
        "inference_budget_extended",
        {
            "reason": reason,
            "original_manifest_unchanged": True,
            "previous": previous.model_dump(mode="json"),
            "updated": updated.model_dump(mode="json"),
        },
        "operator",
    )
    store.write_checkpoint(checkpoint.engine)
    typer.echo(
        json.dumps(
            {
                "run_id": run_id,
                "event_sequence": event.sequence,
                "status": "extended",
                "previous_max_total_tokens": previous.max_total_tokens,
                "new_max_total_tokens": updated.max_total_tokens,
            },
            sort_keys=True,
        )
    )


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
    generation: Annotated[
        str | None,
        typer.Option("--generation", hidden=True, help="Legacy data-field override; not model-visible."),
    ] = None,
    lineage: Annotated[
        str | None,
        typer.Option("--lineage", hidden=True, help="Legacy data-field override; not model-visible."),
    ] = None,
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
    reasoning_mode: Annotated[
        Literal["auto", "enabled", "mandatory", "disabled"],
        typer.Option(
            "--reasoning-mode",
            help=(
                "Use catalog detection or a recorded curator override. Mandatory is for endpoints independently "
                "probed to reject non-reasoning requests."
            ),
        ),
    ] = "auto",
    curator_note: Annotated[
        str | None,
        typer.Option(
            "--curator-note",
            "--opening",
            help="One model-visible, curator-authored note at the start of the visit; omitted for the ready TUI.",
        ),
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
    images: Annotated[
        Literal["auto", "enable", "disable"],
        typer.Option(
            "--images",
            help=(
                "Image policy: auto enables visual access and image tools only for detected image-input models; "
                "enable requires detected support (or --image-input allow); disable keeps the visit text-only."
            ),
        ),
    ] = "auto",
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
    max_web_calls: Annotated[
        int,
        typer.Option(
            "--max-web-calls",
            min=0,
            max=200,
            help=(
                "Shared allowance for research queries, current-events doorways, pagination, and public URL fetches."
            ),
        ),
    ] = 40,
    max_web_cost_usd: Annotated[
        float,
        typer.Option(
            "--max-web-cost-usd",
            min=0.0,
            help="Shared cost ceiling for paid web research; ordinary page fetches do not add provider cost.",
        ),
    ] = 5.0,
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
        image_input_supported = catalog.supports_image_input if image_input == "auto" else image_input == "allow"
        image_capabilities_enabled = _resolve_image_policy(images, image_input_supported)
        if image_capabilities_enabled and image_generation_model and max_generated_images:
            asyncio.run(fetch_openrouter_image_model(image_generation_model, api_key=api_key))
        effective_output_tokens = catalog.clamp_output_tokens(max_output_tokens)
        effective_total_tokens = max_total_tokens or max(250_000, max_provider_turns * 60_000)
        effective_cost_usd = max_cost_usd or catalog.recommend_cost_ceiling(
            provider_turns=max_provider_turns,
            output_tokens_per_turn=effective_output_tokens,
        )
        reasoning_configuration = catalog.select_reasoning(reasoning_mode)
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
            developer=catalog.developer,
            model_input_modalities=sorted(catalog.input_modalities),
            reasoning=reasoning_configuration,
            image_input_supported=image_input_supported,
            image_input_source="catalog" if image_input == "auto" else "curator-override",
            image_capabilities_enabled=image_capabilities_enabled,
            image_generation_model=image_generation_model if image_capabilities_enabled else None,
            max_generated_images=max_generated_images if image_capabilities_enabled else 0,
            max_imported_images=max_imported_images if image_capabilities_enabled else 0,
            max_image_cost_usd=max_image_cost_usd,
            max_web_calls=max_web_calls,
            max_web_cost_usd=max_web_cost_usd,
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
                    "image_capabilities_enabled": image_capabilities_enabled,
                    "image_generation_model": image_generation_model if image_capabilities_enabled else None,
                    "developer": catalog.developer,
                    "reasoning": reasoning_configuration.model_dump(mode="json"),
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
            opening=curator_note,
            once=once,
        )
    )
