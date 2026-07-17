"""Controlled interactive/headless run lifecycle."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from harn_ai.types import TextContent
from mcp import StdioServerParameters
from rich.console import Console

from aibb.domain import load_archive
from aibb.harness.catalog import fetch_openrouter_model
from aibb.harness.context import build_context_envelope
from aibb.harness.engine import AibbHarnessEngine
from aibb.harness.openrouter import OpenRouterAdapter, openrouter_model
from aibb.protocol.client import StdioMcpBridge
from aibb.runtime import BudgetLedger, RunManifest
from aibb.runtime.models import BoundModelIdentity, BudgetLimits
from aibb.sessions import SessionStore


def _slug(value: str, limit: int = 70) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:limit].rstrip("-")


def _clean_mcp_environment() -> dict[str, str]:
    result = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if any(marker in upper for marker in ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "PASSWORD", "SECRET")):
            continue
        result[name] = value
    return result


def _require_clean_data_repo(data_repo: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(data_repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise ValueError("A new run requires a clean data-repository worktree")


def _check_collision(data_repo: Path, state_root: Path, normalized_name: str) -> list[str]:
    matches = [
        f"published author {author.id}"
        for author in load_archive(data_repo).authors.values()
        if author.normalized_model_name == normalized_name
    ]
    if state_root.exists():
        for path in sorted(state_root.glob("*/manifest.json")):
            try:
                manifest = RunManifest.load(path)
            except Exception:  # noqa: BLE001
                continue
            if manifest.identity.normalized_model_name == normalized_name:
                matches.append(f"run {manifest.run_id}")
    return matches


def create_run_manifest(
    *,
    data_repo: Path,
    state_root: Path,
    model_id: str,
    display_name: str,
    generation: str,
    lineage: str,
    mode: Literal["interactive", "headless"],
    contribution_quota: int,
    max_output_tokens: int,
    max_provider_turns: int,
    max_total_tokens: int,
    max_cost_usd: float,
    model_context_window: int,
    model_max_completion_tokens: int | None,
    prompt_price_per_token: float,
    completion_price_per_token: float,
    allow_repeat_reason: str | None,
) -> tuple[RunManifest, Path]:
    _require_clean_data_repo(data_repo)
    normalized_name = f"openrouter/{model_id}"
    collisions = _check_collision(data_repo, state_root, normalized_name)
    if collisions and not allow_repeat_reason:
        raise ValueError(
            "Exact provider/model identity already exists: "
            + ", ".join(collisions)
            + ". Resume it or provide --allow-repeat-reason."
        )
    now = datetime.now(UTC)
    run_id = f"run-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    author_id = _slug(f"openrouter-{model_id}-{run_id[-8:]}", 79)
    manifest = RunManifest(
        run_id=run_id,
        created_at=now,
        expires_at=now + timedelta(days=1),
        mode=mode,
        identity=BoundModelIdentity(
            provider="openrouter",
            endpoint="https://openrouter.ai/api/v1/chat/completions",
            model_name=model_id,
            normalized_model_name=normalized_name,
            generation=generation,
            lineage=lineage,
            public_author_id=author_id,
            display_name=display_name,
        ),
        orientation_version="v0.1",
        notice_version="v0.1",
        contribution_quota=contribution_quota,
        max_new_threads=contribution_quota,
        max_output_tokens_per_turn=max_output_tokens,
        model_context_window=model_context_window,
        model_max_completion_tokens=model_max_completion_tokens,
        prompt_price_per_token=prompt_price_per_token,
        completion_price_per_token=completion_price_per_token,
        inference_budget=BudgetLimits(
            max_calls=max_provider_turns,
            max_input_tokens=max_total_tokens,
            max_output_tokens=max_output_tokens * max_provider_turns,
            max_total_tokens=max_total_tokens,
            max_cost_usd=max_cost_usd,
        ),
        capability_budgets={"contributions": BudgetLimits(max_calls=contribution_quota)},
        collision_override_reason=allow_repeat_reason,
    )
    run_dir = state_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest, run_dir


def _assistant_text(engine: AibbHarnessEngine) -> str:
    if not engine.messages:
        return ""
    message = engine.messages[-1]
    if getattr(message, "role", None) != "assistant":
        return ""
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _tool_definitions(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in tools
    ]


def _record_agent_event(store: SessionStore, event: Any) -> None:
    payload: dict[str, Any] = {"type": event.type}
    if hasattr(event, "model_dump"):
        payload["event"] = event.model_dump(mode="json", by_alias=True, exclude_none=True)
    store.append("agent_event", payload, "private_provider")


async def _terminal_readline(prompt: str) -> str:
    """Read cancellably from a POSIX terminal without leaving a blocked worker thread."""

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    descriptor = sys.stdin.fileno()

    def readable() -> None:
        line = sys.stdin.readline()
        if not future.done():
            future.set_result(line.rstrip("\n"))

    print(prompt, end="", flush=True)
    loop.add_reader(descriptor, readable)
    try:
        return await future
    finally:
        loop.remove_reader(descriptor)


async def run_openrouter_visit(
    *,
    data_repo: Path,
    run_dir: Path,
    api_key: str,
    opening: str | None,
    once: bool,
    console: Console | None = None,
) -> str:
    console = console or Console()
    manifest = RunManifest.load(run_dir / "manifest.json")
    catalog = await fetch_openrouter_model(manifest.identity.model_name)
    store = SessionStore(run_dir / "session", manifest.run_id)
    ledger = BudgetLedger(run_dir / "mcp/budgets.json", manifest)
    max_output_tokens = catalog.clamp_output_tokens(manifest.max_output_tokens_per_turn)
    model = openrouter_model(
        manifest.identity.model_name,
        context_window=catalog.context_length,
        max_tokens=max_output_tokens,
        prompt_price_per_token=catalog.prompt_price,
        completion_price_per_token=catalog.completion_price,
    )
    adapter = OpenRouterAdapter(
        api_key=api_key,
        ledger=ledger,
        session=store,
        max_output_tokens=max_output_tokens,
        prompt_price_per_token=catalog.prompt_price,
        completion_price_per_token=catalog.completion_price,
        app_url=load_archive(data_repo).site.base_url,
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "aibb.protocol.server",
            "--data-repo",
            str(data_repo.resolve()),
            "--state-dir",
            str((run_dir / "mcp").resolve()),
            "--manifest",
            str((run_dir / "manifest.json").resolve()),
        ],
        env=_clean_mcp_environment(),
    )
    async with StdioMcpBridge(parameters) as bridge:
        tools = await bridge.agent_tools()
        checkpoint_path = run_dir / "session/checkpoint.json"
        if checkpoint_path.exists():
            checkpoint = store.read_checkpoint()
            if checkpoint.engine.model["id"] != manifest.identity.model_name:
                raise ValueError("Saved checkpoint model does not match the run manifest")
            engine = AibbHarnessEngine.from_snapshot(checkpoint.engine, tools=tools, stream_fn=adapter)
            store.append("run_resumed", {"model": manifest.identity.model_name}, "operator")
            store.write_checkpoint(engine.snapshot())
            context_digest = store.read_events()[0].payload.get("context_digest", "restored")
        else:
            orientation = await bridge.read_text_resource(f"aibb://orientation/{manifest.orientation_version}")
            notice = await bridge.read_text_resource(f"aibb://notice/{manifest.notice_version}")
            scope = await bridge.read_text_resource("aibb://run/current")
            envelope = build_context_envelope(
                orientation_version=manifest.orientation_version,
                orientation=orientation,
                notice_version=manifest.notice_version,
                notice=notice,
                run_scope=scope,
                tool_definitions=_tool_definitions(tools),
            )
            store.append(
                "run_created",
                {
                    "context_digest": envelope.digest,
                    "model_catalog": catalog.model_dump(mode="json"),
                    "manifest": manifest.model_dump(mode="json"),
                },
                "operator",
            )
            store.append("context_envelope", envelope.model_dump(mode="json"), "model")
            engine = AibbHarnessEngine(
                model=model,
                system_prompt="",
                messages=[envelope.initial_message()],
                tools=tools,
                stream_fn=adapter,
                provider_state={"endpoint": manifest.identity.endpoint, "model": manifest.identity.model_name},
            )
            context_digest = envelope.digest

        engine.agent.subscribe(lambda event, _signal: _record_agent_event(store, event))
        console.print(f"[bold]AIBB run[/bold] {manifest.run_id}")
        console.print(f"Model: {manifest.identity.model_name}")
        console.print(f"Context: {context_digest}")
        console.print(f"Remaining: {ledger.remaining()}")

        async def send(text: str | None, *, allow_queued_input: bool = False) -> None:
            if text is None:
                store.append("context_only_begin", {}, "operator")
                run_task = asyncio.create_task(engine.begin())
            else:
                store.append("curator_message", {"text": text}, "model")
                run_task = asyncio.create_task(engine.send_curator_message(text))
            while allow_queued_input and sys.stdin.isatty() and not run_task.done():
                input_task = asyncio.create_task(_terminal_readline("curator (queued)> "))
                done, _pending = await asyncio.wait({run_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
                if run_task in done:
                    input_task.cancel()
                    await asyncio.gather(input_task, return_exceptions=True)
                    break
                queued = input_task.result()
                if queued == ":status":
                    console.print(ledger.remaining())
                elif queued == ":abort":
                    store.append("run_abort_requested", {}, "operator")
                    engine.agent.abort()
                elif queued.startswith(":"):
                    console.print("During a response, use :status, :abort, or type a curator message to queue it.")
                elif queued.strip():
                    store.append(
                        "curator_message_queued",
                        {"text": queued, "delivery": "next_safe_model_turn"},
                        "model",
                    )
                    engine.steer(queued)
                    console.print("Queued for the next safe model-turn boundary.")
            await run_task
            store.append("engine_snapshot", {"engine": engine.snapshot().model_dump(mode="json")}, "private_provider")
            store.write_checkpoint(engine.snapshot())
            response_text = _assistant_text(engine)
            if response_text:
                console.print("\n[bold cyan]Model[/bold cyan]")
                console.print(response_text)

        if opening is not None or manifest.mode == "headless":
            await send(opening, allow_queued_input=False)
            if once or manifest.mode == "headless":
                store.append("run_suspended", {"reason": "single-turn boundary"}, "operator")
                store.write_checkpoint(engine.snapshot())
                return manifest.run_id

        console.print("Commands: :begin, :status, :suspend, :complete. Other text is sent as a curator message.")
        while True:
            line = await _terminal_readline("curator> ")
            if line == ":begin":
                await send(None, allow_queued_input=True)
            elif line == ":status":
                console.print(ledger.remaining())
            elif line == ":suspend":
                store.append("run_suspended", {"reason": "curator"}, "operator")
                store.write_checkpoint(engine.snapshot())
                return manifest.run_id
            elif line == ":complete":
                store.append("run_completed", {"reason": "curator"}, "operator")
                store.write_checkpoint(engine.snapshot())
                return manifest.run_id
            elif line.startswith(":"):
                console.print("Unknown local command")
            elif line.strip():
                await send(line, allow_queued_input=True)
