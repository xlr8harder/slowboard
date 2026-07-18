"""Narrow Slowboard-owned boundary around Harn's low-level agent loop."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from harn_agent.agent import Agent
from harn_agent.types import AgentContext, AgentLoopTurnUpdate, AgentMessage, AgentTool
from harn_ai.types import Model, TextContent, UserMessage, validate_message
from pydantic import BaseModel, ConfigDict, Field


class EngineSnapshot(BaseModel):
    """Serializable state needed to reconstruct the model-visible agent context."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    context_generation: int = Field(default=0, ge=0)
    system_prompt: str
    model: dict[str, Any]
    messages: list[dict[str, Any]]
    thinking_level: str = "off"
    provider_state: dict[str, Any] = Field(default_factory=dict)


class _SlowboardAgent(Agent):
    """Expose Harn's loop stop hook omitted from its pinned AgentOptions API."""

    def __init__(self, options: dict[str, Any], *, should_stop_after_turn: Callable[[Any], Any] | None) -> None:
        self._slowboard_should_stop_after_turn = should_stop_after_turn
        super().__init__(options)

    def _create_loop_config(self, *, skip_initial_steering_poll: bool = False) -> Any:
        config = super()._create_loop_config(skip_initial_steering_poll=skip_initial_steering_poll)
        config.shouldStopAfterTurn = self._slowboard_should_stop_after_turn
        return config


def _dump_message(message: AgentMessage) -> dict[str, Any]:
    if not hasattr(message, "model_dump"):
        raise TypeError(f"Cannot persist custom Harn message type: {type(message).__name__}")
    return message.model_dump(mode="json", by_alias=True, exclude_none=True)


def _labeled_user_message(label: str, text: str) -> UserMessage:
    return UserMessage(
        content=[TextContent(text=f"[{label}]\n{text}")],
        timestamp=int(time.time() * 1000),
    )


def _curator_message(text: str) -> UserMessage:
    return _labeled_user_message("Curator", text)


def _harness_message(text: str) -> UserMessage:
    return _labeled_user_message("Slowboard harness", text)


class AibbHarnessEngine:
    """Own prompt, tools, provider stream, and reconstruction around Harn Agent."""

    def __init__(
        self,
        *,
        model: Model,
        system_prompt: str,
        tools: list[AgentTool],
        stream_fn: Callable[..., Any],
        messages: list[AgentMessage] | None = None,
        thinking_level: str = "off",
        provider_state: dict[str, Any] | None = None,
        context_generation: int = 0,
        prepare_next_turn: Callable[[AibbHarnessEngine], AgentLoopTurnUpdate | None | Any] | None = None,
        should_stop_after_turn: Callable[[AibbHarnessEngine], bool | Any] | None = None,
    ) -> None:
        self.provider_state = dict(provider_state or {})
        self.context_generation = context_generation

        async def prepare(_signal: Any) -> AgentLoopTurnUpdate | None:
            if prepare_next_turn is None:
                return None
            value = prepare_next_turn(self)
            return await value if inspect.isawaitable(value) else value

        async def should_stop(_context: Any) -> bool:
            if should_stop_after_turn is None:
                return False
            value = should_stop_after_turn(self)
            return bool(await value) if inspect.isawaitable(value) else bool(value)

        self._agent = _SlowboardAgent(
            {
                "initialState": {
                    "systemPrompt": system_prompt,
                    "model": model,
                    "thinkingLevel": thinking_level,
                    "tools": list(tools),
                    "messages": list(messages or []),
                },
                "streamFn": stream_fn,
                "toolExecution": "sequential",
                "steeringMode": "one-at-a-time",
                "followUpMode": "one-at-a-time",
                "maxRetries": 0,
                "prepareNextTurn": prepare if prepare_next_turn is not None else None,
            },
            should_stop_after_turn=should_stop if should_stop_after_turn is not None else None,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: EngineSnapshot,
        *,
        tools: list[AgentTool],
        stream_fn: Callable[..., Any],
        prepare_next_turn: Callable[[AibbHarnessEngine], AgentLoopTurnUpdate | None | Any] | None = None,
        should_stop_after_turn: Callable[[AibbHarnessEngine], bool | Any] | None = None,
    ) -> AibbHarnessEngine:
        return cls(
            model=Model.model_validate(snapshot.model),
            system_prompt=snapshot.system_prompt,
            tools=tools,
            stream_fn=stream_fn,
            messages=[validate_message(message) for message in snapshot.messages],
            thinking_level=snapshot.thinking_level,
            provider_state=snapshot.provider_state,
            context_generation=snapshot.context_generation,
            prepare_next_turn=prepare_next_turn,
            should_stop_after_turn=should_stop_after_turn,
        )

    @property
    def messages(self) -> list[AgentMessage]:
        return list(self._agent.state.messages)

    @property
    def agent(self) -> Agent:
        """Expose the pinned low-level engine for event subscription only."""

        return self._agent

    async def send_curator_message(self, text: str) -> None:
        await self._agent.prompt(_curator_message(text))

    async def send_harness_message(self, text: str) -> None:
        """Send a versioned, automatically generated operational message."""

        await self._agent.prompt(_harness_message(text))

    async def begin(self) -> None:
        """Begin from a preinstalled non-assistant context message without adding text."""

        await self._agent.continue_()

    def steer(self, text: str) -> None:
        self._agent.steer(_curator_message(text))

    def follow_up(self, text: str) -> None:
        self._agent.followUp(_curator_message(text))

    def replace_model_visible_context(self, snapshot: EngineSnapshot) -> AgentLoopTurnUpdate:
        """Install a recorded context transition at a safe Harn turn boundary."""

        if snapshot.model.get("id") != self._agent.state.model.id:
            raise ValueError("A context transition cannot change the bound model")
        if snapshot.system_prompt != self._agent.state.systemPrompt:
            raise ValueError("A context transition cannot change the system prompt")
        messages = [validate_message(message) for message in snapshot.messages]
        self._agent.state.messages = messages
        self.context_generation = snapshot.context_generation
        return AgentLoopTurnUpdate(
            context=AgentContext(
                systemPrompt=self._agent.state.systemPrompt,
                messages=list(messages),
                tools=list(self._agent.state.tools),
            )
        )

    def snapshot(self) -> EngineSnapshot:
        state = self._agent.state
        return EngineSnapshot(
            context_generation=self.context_generation,
            system_prompt=state.systemPrompt,
            model=state.model.model_dump(mode="json", by_alias=True, exclude_none=True),
            messages=[_dump_message(message) for message in state.messages],
            thinking_level=state.thinkingLevel,
            provider_state=self.provider_state,
        )
