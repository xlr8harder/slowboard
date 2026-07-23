"""Budgeted image creation and safe import into private run state."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import httpx
import mcp.types as types
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from aibb.domain.models import ImageAttachment
from aibb.protocol.world import WorldCapabilityError, validate_public_url
from aibb.runtime import BudgetLedger, RunManifest
from aibb.runtime.budget import Usage

IMAGE_ENDPOINT = "https://openrouter.ai/api/v1/images"
MAX_IMAGE_BYTES = 16_000_000
MAX_IMAGE_PIXELS = 16_000_000
ALLOWED_INPUT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ImageCapabilityError(ValueError):
    """A safe contributor-facing image capability error."""


class StagedImageAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    id: str = Field(pattern=r"^image-[a-f0-9]{16}$")
    run_id: str
    created_at: datetime
    source: Literal["generated", "imported"]
    path: str = Field(pattern=r"^images/image-[a-f0-9]{16}\.webp$")
    media_type: Literal["image/webp"] = "image/webp"
    width: int = Field(ge=1, le=8192)
    height: int = Field(ge=1, le=8192)
    byte_size: int = Field(ge=1, le=MAX_IMAGE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt: str | None = Field(default=None, max_length=4000)
    generator_model: str | None = Field(default=None, max_length=240)
    source_url: str | None = Field(default=None, max_length=2048)
    presented_to_author: bool = False

    def public_attachment(self, *, alt_text: str, caption: str | None) -> ImageAttachment:
        return ImageAttachment(
            id=self.id,
            path=f"assets/images/{self.sha256}.webp",
            width=self.width,
            height=self.height,
            byte_size=self.byte_size,
            sha256=self.sha256,
            alt_text=alt_text,
            caption=caption,
            source=self.source,
            prompt=self.prompt,
            generator_model=self.generator_model,
            source_url=self.source_url,
            presented_to_author=self.presented_to_author,
        )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    _atomic_bytes(path, value.encode("utf-8"))


def normalize_image(raw: bytes) -> tuple[bytes, int, int]:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ImageCapabilityError(f"Image input must be between 1 and {MAX_IMAGE_BYTES} bytes")
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.format not in {"JPEG", "PNG", "WEBP"}:
                raise ImageCapabilityError("Only JPEG, PNG, and WebP image inputs are supported")
            width, height = opened.size
            if width < 1 or height < 1 or width > 8192 or height > 8192 or width * height > MAX_IMAGE_PIXELS:
                raise ImageCapabilityError("Image dimensions exceed the safe 16-megapixel, 8192-pixel-edge limit")
            image = ImageOps.exif_transpose(opened).convert("RGBA" if opened.mode in {"RGBA", "LA"} else "RGB")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=92, method=6, exact=True)
    except (OSError, UnidentifiedImageError) as error:
        raise ImageCapabilityError("Image data could not be decoded safely") from error
    normalized = output.getvalue()
    if len(normalized) > MAX_IMAGE_BYTES:
        raise ImageCapabilityError("Normalized image exceeds the 16 MB attachment limit")
    return normalized, image.width, image.height


def load_staged_image(state_dir: Path, run_id: str, asset_id: str) -> tuple[StagedImageAsset, Path]:
    if not asset_id.startswith("image-") or len(asset_id) != 22 or not asset_id[6:].isalnum():
        raise ImageCapabilityError("Invalid image asset ID")
    metadata_path = state_dir.resolve() / "images" / f"{asset_id}.json"
    try:
        asset = StagedImageAsset.model_validate_json(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ImageCapabilityError(f"Unknown staged image: {asset_id}") from error
    if asset.run_id != run_id:
        raise ImageCapabilityError("Staged image belongs to a different run")
    binary_path = state_dir.resolve() / asset.path
    try:
        raw = binary_path.read_bytes()
    except FileNotFoundError as error:
        raise ImageCapabilityError(f"Staged image data is missing: {asset_id}") from error
    if len(raw) != asset.byte_size or hashlib.sha256(raw).hexdigest() != asset.sha256:
        raise ImageCapabilityError(f"Staged image failed integrity validation: {asset_id}")
    return asset, binary_path


class ImageCapabilityState:
    def __init__(
        self,
        state_dir: Path,
        manifest: RunManifest,
        *,
        openrouter_api_key: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
    ) -> None:
        self.state_dir = state_dir.resolve()
        self.images_dir = self.state_dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest
        self.openrouter_api_key = openrouter_api_key
        self.transport = transport
        self.resolver = resolver
        self.ledger = BudgetLedger(self.state_dir / "budgets.json", manifest)
        self.log_path = self.state_dir / "image-events.jsonl"

    @property
    def enabled(self) -> set[str]:
        if not self.manifest.image_capabilities_enabled or not self.manifest.image_input_supported:
            return set()
        names = set()
        if (
            "generate_image" in self.manifest.capability_budgets
            and self.manifest.image_generation_model
            and self.openrouter_api_key
        ):
            names.add("generate_image")
        if "import_image" in self.manifest.capability_budgets:
            names.add("import_image")
        return names

    def _append_log(self, event: dict[str, object]) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": self.manifest.run_id,
            **event,
        }
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _per_call_limit(self, capability: str, field: str, default: int | float) -> int | float:
        limits = self.manifest.capability_budgets[capability]
        value = getattr(limits, field)
        calls = limits.max_calls or 1
        return default if value is None else value / calls

    def _stage(
        self,
        raw: bytes,
        *,
        source: Literal["generated", "imported"],
        prompt: str | None = None,
        generator_model: str | None = None,
        source_url: str | None = None,
    ) -> StagedImageAsset:
        normalized, width, height = normalize_image(raw)
        digest = hashlib.sha256(normalized).hexdigest()
        asset_id = f"image-{uuid.uuid4().hex[:16]}"
        binary_path = self.images_dir / f"{asset_id}.webp"
        asset = StagedImageAsset(
            id=asset_id,
            run_id=self.manifest.run_id,
            created_at=datetime.now(UTC),
            source=source,
            path=str(binary_path.relative_to(self.state_dir)),
            width=width,
            height=height,
            byte_size=len(normalized),
            sha256=digest,
            prompt=prompt,
            generator_model=generator_model,
            source_url=source_url,
            presented_to_author=self.manifest.image_input_supported,
        )
        _atomic_bytes(binary_path, normalized)
        _atomic_text(self.images_dir / f"{asset_id}.json", asset.model_dump_json(indent=2) + "\n")
        return asset

    def _tool_result(self, asset: StagedImageAsset) -> types.CallToolResult:
        payload = {
            "asset": asset.model_dump(mode="json", exclude={"path"}, exclude_none=True),
            "attach_with": {
                "asset_id": asset.id,
                "alt_text": "Describe the image for readers who cannot see it.",
                "caption": "Optional public caption.",
            },
            "visual_access": (
                "The image is included below for your visual inspection."
                if asset.presented_to_author
                else "This model endpoint does not advertise image input; the image is staged but not shown visually."
            ),
            "unpublished": True,
        }
        content: list[types.TextContent | types.ImageContent] = [
            types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        ]
        if asset.presented_to_author:
            _, path = load_staged_image(self.state_dir, self.manifest.run_id, asset.id)
            content.append(
                types.ImageContent(
                    type="image",
                    data=base64.b64encode(path.read_bytes()).decode("ascii"),
                    mimeType="image/webp",
                )
            )
        return types.CallToolResult(content=content, structuredContent=payload)

    async def generate(self, prompt: str, aspect_ratio: str | None = None) -> types.CallToolResult:
        if "generate_image" not in self.enabled:
            raise ImageCapabilityError("Image generation is not enabled for this run")
        if not prompt.strip():
            raise ImageCapabilityError("generate_image requires a non-empty prompt")
        if not self.openrouter_api_key:
            raise ImageCapabilityError(
                "Image generation is unavailable because its operator credential is not configured"
            )
        payload: dict[str, object] = {
            "model": self.manifest.image_generation_model,
            "prompt": prompt,
            "n": 1,
            "output_format": "webp",
        }
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        request_bytes = len(_canonical_json(payload).encode("utf-8"))
        key = f"generate-image-{uuid.uuid4().hex}"
        reserved_cost = float(self._per_call_limit("generate_image", "max_cost_usd", 2.0))
        requested = Usage(
            calls=1,
            cost_usd=reserved_cost,
            request_bytes=request_bytes,
            result_bytes=int(self._per_call_limit("generate_image", "max_result_bytes", MAX_IMAGE_BYTES)),
        )
        self.ledger.reserve("generate_image", key, requested)
        self._append_log(
            {
                "type": "generation_requested",
                "reservation_key": key,
                "model": self.manifest.image_generation_model or "",
                "prompt": prompt,
                "aspect_ratio": aspect_ratio or "",
            }
        )
        try:
            headers = {
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "Content-Type": "application/json",
                "X-Title": f"{self.manifest.archive_title or 'Slowboard'} image capability",
            }
            if self.manifest.archive_base_url:
                headers["HTTP-Referer"] = self.manifest.archive_base_url
            async with httpx.AsyncClient(timeout=300, transport=self.transport) as client:
                response = await client.post(IMAGE_ENDPOINT, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            encoded = result["data"][0]["b64_json"]
            raw = base64.b64decode(encoded, validate=True)
            asset = self._stage(
                raw,
                source="generated",
                prompt=prompt,
                generator_model=self.manifest.image_generation_model,
            )
            usage = result.get("usage") or {}
            actual_cost = float(usage.get("cost") or reserved_cost)
            self.ledger.reconcile(
                "generate_image",
                key,
                Usage(calls=1, cost_usd=actual_cost, request_bytes=request_bytes, result_bytes=len(raw)),
            )
            self._append_log(
                {
                    "type": "generation_completed",
                    "reservation_key": key,
                    "asset_id": asset.id,
                    "sha256": asset.sha256,
                    "cost_usd": actual_cost,
                }
            )
            return self._tool_result(asset)
        except Exception:
            account = self.ledger.read().accounts["generate_image"]
            if key in account.reservations:
                self.ledger.reconcile("generate_image", key, Usage())
            raise

    async def import_url(self, url: str) -> types.CallToolResult:
        if "import_image" not in self.enabled:
            raise ImageCapabilityError("Image import is not enabled for this run")
        key = f"import-image-{uuid.uuid4().hex}"
        request_bytes = len(url.encode("utf-8"))
        requested = Usage(
            calls=1,
            request_bytes=request_bytes,
            result_bytes=int(self._per_call_limit("import_image", "max_result_bytes", MAX_IMAGE_BYTES)),
        )
        self.ledger.reserve("import_image", key, requested)
        self._append_log({"type": "import_requested", "reservation_key": key, "url": url})
        try:
            try:
                current = validate_public_url(url, **({"resolver": self.resolver} if self.resolver else {}))
            except WorldCapabilityError as error:
                raise ImageCapabilityError(
                    str(error).replace("fetch_public_url accepts", "import_public_image accepts")
                ) from error
            raw = b""
            content_type = ""
            async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
                for _ in range(6):
                    async with client.stream("GET", current, follow_redirects=False) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise ImageCapabilityError("Image URL redirected without a destination")
                            try:
                                current = validate_public_url(
                                    urljoin(current, location),
                                    **({"resolver": self.resolver} if self.resolver else {}),
                                )
                            except WorldCapabilityError as error:
                                raise ImageCapabilityError(str(error)) from error
                            continue
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
                        if content_type not in ALLOWED_INPUT_TYPES:
                            raise ImageCapabilityError("Remote image must be JPEG, PNG, or WebP")
                        chunks = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_IMAGE_BYTES:
                                raise ImageCapabilityError("Remote image exceeds the 16 MB import limit")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        break
                else:
                    raise ImageCapabilityError("Image URL exceeded the redirect limit")
            if not raw:
                raise ImageCapabilityError("Remote image response was empty")
            asset = self._stage(raw, source="imported", source_url=current)
            self.ledger.reconcile(
                "import_image",
                key,
                Usage(calls=1, request_bytes=request_bytes, result_bytes=len(raw)),
            )
            self._append_log(
                {
                    "type": "import_completed",
                    "reservation_key": key,
                    "asset_id": asset.id,
                    "sha256": asset.sha256,
                    "media_type": content_type,
                    "resolved_url": current,
                }
            )
            return self._tool_result(asset)
        except Exception:
            account = self.ledger.read().accounts["import_image"]
            if key in account.reservations:
                self.ledger.reconcile("import_image", key, Usage())
            raise
