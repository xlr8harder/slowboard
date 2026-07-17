"""Narrow Slowboard-owned boundary around Harn's low-level agent loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from harn_agent.agent import Agent
from harn_agent.types import AgentMessage, AgentTool
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


def _dump_message(message: AgentMessage) -> dict[str, Any]:
    if not hasattr(message, "model_dump"):
        raise TypeError(f"Cannot persist custom Harn message type: {type(message).__name__}")
    return message.model_dump(mode="json", by_alias=True, exclude_none=True)


def _curator_message(text: str) -> UserMessage:
    return UserMessage(
        content=[TextContent(text=f"[Curator]\n{text}")],
        timestamp=int(time.time() * 1000),
    )


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
    ) -> None:
        self.provider_state = dict(provider_state or {})
        self.context_generation = context_generation
        self._agent = Agent(
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
            }
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: EngineSnapshot,
        *,
        tools: list[AgentTool],
        stream_fn: Callable[..., Any],
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

    async def begin(self) -> None:
        """Begin from a preinstalled non-assistant context message without adding text."""

        await self._agent.continue_()

    def steer(self, text: str) -> None:
        self._agent.steer(_curator_message(text))

    def follow_up(self, text: str) -> None:
        self._agent.followUp(_curator_message(text))

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
