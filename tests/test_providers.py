import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from app.catalog import model_spec, seed_definitions
from app.models import VideoModel
from app.providers.base import ProviderError, Submission, VideoRequest
from app.providers.fal import MAX_JSON_BYTES, FalProvider, validate_media_url
from pydantic import SecretStr, ValidationError

REQUEST_ID = "38cf4064-3051-4b3c-9589-09b190cc175f"
FAKE_KEY = "fake-test-key-not-a-real-credential"


def run(awaitable):
    return asyncio.run(awaitable)


def model(profile="wan27_t2v"):
    return model_spec(
        VideoModel(
            **next(
                definition
                for definition in seed_definitions()
                if definition["pricing_details"]["adapter_profile"] == profile
            )
        )
    )


def request(spec, **overrides):
    values = dict(
        model_id=spec.model_id,
        prompt="A calm sea at dawn",
        duration_seconds=5,
        resolution="720p" if spec.adapter_profile.startswith("wan") else "1080p",
        aspect_ratio="16:9",
        audio_enabled=spec.adapter_profile.startswith("wan"),
    )
    values.update(overrides)
    return VideoRequest(**values)


def submission(spec):
    owner, app = spec.endpoint_id.split("/")[:2]
    base = f"https://queue.fal.run/{owner}/{app}/requests/{REQUEST_ID}"
    return Submission(
        request_id=REQUEST_ID,
        status_url=base + "/status",
        response_url=base,
        cancel_url=base + "/cancel",
    )


@pytest.fixture
def provider_factory():
    clients = []
    calls = []

    def factory(handler=None, key=FAKE_KEY):
        def capture(incoming):
            calls.append(incoming)
            return handler(incoming) if handler else httpx.Response(500)

        client = httpx.AsyncClient(transport=httpx.MockTransport(capture), follow_redirects=True)
        clients.append(client)
        return FalProvider(SecretStr(key), client), calls

    yield factory
    for client in clients:
        run(client.aclose())


@pytest.mark.parametrize("profile", ["wan27_t2v", "kling3pro_t2v"])
def test_paid_submission_uses_selected_endpoint_and_profile_types(provider_factory, profile):
    spec = model(profile)
    expected = submission(spec)
    provider, calls = provider_factory(
        lambda incoming: httpx.Response(
            200, json={**expected.model_dump(), "status": "IN_QUEUE", "queue_position": 0}
        )
    )
    result = run(provider.submit_generation(spec, request(spec), paid_authorized=True))
    assert result == expected
    assert len(calls) == 1
    call = calls[0]
    assert str(call.url) == "https://queue.fal.run/" + spec.endpoint_id
    assert call.headers["Authorization"] == "Key " + FAKE_KEY
    assert call.headers["X-Fal-No-Retry"] == "1"
    assert call.headers["X-Fal-Queue-Priority"] == "low"
    payload = json.loads(call.content)
    assert payload["enable_safety_checker"] is True
    if profile.startswith("wan"):
        assert payload["duration"] == 5
        assert payload["resolution"] == "720p"
        assert "generate_audio" not in payload
    else:
        assert payload["duration"] == "5"
        assert payload["generate_audio"] is False
        assert "resolution" not in payload


def test_no_paid_call_without_explicit_authorization_or_key(provider_factory):
    spec = model()
    provider, calls = provider_factory()
    with pytest.raises(ProviderError, match="paid_generation_not_authorized"):
        run(provider.submit_generation(spec, request(spec)))
    assert calls == []
    provider, calls = provider_factory(key="")
    with pytest.raises(ProviderError, match="provider_not_configured"):
        run(provider.submit_generation(spec, request(spec), paid_authorized=True))
    assert calls == []


@pytest.mark.parametrize(
    "profile,changes,code",
    [
        ("wan27_t2v", {"audio_enabled": False}, "native_audio_cannot_be_disabled"),
        ("wan27_t2v", {"resolution": "4k"}, "unsupported_resolution"),
        ("wan27_t2v", {"duration_seconds": 1}, "unsupported_duration"),
        ("wan27_t2v", {"duration_seconds": 16}, "unsupported_duration"),
        ("wan27_t2v", {"mode": "image_to_video"}, "unsupported_generation_mode"),
        ("wan27_t2v", {"negative_prompt": "x" * 501}, "negative_prompt_too_long"),
        (
            "wan27_t2v",
            {"reference_image_url": "https://v3.fal.media/a.png"},
            "reference_image_not_supported",
        ),
        ("kling3pro_t2v", {"resolution": "720p"}, "unsupported_resolution"),
        ("kling3pro_t2v", {"duration_seconds": 2}, "unsupported_duration"),
        ("kling3pro_t2v", {"aspect_ratio": "4:3"}, "unsupported_aspect_ratio"),
        ("kling3pro_t2v", {"prompt": "x" * 2501}, "prompt_too_long"),
    ],
)
def test_unsupported_model_combinations_rejected_without_http(
    provider_factory, profile, changes, code
):
    spec = model(profile)
    provider, calls = provider_factory()
    with pytest.raises(ProviderError, match=code):
        run(provider.validate_request(spec, request(spec, **changes)))
    assert calls == []


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"enabled": False}, "model_disabled"),
        ({"provider": "unimplemented"}, "unsupported_model_profile"),
        ({"endpoint_id": "https://metadata.invalid/latest"}, "unsupported_model_profile"),
    ],
)
def test_model_registry_cannot_inject_unimplemented_provider_urls(provider_factory, changes, code):
    original = model()
    provider, calls = provider_factory()
    with pytest.raises(ProviderError, match=code):
        run(provider.validate_request(replace(original, **changes), request(original)))
    assert calls == []


