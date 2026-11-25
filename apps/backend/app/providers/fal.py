"""fal queue adapter. Never automatically replay a paid generation submission.

The public model OpenAPI queue wrapper is less precise than the canonical queue
reference. Returned queue URLs are validated, persisted and reused. COMPLETED
includes failed/cancelled requests; it does not imply a usable result.
"""

import asyncio
import re
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from pydantic import SecretStr, ValidationError

from app.providers.base import (
    CancelOutcome,
    CostQuote,
    GenerationResult,
    GenerationStatus,
    ModelSpec,
    ProviderError,
    Submission,
    VideoRequest,
)

PROFILES = {
    "wan27_t2v": "fal-ai/wan/v2.7/text-to-video",
    "wan27_i2v": "fal-ai/wan/v2.7/image-to-video",
    "kling3pro_t2v": "fal-ai/kling-video/v3/pro/text-to-video",
    "kling3pro_i2v": "fal-ai/kling-video/v3/pro/image-to-video",
}
MAX_JSON_BYTES = 1024 * 1024
MAX_REQUEST_SECONDS = 45
TIMEOUT = httpx.Timeout(connect=5, read=30, write=10, pool=5)
SAFE_ERROR_TYPES = {
    "content_policy_violation",
    "request_timeout",
    "startup_timeout",
    "request_cancelled",
    "request_canceled",
    "cancelled",
    "canceled",
    "runner_disconnected",
    "internal_error",
    "rate_limited",
    "validation_error",
}
CANCELLATION_TYPES = {"request_cancelled", "request_canceled", "cancelled", "canceled"}


