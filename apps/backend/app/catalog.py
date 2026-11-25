"""Database-backed model catalog seeded only from verified official fal docs.

Seeding inserts missing entries. It never rewrites operator pricing, enablement,
default designation, endpoint/profile configuration, or provider settings.
"""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProviderConfiguration, VideoModel
from app.providers.base import ModelSpec
from app.providers.fal import PROFILES

VERIFIED_AT = datetime(2026, 11, 6, tzinfo=UTC)
REFERENCE_UPLOAD_REASON = "Reference uploads arrive Phase3"


def seed_definitions() -> list[dict]:
    definitions = []
    for profile, endpoint in PROFILES.items():
        wan = profile.startswith("wan")
        image = profile.endswith("i2v")
        mode = "image_to_video" if image else "text_to_video"
        rates = (
            {"720p": "0.10", "1080p": "0.15"}
            if wan
            else {
                "audio_off": "0.112",
                "audio_on": "0.168",
                "voice_control": "0.196",
            }
        )
        multipliers = (
            {"720p": "1", "1080p": "1.5"}
            if wan
            else {
                "audio_off": "1",
                "audio_on": "1.5",
            }
        )
        definitions.append(
            {
                "model_id": endpoint,
                "display_name": ("Wan 2.7" if wan else "Kling 3 Pro")
                + (" · Image to video" if image else " · Text to video"),
                "provider": "fal",
                "endpoint_id": endpoint,
                "supported_generation_modes": [mode],
                "supported_resolutions": ["720p", "1080p"] if wan else ["1080p"],
                # I2V has no ratio parameter. Source geometry is validated by the
                # future owned-reference-image ingestion flow before enabling it.
                "supported_aspect_ratios": []
                if image
                else (["16:9", "9:16", "1:1", "4:3", "3:4"] if wan else ["16:9", "9:16", "1:1"]),
                "min_duration_seconds": 2 if wan else 3,
                "max_duration_seconds": 15,
                "supports_reference_images": image,
                "supports_audio": True,
                "price_usd": Decimal("0.10" if wan else "0.112"),
                "pricing_unit": "second",
                "pricing_details": {
                    "adapter_profile": profile,
                    "source": "public_model_page",
                    "account_pricing_verified": False,
                    "public_rates": rates,
                    "multipliers": multipliers,
                    "audio_control": "always_on" if wan else "selectable",
                    "aspect_ratio_source": "reference_image" if image else "request",
                    "resolution_control": "selectable" if wan else "fixed",
                    "availability_reason": REFERENCE_UPLOAD_REASON if image else None,
                    "pricing_source_url": "https://fal.ai/models/" + endpoint,
                    "schema_source_url": "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id="
                    + endpoint,
                    "verification_date": "2026-11-06",
                    "requires_explicit_price_acknowledgement": True,
                },
                "documentation_url": "https://fal.ai/models/" + endpoint + "/api",
                "verified_at": VERIFIED_AT,
                "enabled": not image,
                "is_default": False,
            }
        )
    return definitions


def seed_catalog(db: Session) -> list[VideoModel]:
    provider = db.scalar(
        select(ProviderConfiguration).where(ProviderConfiguration.provider == "fal")
    )
    if provider is None:
        db.add(
            ProviderConfiguration(
                provider="fal", credential_env_var="VIDEO_FAL_KEY", enabled=True, settings={}
            )
        )
        db.flush()
    existing = {row.model_id: row for row in db.scalars(select(VideoModel)).all()}
    initially_empty = not existing
    result = []
    for definition in seed_definitions():
        row = existing.get(definition["model_id"])
        if row is None:
            # Only an initially empty catalog gets a default automatically.
            # A later seed must not undo an operator's choice to clear defaults.
            definition["is_default"] = initially_empty and not result
            row = VideoModel(**definition)
            db.add(row)
        result.append(row)
    db.flush()
    return result


def model_spec(row: VideoModel) -> ModelSpec:
    pricing = deepcopy(row.pricing_details or {})
    return ModelSpec(
        model_id=row.model_id,
        endpoint_id=row.endpoint_id,
        provider=row.provider,
        adapter_profile=str(pricing.get("adapter_profile", "")),
        modes=tuple(row.supported_generation_modes or []),
        resolutions=tuple(row.supported_resolutions or []),
        aspect_ratios=tuple(row.supported_aspect_ratios or []),
        min_duration_seconds=row.min_duration_seconds,
        max_duration_seconds=row.max_duration_seconds,
        supports_reference_images=row.supports_reference_images,
        supports_audio=row.supports_audio,
        enabled=row.enabled,
        pricing=pricing,
        price_usd=row.price_usd,
        pricing_unit=row.pricing_unit,
    )


def _safe_rates(values) -> dict:
    if not isinstance(values, dict):
        return {}
    result = {}
    for key in ("720p", "1080p", "audio_off", "audio_on", "voice_control"):
        try:
            value = Decimal(str(values[key]))
            if value.is_finite() and 0 < value <= 10000:
                result[key] = str(value)
        except (KeyError, InvalidOperation, ValueError, TypeError):
            continue
    return result


def serialize_model(row: VideoModel) -> dict:
    """Explicit public projection; never return raw provider settings/secrets."""
    details = row.pricing_details or {}
    profile = details.get("adapter_profile")
    safe_details = {
        "adapter_profile": profile if profile in PROFILES else None,
        "source": details.get("source")
        if details.get("source")
        in {"public_model_page", "account_pricing_api", "operator_verified"}
        else "unavailable",
        # A refreshed account BASE price does not verify a complete request's
        # resolution/audio rate. No Phase 2 profile has an exact-variant quote API.
        "account_pricing_verified": False,
        "public_rates": _safe_rates(details.get("public_rates")),
        "audio_control": details.get("audio_control")
        if details.get("audio_control") in {"always_on", "selectable"}
        else "unknown",
        "aspect_ratio_source": details.get("aspect_ratio_source")
        if details.get("aspect_ratio_source") in {"request", "reference_image"}
        else "unknown",
        "resolution_control": details.get("resolution_control")
        if details.get("resolution_control") in {"selectable", "fixed"}
        else "unknown",
        "requires_explicit_price_acknowledgement": True,
    }
    reason = (
        REFERENCE_UPLOAD_REASON
        if details.get("availability_reason") == REFERENCE_UPLOAD_REASON
        else None
    )
    return {
        "model_id": row.model_id,
        "display_name": row.display_name,
        "provider": row.provider,
        "endpoint_id": row.endpoint_id,
        "supported_generation_modes": list(row.supported_generation_modes),
        "supported_resolutions": list(row.supported_resolutions),
        "supported_aspect_ratios": list(row.supported_aspect_ratios),
        "min_duration_seconds": row.min_duration_seconds,
        "max_duration_seconds": row.max_duration_seconds,
        "supported_duration_seconds": list(
            range(row.min_duration_seconds, min(row.max_duration_seconds, 600) + 1)
        ),
        "supports_reference_images": row.supports_reference_images,
        "supports_audio": row.supports_audio,
        "price_usd": str(row.price_usd) if row.price_usd is not None else None,
        "pricing_unit": row.pricing_unit,
        "pricing_details": safe_details,
        "enabled": row.enabled,
        "is_default": row.is_default,
        "availability_reason": reason,
        "documentation_url": row.documentation_url,
        "verified_at": row.verified_at.isoformat() if row.verified_at else None,
    }