@pytest.mark.parametrize(
    "profile,image_field",
    [
        ("wan27_i2v", "image_url"),
        ("kling3pro_i2v", "start_image_url"),
    ],
)
def test_internal_image_translation_never_sends_ignored_aspect_parameter(
    provider_factory, profile, image_field
):
    spec = replace(model(profile), enabled=True)
    provider, calls = provider_factory()
    payload = run(
        provider.validate_request(
            spec,
            request(
                spec,
                mode="image_to_video",
                reference_image_url="https://v3.fal.media/owned-reference.png",
            ),
        )
    )
    assert payload[image_field].endswith("owned-reference.png")
    assert "aspect_ratio" not in payload
    assert calls == []


@pytest.mark.parametrize(
    "profile,resolution,audio,expected",
    [
        ("wan27_t2v", "720p", True, "0.500000"),
        ("wan27_t2v", "1080p", True, "0.750000"),
        ("kling3pro_t2v", "1080p", False, "0.560000"),
        ("kling3pro_t2v", "1080p", True, "0.840000"),
    ],
)
def test_public_price_quotes_are_decimal_and_explicitly_unverified(
    provider_factory, profile, resolution, audio, expected
):
    spec = model(profile)
    provider, calls = provider_factory()
    quote = run(
        provider.estimate_cost(spec, request(spec, resolution=resolution, audio_enabled=audio))
    )
    assert quote.amount_usd == Decimal(expected)
    assert quote.verified is False
    assert quote.source == "public_model_page"
    assert calls == []