def validate_media_url(url: str) -> str:
    """Approve only fal's media host family; downloaders must still pin public IPs."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        valid = (
            parsed.scheme == "https"
            and parsed.netloc == hostname
            and (hostname == "fal.media" or hostname.endswith(".fal.media"))
            and re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*", hostname)
            and bool(parsed.path)
            and not parsed.fragment
            and "\\" not in url
            and not any(ord(char) < 33 for char in url)
            and len(url) <= 2048
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderError("provider_media_url_not_allowed")
    return url


class FalProvider:
    def __init__(self, api_key: SecretStr | str = "", client: httpx.AsyncClient | None = None):
        self._key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_models(self) -> list[dict]:
        # Supported verified schema profiles; the database decides visibility,
        # enabled/default status, pricing, and which profiles are installed.
        return [
            {"endpoint_id": endpoint, "adapter_profile": profile}
            for profile, endpoint in PROFILES.items()
        ]

    @staticmethod
    def _profile(model: ModelSpec) -> str:
        if model.provider != "fal" or PROFILES.get(model.adapter_profile) != model.endpoint_id:
            raise ProviderError("unsupported_model_profile")
        return model.adapter_profile

    async def validate_request(self, model: ModelSpec, request: VideoRequest) -> dict:
        profile = self._profile(model)
        if not model.enabled:
            raise ProviderError("model_disabled")
        if request.model_id != model.model_id:
            raise ProviderError("model_request_mismatch")
        expected_mode = "image_to_video" if profile.endswith("i2v") else "text_to_video"
        if request.mode != expected_mode or request.mode not in model.modes:
            raise ProviderError("unsupported_generation_mode")
        if request.resolution not in model.resolutions:
            raise ProviderError("unsupported_resolution")
        is_image = expected_mode == "image_to_video"
        if not is_image and request.aspect_ratio not in model.aspect_ratios:
            raise ProviderError("unsupported_aspect_ratio")
        family_min = 2 if profile.startswith("wan") else 3
        if (
            not max(family_min, model.min_duration_seconds)
            <= request.duration_seconds
            <= min(15, model.max_duration_seconds)
        ):
            raise ProviderError("unsupported_duration")
        if not model.supports_audio and request.audio_enabled:
            raise ProviderError("unsupported_audio")
        if is_image:
            if not model.supports_reference_images or not request.reference_image_url:
                raise ProviderError("reference_image_required")
            validate_media_url(request.reference_image_url)
        elif request.reference_image_url is not None:
            raise ProviderError("reference_image_not_supported")

        payload = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "enable_safety_checker": True,
        }
        if profile.startswith("wan"):
            if request.resolution not in {"720p", "1080p"}:
                raise ProviderError("unsupported_resolution")
            if not is_image and request.aspect_ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
                raise ProviderError("unsupported_aspect_ratio")
            if len(request.negative_prompt) > 500:
                raise ProviderError("negative_prompt_too_long")
            if not request.audio_enabled:
                raise ProviderError("native_audio_cannot_be_disabled")
            payload.update(
                duration=request.duration_seconds,
                resolution=request.resolution,
                enable_prompt_expansion=False,
            )
            if is_image:
                payload["image_url"] = request.reference_image_url
            else:
                payload["aspect_ratio"] = request.aspect_ratio
        else:
            if len(request.prompt) > 2500 or len(request.negative_prompt) > 2500:
                raise ProviderError("prompt_too_long")
            # Pro has a fixed resolution tier. Do not invent a resolution field.
            if request.resolution != "1080p":
                raise ProviderError("unsupported_resolution")
            if not is_image and request.aspect_ratio not in {"16:9", "9:16", "1:1"}:
                raise ProviderError("unsupported_aspect_ratio")
            payload.update(
                duration=str(request.duration_seconds), generate_audio=request.audio_enabled
            )
            if is_image:
                payload["start_image_url"] = request.reference_image_url
            else:
                payload["aspect_ratio"] = request.aspect_ratio
        return payload

    async def estimate_cost(self, model: ModelSpec, request: VideoRequest) -> CostQuote:
        await self.validate_request(model, request)
        source = (
            "public_model_page"
            if model.pricing.get("source") == "public_model_page"
            else "configured_estimate"
        )
        source_time = model.pricing.get("verification_date")
        basis = {
            "model_id": model.model_id,
            "duration_seconds": request.duration_seconds,
            "resolution": request.resolution,
            "audio_enabled": request.audio_enabled,
            "pricing_unit": model.pricing_unit,
            "pricing_source_time": source_time
            if isinstance(source_time, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_time)
            else None,
        }
        try:
            if model.price_usd is None or model.pricing_unit != "second":
                raise ValueError("Unavailable price")
            unit_price = Decimal(str(model.price_usd))
            multipliers = model.pricing.get("multipliers", {})
            variant = (
                request.resolution
                if model.adapter_profile.startswith("wan")
                else ("audio_on" if request.audio_enabled else "audio_off")
            )
            multiplier = Decimal(str(multipliers[variant]))
            if (
                not unit_price.is_finite()
                or unit_price <= 0
                or not multiplier.is_finite()
                or multiplier <= 0
            ):
                raise ValueError("Unavailable price")
            amount = (unit_price * multiplier * request.duration_seconds).quantize(
                Decimal("0.000001"), rounding=ROUND_UP
            )
            basis.update(
                unit_price_usd=str(unit_price), multiplier=str(multiplier), variant=variant
            )
        except (InvalidOperation, TypeError, ValueError, KeyError):
            return CostQuote(amount_usd=None, source="unavailable", verified=False, basis=basis)
        return CostQuote(
            amount_usd=amount,
            source=source,
            # The current pricing API exposes a BASE billing unit, not the exact
            # resolution/audio variant. Public multipliers remain estimates even
            # if an operator flips a Boolean verification flag in the database.
            verified=False,
            basis=basis,
        )

    async def refresh_pricing(self, model: ModelSpec) -> dict:
        """Read live account BASE pricing without submitting any inference.

        This response cannot prove the public resolution/audio multipliers. The
        caller may retain provenance or show a discrepancy, but must not promote
        an entire request quote to verified from this base unit alone.
        """
        self._profile(model)
        if not self._key:
            return {
                "available": False,
                "reason": "provider_not_configured",
                "variant_verified": False,
            }
        result = await self._json(
            "GET",
            "https://api.fal.ai/v1/models/pricing?" + urlencode({"endpoint_id": model.endpoint_id}),
        )
        prices = result.get("prices")
        if not isinstance(prices, list):
            raise ProviderError("provider_pricing_unavailable")
        for price in prices:
            if not isinstance(price, dict) or price.get("endpoint_id") != model.endpoint_id:
                continue
            try:
                rate = Decimal(str(price.get("unit_price")))
                unit = price.get("unit")
                if (
                    not rate.is_finite()
                    or rate < 0
                    or price.get("currency") != "USD"
                    or unit not in {"second", "seconds", "video", "request"}
                ):
                    raise ValueError("Unusable unit pricing")
            except (ValueError, TypeError, InvalidOperation):
                raise ProviderError("provider_pricing_unavailable") from None
            return {
                "available": True,
                "endpoint_id": model.endpoint_id,
                "base_unit_price_usd": str(rate),
                "unit": unit,
                "currency": "USD",
                "source": "account_pricing_api",
                "variant_verified": False,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "source_url": "https://fal.ai/docs/platform-apis/v1/models/pricing",
            }
        raise ProviderError("provider_pricing_unavailable")

    def reconcile_submission(self, model: ModelSpec, request_id: str) -> Submission:
        """Pure translation for an operator-verified existing queue request.

        Status/result/cancel URLs use owner/app without the endpoint path.
        This method performs no HTTP request or generation submission.
        """
        self._profile(model)
        try:
            canonical = str(UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise ProviderError("invalid_provider_request_id") from None
        if request_id != canonical:
            raise ProviderError("invalid_provider_request_id")
        owner, app = model.endpoint_id.split("/")[:2]
        base = f"https://queue.fal.run/{owner}/{app}/requests/{canonical}"
        return self._submission(
            model,
            {
                "request_id": canonical,
                "status_url": base + "/status",
                "response_url": base,
                "cancel_url": base + "/cancel",
            },
        )

    @staticmethod
    def _submission(model: ModelSpec, raw: dict | Submission) -> Submission:
        try:
            submission = raw if isinstance(raw, Submission) else Submission.model_validate(raw)
            request_id = str(UUID(submission.request_id))
            if request_id != submission.request_id:
                raise ValueError("Noncanonical ID")
            owner, app = model.endpoint_id.split("/")[:2]
            base = f"/{owner}/{app}/requests/{request_id}"
            for url, suffix in (
                (submission.status_url, "/status"),
                (submission.response_url, ""),
                (submission.cancel_url, "/cancel"),
            ):
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "queue.fal.run"
                    or parsed.path != base + suffix
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("Noncanonical queue URL")
        except (ValidationError, TypeError, ValueError, AttributeError):
            raise ProviderError("invalid_provider_submission") from None
        return submission

    async def _json(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        paid: bool = False,
        accepted_errors: tuple[int, ...] = (),
    ) -> dict:
        if not self._key:
            raise ProviderError("provider_not_configured")
        headers = {"Authorization": "Key " + self._key}
        if paid:
            headers.update({"X-Fal-No-Retry": "1", "X-Fal-Queue-Priority": "low"})
        try:
            async with (
                asyncio.timeout(MAX_REQUEST_SECONDS),
                self._client.stream(
                    method,
                    url,
                    json=payload,
                    headers=headers,
                    timeout=TIMEOUT,
                    follow_redirects=False,
                ) as response,
            ):
                status = response.status_code
                if not 200 <= status < 300 and status not in accepted_errors:
                    if status in {401, 403}:
                        code = "provider_auth_failed"
                    elif status == 422:
                        code = "provider_validation_failed"
                    elif status == 429:
                        code = "provider_rate_limited"
                    elif status == 404:
                        code = "provider_not_found"
                    elif status == 499:
                        code = "provider_cancelled"
                    elif 300 <= status < 400:
                        code = "provider_redirect_refused"
                    else:
                        code = (
                            "provider_server_error" if status >= 500 else "provider_request_failed"
                        )
                    raise ProviderError(
                        code,
                        ambiguous=paid and (status >= 500 or 300 <= status < 400),
                        retryable=not paid and (status >= 500 or status == 429),
                    )
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_JSON_BYTES:
                        raise ProviderError("provider_response_too_large", ambiguous=paid)
                    chunks.append(chunk)
                try:
                    import json

                    result = json.loads(b"".join(chunks))
                    if not isinstance(result, dict):
                        raise ValueError("Expected object")
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise ProviderError("provider_invalid_response", ambiguous=paid) from None
                result["__response_status"] = status
                return result
        except TimeoutError:
            # Per-read socket timeouts do not bound a peer that keeps sending
            # small chunks. This deadline covers connect, headers and the whole
            # streamed JSON body. A paid POST may already have been accepted.
            raise ProviderError("provider_timeout", ambiguous=paid, retryable=not paid) from None
        except httpx.TransportError:
            raise ProviderError(
                "provider_transport_error", ambiguous=paid, retryable=not paid
            ) from None

    async def submit_generation(
        self, model: ModelSpec, request: VideoRequest, *, paid_authorized: bool = False
    ) -> Submission:
        if not paid_authorized:
            raise ProviderError("paid_generation_not_authorized")
        payload = await self.validate_request(model, request)
        result = await self._json(
            "POST", "https://queue.fal.run/" + model.endpoint_id, payload=payload, paid=True
        )
        try:
            # Queue responses contain additional status/log fields; retain only
            # the persisted connection coordinates from this exact request.
            raw = {name: result[name] for name in Submission.model_fields}
            return self._submission(model, raw)
        except (KeyError, ProviderError):
            # The paid POST may have succeeded before response corruption.
            raise ProviderError("invalid_provider_submission", ambiguous=True) from None

    async def get_generation_status(
        self, model: ModelSpec, submission: Submission
    ) -> GenerationStatus:
        self._profile(model)
        submission = self._submission(model, submission)
        try:
            result = await self._json("GET", submission.status_url)
        except ProviderError as exc:
            if exc.code == "provider_cancelled":
                return GenerationStatus("cancelled", "provider_cancelled")
            raise
        if result.get("request_id") != submission.request_id:
            raise ProviderError("provider_request_mismatch")
        state = result.get("status")
        error_type = result.get("error_type")
        if not isinstance(error_type, (str, type(None))) or not isinstance(state, str):
            raise ProviderError("provider_invalid_response")
        if state == "COMPLETED" and (result.get("error") or error_type):
            if error_type in CANCELLATION_TYPES:
                return GenerationStatus("cancelled", "provider_cancelled")
            return GenerationStatus(
                "failed",
                error_type if error_type in SAFE_ERROR_TYPES else "provider_generation_failed",
            )
        mapped = {"IN_QUEUE": "queued", "IN_PROGRESS": "running", "COMPLETED": "completed"}
        if state not in mapped:
            raise ProviderError("provider_unknown_status")
        return GenerationStatus(mapped[state])

    async def retrieve_result(self, model: ModelSpec, submission: Submission) -> GenerationResult:
        self._profile(model)
        submission = self._submission(model, submission)
        result = await self._json("GET", submission.response_url)
        video = result.get("video")
        if not isinstance(video, dict) or not isinstance(video.get("url"), str):
            raise ProviderError("provider_missing_video")
        video_url = validate_media_url(video["url"])
        # Never persist an echoed/expanded prompt or unbounded provider object.
        metadata = {
            key: value
            for key, value in video.items()
            if key in {"file_size", "duration", "width", "height", "fps", "num_frames"}
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= value <= 10**12
        }
        if video.get("content_type") in {"video/mp4", "video/webm", "video/quicktime"}:
            metadata["content_type"] = video["content_type"]
        return GenerationResult(video_url=video_url, metadata=metadata)

    async def cancel_generation(self, model: ModelSpec, submission: Submission) -> CancelOutcome:
        self._profile(model)
        submission = self._submission(model, submission)
        try:
            result = await self._json("PUT", submission.cancel_url, accepted_errors=(400, 404))
        except ProviderError as exc:
            if exc.code in {"provider_not_found", "provider_request_failed"}:
                return CancelOutcome("reconciliation_required", False)
            raise
        state = result.get("status")
        if state == "OK" and result["__response_status"] == 200:
            return CancelOutcome("cancelled", True)
        if state == "CANCELLATION_REQUESTED":
            return CancelOutcome("cancellation_requested", False)
        if state == "ALREADY_COMPLETED":
            return CancelOutcome("already_completed", False)
        if state == "NOT_FOUND":
            return CancelOutcome("reconciliation_required", False)
        # Legacy generated OpenAPI's {success:true} alone proves neither queue
        # removal nor stopped execution under the canonical queue contract.
        return CancelOutcome("reconciliation_required", False)
