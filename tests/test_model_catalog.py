from aibb.harness.catalog import OpenRouterModelRecord


def _record(reasoning: dict[str, object] | None) -> OpenRouterModelRecord:
    return OpenRouterModelRecord(
        id="example/model",
        name="Example Labs: Model",
        context_length=100_000,
        pricing={"prompt": "0.000001", "completion": "0.000002"},
        architecture={"input_modalities": ["text", "image"]},
        supported_parameters=["tools", "reasoning"],
        reasoning=reasoning,
    )


def test_catalog_selects_high_reasoning_without_starving_the_visible_answer() -> None:
    record = _record(
        {
            "mandatory": False,
            "default_enabled": False,
            "default_effort": "medium",
            "supported_efforts": ["max", "xhigh", "high", "medium", "low"],
        }
    )

    selected = record.select_reasoning()

    assert record.developer == "Example Labs"
    assert selected.enabled is True
    assert selected.selected_effort == "high"
    assert selected.request_parameter == {"effort": "high", "exclude": False}


def test_non_reasoning_catalog_record_does_not_invent_a_mode() -> None:
    selected = _record(None).select_reasoning()

    assert selected.enabled is False
    assert selected.request_parameter is None
    assert selected.source == "unavailable"


def test_probe_informed_mandatory_reasoning_override_is_explicit() -> None:
    selected = _record(None).select_reasoning("mandatory")

    assert selected.enabled is True
    assert selected.mandatory is True
    assert selected.selected_effort is None
    assert selected.request_parameter == {"enabled": True}
    assert selected.source == "curator-override"


def test_provider_context_ceiling_clamps_model_catalog_maximum() -> None:
    record = _record(None).model_copy(
        update={
            "context_length": 1_048_576,
            "top_provider": {"context_length": 524_288, "max_completion_tokens": None},
        }
    )

    assert record.effective_context_length == 524_288
    assert record.clamp_output_tokens(600_000) == 520_192


def test_missing_provider_context_uses_model_catalog_maximum() -> None:
    record = _record(None)

    assert record.effective_context_length == 100_000