@pytest.mark.parametrize("price", [None, Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_unknown_or_invalid_price_never_becomes_zero(provider_factory, price):
    spec = replace(model(), price_usd=price)
    provider, _ = provider_factory()
    quote = run(provider.estimate_cost(spec, request(spec)))
    assert quote.amount_usd is None and not quote.verified


def test_editing_verification_flag_does_not_verify_public_variant_multipliers(provider_factory):
    spec = model()
    spec = replace(spec, pricing={**spec.pricing, "account_pricing_verified": True})
    provider, _ = provider_factory()
    assert not run(provider.estimate_cost(spec, request(spec))).verified


@pytest.mark.parametrize(
    "status,error_type,expected",
    [
        ("IN_QUEUE", None, "queued"),
        ("IN_PROGRESS", None, "running"),
        ("COMPLETED", None, "completed"),
        ("COMPLETED", "content_policy_violation", "failed"),
        ("COMPLETED", "request_cancelled", "cancelled"),
    ],
)
def test_status_does_not_treat_completed_failure_as_success(
    provider_factory, status, error_type, expected
):
    spec = model()
    body = {"request_id": REQUEST_ID, "status": status}
    if error_type:
        body.update(error_type=error_type, error="raw-error-with-secret-marker")
    provider, _ = provider_factory(lambda incoming: httpx.Response(200, json=body))
    result = run(provider.get_generation_status(spec, submission(spec)))
    assert result.state == expected
    assert "raw-error" not in str(result)


@pytest.mark.parametrize(
    "code,body,expected,stopped",
    [
        (200, {"status": "OK"}, "cancelled", True),
        (202, {"status": "CANCELLATION_REQUESTED"}, "cancellation_requested", False),
        (400, {"status": "ALREADY_COMPLETED"}, "already_completed", False),
        (404, {"status": "NOT_FOUND"}, "reconciliation_required", False),
        (200, {"success": True}, "reconciliation_required", False),
    ],
)
def test_cancellation_only_confirms_documented_queue_removal(
    provider_factory, code, body, expected, stopped
):
    spec = model()
    provider, calls = provider_factory(lambda incoming: httpx.Response(code, json=body))
    result = run(provider.cancel_generation(spec, submission(spec)))
    assert result.state == expected and result.confirmed_stopped is stopped
    assert calls[0].method == "PUT"


def test_submit_timeout_is_ambiguous_and_never_retried(provider_factory):
    spec = model()

    def timeout(incoming):
        raise httpx.ReadTimeout("sensitive-provider-url-and-key", request=incoming)

    provider, calls = provider_factory(timeout)
    with pytest.raises(ProviderError) as error:
        run(provider.submit_generation(spec, request(spec), paid_authorized=True))
    assert error.value.ambiguous and not error.value.retryable
    assert str(error.value) == "provider_transport_error"
    assert "sensitive" not in repr(error.value)
    assert len(calls) == 1


def test_poll_failure_is_retryable_without_submitting_again(provider_factory):
    spec = model()
    provider, calls = provider_factory(lambda incoming: httpx.Response(503, text=FAKE_KEY))
    with pytest.raises(ProviderError) as error:
        run(provider.get_generation_status(spec, submission(spec)))
    assert error.value.retryable and not error.value.ambiguous
    assert FAKE_KEY not in str(error.value)
    assert len(calls) == 1 and calls[0].method == "GET"


def test_provider_redirect_never_leaks_authorization(provider_factory):
    spec = model()
    provider, calls = provider_factory(
        lambda incoming: httpx.Response(
            307, headers={"Location": "https://attacker.invalid/capture"}
        )
    )
    with pytest.raises(ProviderError) as error:
        run(provider.submit_generation(spec, request(spec), paid_authorized=True))
    assert error.value.code == "provider_redirect_refused" and error.value.ambiguous
    assert len(calls) == 1 and calls[0].url.host == "queue.fal.run"


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://v3.fal.media/video.mp4",
        "https://v3.fal.media.evil.invalid/video.mp4",
        "https://127.0.0.1/video.mp4",
        "https://169.254.169.254/latest/meta-data",
        "https://user:pass@v3.fal.media/video.mp4",
        "https://v3.fal.media:443/video.mp4",
        "https://storage.googleapis.com/unreviewed-bucket/video.mp4",
        "https://v3.fal.media/video.mp4#fragment",
        "https://v3.fal.media/\nvideo.mp4",
    ],
)
def test_media_url_policy_rejects_private_and_unapproved_hosts(bad_url):
    with pytest.raises(ProviderError, match="provider_media_url_not_allowed"):
        validate_media_url(bad_url)


def test_result_returns_only_approved_url_and_safe_metadata(provider_factory):
    spec = model()
    provider, _ = provider_factory(
        lambda incoming: httpx.Response(
            200,
            json={
                "video": {
                    "url": "https://v3b.fal.media/files/result.mp4",
                    "file_size": 1234,
                    "content_type": "video/mp4",
                    "file_name": "secret-marker.mp4",
                    "width": 1280,
                },
                "actual_prompt": "secret-marker",
                "debug_api_key": FAKE_KEY,
            },
        )
    )
    result = run(provider.retrieve_result(spec, submission(spec)))
    assert result.video_url.endswith("result.mp4")
    assert result.metadata == {"file_size": 1234, "content_type": "video/mp4", "width": 1280}
    assert "secret-marker" not in repr(result)


@pytest.mark.parametrize(
    "field,bad_url",
    [
        ("status_url", "https://attacker.invalid/status"),
        ("response_url", "https://queue.fal.run/fal-ai/wan/requests/other"),
        (
            "cancel_url",
            "https://queue.fal.run/fal-ai/wan/v2.7/text-to-video/requests/38cf4064-3051-4b3c-9589-09b190cc175f/cancel",
        ),
    ],
)
def test_corrupt_submission_urls_fail_before_any_authenticated_request(
    provider_factory, field, bad_url
):
    spec = model()
    broken = submission(spec).model_copy(update={field: bad_url})
    provider, calls = provider_factory()
    with pytest.raises(ProviderError, match="invalid_provider_submission"):
        run(provider.get_generation_status(spec, broken))
    assert calls == []


def test_corrupt_paid_submission_response_requires_reconciliation(provider_factory):
    spec = model()
    body = submission(spec).model_dump()
    body["status_url"] = "https://attacker.invalid/steal-key"
    provider, calls = provider_factory(lambda incoming: httpx.Response(200, json=body))
    with pytest.raises(ProviderError) as error:
        run(provider.submit_generation(spec, request(spec), paid_authorized=True))
    assert error.value.ambiguous and len(calls) == 1


def test_provider_response_size_is_bounded(provider_factory):
    spec = model()
    provider, calls = provider_factory(
        lambda incoming: httpx.Response(200, content=b" " * (MAX_JSON_BYTES + 1))
    )
    with pytest.raises(ProviderError, match="provider_response_too_large") as error:
        run(provider.submit_generation(spec, request(spec), paid_authorized=True))
    assert error.value.ambiguous and len(calls) == 1


