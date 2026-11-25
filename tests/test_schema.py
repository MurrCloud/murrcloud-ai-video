"""Storage invariants that protect access, accounting and paid-job recovery."""

import os
import re
from collections import Counter
from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from app.db import make_engine, make_session_factory
from app.models import (
    ActualUsageCost,
    Base,
    GenerationJob,
    Project,
    ProviderConfiguration,
    Scene,
    SceneVersion,
    User,
    VideoModel,
)
from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def db_session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


def owner(session, email="owner@example.com"):
    user = User(email=email, password_hash="test-only-hash", role="editor")
    session.add(user)
    session.flush()
    return user


def production_plan(session):
    user = owner(session)
    provider = ProviderConfiguration(provider="fixture", credential_env_var="FIXTURE_API_KEY")
    session.add(provider)
    session.flush()
    model = VideoModel(
        model_id="fixture-model",
        display_name="Test fixture only",
        provider="fixture",
        endpoint_id="fixture/not-a-real-provider-endpoint",
        min_duration_seconds=5,
        max_duration_seconds=10,
    )
    project = Project(owner_id=user.id, name="Test project", budget_usd=Decimal("5"))
    session.add_all([model, project])
    session.flush()
    scene = Scene(
        project_id=project.id,
        scene_key="scene_001",
        order_index=1,
        duration_seconds=5,
        video_model_id=model.model_id,
        visual_prompt="A calm sea",
    )
    session.add(scene)
    session.flush()
    version = SceneVersion(
        scene_id=scene.id,
        version_number=1,
        video_model_id=model.model_id,
        generation_mode="text_to_video",
        duration_seconds=5,
        visual_prompt="A calm sea",
    )
    session.add(version)
    session.flush()
    return project, model, scene, version


