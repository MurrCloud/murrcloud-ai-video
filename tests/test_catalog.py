import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from app.catalog import REFERENCE_UPLOAD_REASON, model_spec, seed_catalog, serialize_model
from app.db import make_engine, make_session_factory
from app.models import Base, ProviderConfiguration, VideoModel
from sqlalchemy import select


@pytest.fixture
def catalog_db():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as db:
        yield db
    engine.dispose()


def test_catalog_seeds_four_verified_profiles_and_a_single_default(catalog_db):
    rows = seed_catalog(catalog_db)
    catalog_db.commit()
    assert len(rows) == 4
    assert len(catalog_db.scalars(select(VideoModel)).all()) == 4
    assert sum(row.is_default for row in rows) == 1
    assert rows[0].is_default and rows[0].enabled
    for row in rows:
        assert row.model_id == row.endpoint_id
        assert row.provider == "fal"
        assert row.documentation_url.startswith("https://fal.ai/models/")
        assert row.verified_at.date().isoformat() == "2026-11-06"
        assert row.pricing_details["account_pricing_verified"] is False
        assert row.pricing_unit == "second"
        if row.supports_reference_images:
            assert not row.enabled
            assert row.supported_aspect_ratios == []
            assert row.pricing_details["availability_reason"] == REFERENCE_UPLOAD_REASON
            assert row.pricing_details["aspect_ratio_source"] == "reference_image"
        else:
            assert row.enabled
            assert "16:9" in row.supported_aspect_ratios
    provider = catalog_db.scalar(select(ProviderConfiguration))
    assert provider.provider == "fal"
    assert provider.credential_env_var == "VIDEO_FAL_KEY"
    assert provider.settings == {}


def test_reseeding_preserves_operator_enablement_defaults_and_pricing(catalog_db):
    first = seed_catalog(catalog_db)
    catalog_db.commit()
    first[0].enabled = False
    first[0].is_default = False
    first[0].price_usd = Decimal("7.123456")
    first[0].pricing_details = {"adapter_profile": "wan27_t2v", "operator_note": "keep"}
    catalog_db.flush()
    first[2].is_default = True
    provider = catalog_db.scalar(select(ProviderConfiguration))
    provider.enabled = False
    provider.settings = {"configured": True}
    catalog_db.commit()
    previous_ids = {row.model_id: row.id for row in first}
    again = seed_catalog(catalog_db)
    catalog_db.commit()
    assert {row.model_id: row.id for row in again} == previous_ids
    assert again[0].enabled is False and again[0].is_default is False
    assert again[0].price_usd == Decimal("7.123456")
    assert again[0].pricing_details["operator_note"] == "keep"
    assert again[2].is_default is True
    assert provider.enabled is False
    assert provider.settings == {"configured": True}


def test_reseeding_does_not_restore_an_operator_cleared_default(catalog_db):
    rows = seed_catalog(catalog_db)
    catalog_db.commit()
    rows[0].is_default = False
    catalog_db.commit()
    seed_catalog(catalog_db)
    catalog_db.commit()
    assert not any(row.is_default for row in catalog_db.scalars(select(VideoModel)))


def test_model_spec_is_an_independent_database_free_snapshot(catalog_db):
    row = seed_catalog(catalog_db)[0]
    spec = model_spec(row)
    assert spec.adapter_profile == "wan27_t2v"
    assert spec.modes == ("text_to_video",)
    assert spec.resolutions == ("720p", "1080p")
    row.pricing_details["multipliers"]["1080p"] = "999"
    assert spec.pricing["multipliers"]["1080p"] == "1.5"
    with pytest.raises(FrozenInstanceError):
        spec.enabled = False


def test_public_catalog_projection_excludes_credentials_and_arbitrary_details(catalog_db):
    row = seed_catalog(catalog_db)[0]
    row.pricing_details = {
        **row.pricing_details,
        "api_key": "secret-marker-123",
        "credential_env_var": "PRIVATE_ENV_NAME",
        "request_headers": {"Authorization": "Key secret-marker-123"},
        "public_rates": {"720p": "0.10", "evil": "secret-marker-123", "1080p": "NaN"},
    }
    public = serialize_model(row)
    serialized = json.dumps(public)
    assert "secret-marker-123" not in serialized
    assert "PRIVATE_ENV_NAME" not in serialized
    assert "request_headers" not in serialized
    assert public["pricing_details"]["public_rates"] == {"720p": "0.10"}
    assert public["pricing_details"]["audio_control"] == "always_on"
    assert public["supported_duration_seconds"] == list(range(2, 16))


def test_unknown_price_remains_null_in_public_catalog(catalog_db):
    row = seed_catalog(catalog_db)[0]
    row.price_usd = None
    assert serialize_model(row)["price_usd"] is None