def test_live_pricing_refresh_is_read_only_and_does_not_verify_request_variant(provider_factory):
    spec = model()
    provider, calls = provider_factory(
        lambda incoming: httpx.Response(
            200,
            json={
                "prices": [
                    {
                        "endpoint_id": spec.endpoint_id,
                        "unit_price": 0.10,
                        "unit": "second",
                        "currency": "USD",
                    }
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )
    )
    price = run(provider.refresh_pricing(spec))
    assert price["available"] and not price["variant_verified"]
    assert Decimal(price["base_unit_price_usd"]) == Decimal("0.10")
    assert price["retrieved_at"]
    assert len(calls) == 1 and calls[0].method == "GET"
    assert calls[0].url.host == "api.fal.ai"
    assert calls[0].url.params["endpoint_id"] == spec.endpoint_id
    assert not run(provider.estimate_cost(spec, request(spec))).verified


def test_pricing_without_credentials_is_unavailable_and_never_calls_network(provider_factory):
    provider, calls = provider_factory(key="")
    assert run(provider.refresh_pricing(model()))["available"] is False
    assert calls == []


@pytest.mark.parametrize(
    "values",
    [
        {"prompt": " "},
        {"duration_seconds": "5"},
        {"duration_seconds": True},
        {"unknown": "field"},
    ],
)
def test_provider_request_contract_validates_untrusted_input(values):
    spec = model()
    with pytest.raises(ValidationError):
        request(spec, **values)


@pytest.mark.parametrize("profile", ["wan27_t2v", "wan27_i2v", "kling3pro_t2v", "kling3pro_i2v"])
def test_payload_keys_and_types_match_reviewed_official_schema(provider_factory, profile):
    schema_file = (
        Path(__file__).resolve().parents[1]
        / "apps/backend/app/providers/schemas/verified-inputs.json"
    )
    schema = json.loads(schema_file.read_text())["profiles"][profile]
    spec = replace(model(profile), enabled=True)
    image = profile.endswith("i2v")
    values = (
        {"mode": "image_to_video", "reference_image_url": "https://v3.fal.media/reference.png"}
        if image
        else {}
    )
    provider, calls = provider_factory()
    payload = run(provider.validate_request(spec, request(spec, **values)))
    assert set(payload).issubset(schema["input_fields"])
    expected = {"prompt", "negative_prompt", "enable_safety_checker", "duration"}
    expected |= (
        {"resolution", "enable_prompt_expansion"}
        if profile.startswith("wan")
        else {"generate_audio"}
    )
    expected.add(
        ("image_url" if profile.startswith("wan") else "start_image_url")
        if image
        else "aspect_ratio"
    )
    assert set(payload) == expected
    assert type(payload["duration"]) is (int if schema["duration_type"] == "integer" else str)
    assert payload["duration"] in schema["durations"]
    assert calls == []


def test_catalog_edits_cannot_expand_wan_provider_schema(provider_factory):
    original = model()
    spec = replace(original, aspect_ratios=(*original.aspect_ratios, "16:10"))
    # This bypass simulates a stale/tampered typed snapshot at an internal boundary.
    invalid = request(spec).model_copy(update={"aspect_ratio": "16:10"})
    provider, calls = provider_factory()
    with pytest.raises(ProviderError, match="unsupported_aspect_ratio"):
        run(provider.validate_request(spec, invalid))
    assert calls == []


@pytest.mark.parametrize("paid", [True, False])
def test_absolute_provider_deadline_stops_continuously_dripping_json(
    provider_factory, monkeypatch, paid
):
    from app.providers import fal

    class DrippingBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"{"
            while True:
                # Every chunk arrives well within the HTTPX per-read timeout.
                await asyncio.sleep(0.005)
                yield b" "

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(fal, "MAX_REQUEST_SECONDS", 0.02)
    stream = DrippingBody()
    provider, calls = provider_factory(lambda incoming: httpx.Response(200, stream=stream))
    spec = model()
    started = time.monotonic()
    with pytest.raises(ProviderError, match="provider_timeout") as failure:
        if paid:
            run(provider.submit_generation(spec, request(spec), paid_authorized=True))
        else:
            run(provider.get_generation_status(spec, submission(spec)))
    assert time.monotonic() - started < 1.0
    assert failure.value.ambiguous is paid
    assert failure.value.retryable is not paid
    assert stream.closed
    assert len(calls) == 1
    assert calls[0].method == ("POST" if paid else "GET")
    assert FAKE_KEY not in repr(failure.value)