def test_rejects_invalid_role_and_negative_budget(db_session):
    user = owner(db_session)
    db_session.commit()
    db_session.add(User(email="bad@example.com", password_hash="hash", role="superuser"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.add(Project(owner_id=user.id, name="Negative", budget_usd=Decimal("-0.01")))
    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize(
    "budget,approved,committed",
    [
        ("1", "2", "0"),
        ("2", None, "0.50"),
        ("2", "1", "1.01"),
    ],
)
def test_database_rejects_unapproved_cost_commitments(db_session, budget, approved, committed):
    user = owner(db_session)
    db_session.add(
        Project(
            owner_id=user.id,
            name="Over cap",
            budget_usd=Decimal(budget),
            approved_cap_usd=Decimal(approved) if approved is not None else None,
            committed_cost_usd=Decimal(committed),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_duplicate_idempotency_keys_and_provider_job_ids_are_blocked(db_session):
    project, model, _scene, version = production_plan(db_session)
    base = dict(
        project_id=project.id,
        scene_version_id=version.id,
        model_id=model.model_id,
        provider="fixture",
        endpoint_id=model.endpoint_id,
    )
    db_session.add(GenerationJob(**base, idempotency_key="once", provider_job_id="remote-1"))
    db_session.commit()
    db_session.add(GenerationJob(**base, idempotency_key="once", provider_job_id="remote-2"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.add(GenerationJob(**base, idempotency_key="different", provider_job_id="remote-1"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_unverified_pricing_remains_unknown(db_session):
    project, model, _scene, _version = production_plan(db_session)
    db_session.add(ActualUsageCost(project_id=project.id, provider="fixture", status="unavailable"))
    db_session.commit()
    assert model.price_usd is None
    assert db_session.scalar(select(ActualUsageCost)).amount_usd is None


def test_previous_scene_version_survives_regeneration(db_session):
    _project, model, scene, first = production_plan(db_session)
    db_session.add(
        SceneVersion(
            scene_id=scene.id,
            version_number=2,
            video_model_id=model.model_id,
            generation_mode="text_to_video",
            duration_seconds=5,
            visual_prompt="A stormy sea",
        )
    )
    db_session.commit()
    versions = db_session.scalars(select(SceneVersion).order_by(SceneVersion.version_number)).all()
    assert len(versions) == 2
    assert versions[0].id == first.id
    assert versions[0].visual_prompt == "A calm sea"
    assert versions[1].visual_prompt == "A stormy sea"


def migration_config(url, output=None):
    backend = Path(__file__).resolve().parents[1] / "apps" / "backend"
    config = Config(str(backend / "alembic.ini"), output_buffer=output)
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migration_roundtrip_sqlite(tmp_path):
    url = f"sqlite:///{tmp_path / 'schema.db'}"
    config = migration_config(url)
    command.upgrade(config, "head")
    engine = make_engine(url)
    assert set(Base.metadata.tables).issubset(inspect(engine).get_table_names())
    engine.dispose()
    command.downgrade(config, "base")
    engine = make_engine(url)
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()


def test_postgres_migration_compiles_without_live_credentials():
    sql = StringIO()
    command.upgrade(
        migration_config("postgresql+psycopg://unused:unused@localhost/unused", sql),
        "head",
        sql=True,
    )
    generated = sql.getvalue()
    assert "CREATE TABLE generation_jobs" in generated
    assert "JSONB" in generated
    assert "UNIQUE (provider, provider_job_id)" in generated
    assert "CREATE UNIQUE INDEX uq_video_models_one_default" in generated


def test_postgres_metadata_has_no_relation_name_collisions():
    # PostgreSQL tables, explicit indexes and indexes backing PRIMARY KEY/UNIQUE
    # constraints share a schema namespace. SQLite permits colliding constraint
    # names, so its migration roundtrip cannot catch this failure.
    preparer = postgresql.dialect().identifier_preparer
    names = []
    for table in Base.metadata.tables.values():
        schema = table.schema or "public"
        names.append((schema, preparer.format_table(table)))
        names.extend((schema, preparer.format_index(index)) for index in table.indexes)
        names.extend(
            (schema, preparer.format_constraint(constraint))
            for constraint in table.constraints
            if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
        )
    duplicates = {name: count for name, count in Counter(names).items() if count > 1}
    assert not duplicates, f"PostgreSQL relation-name collisions: {duplicates}"


def test_postgres_static_migration_has_no_relation_name_collisions():
    sql = StringIO()
    command.upgrade(
        migration_config("postgresql+psycopg://unused:unused@localhost/unused", sql),
        "head",
        sql=True,
    )
    generated = sql.getvalue()
    names = re.findall(r"CREATE TABLE ([^\s(]+)", generated)
    names += re.findall(r"CREATE (?:UNIQUE )?INDEX ([^\s(]+)", generated)
    names += re.findall(r"CONSTRAINT ([^\s]+) (?:PRIMARY KEY|UNIQUE)\b", generated)
    duplicates = {name: count for name, count in Counter(names).items() if count > 1}
    assert not duplicates, f"Migration creates duplicate PostgreSQL relations: {duplicates}"


@pytest.mark.integration
def test_live_postgres_migration_roundtrip_and_drift():
    url = os.environ.get("VIDEO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set VIDEO_TEST_DATABASE_URL to an isolated PostgreSQL test database")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg" or not (
        parsed.database == "video_studio_test"
        or (parsed.database or "").startswith("video_studio_test_")
    ):
        pytest.fail("Refusing migration tests outside a dedicated video_studio_test database")
    engine = make_engine(url)
    # No database resets: this test creates and cleans only its own random schema.
    schema = "video_schema_test_" + uuid4().hex
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.commit()
            try:
                connection.execute(text(f'SET search_path TO "{schema}"'))
                connection.commit()
                config = migration_config(url)
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                assert set(Base.metadata.tables).issubset(inspect(connection).get_table_names())
                command.check(config)
                command.downgrade(config, "base")
                assert inspect(connection).get_table_names() == ["alembic_version"]
                connection.commit()
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                connection.commit()
    finally:
        engine.dispose()
