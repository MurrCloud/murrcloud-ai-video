"""Cumulative cost reports distinguish authorization, reservations and known charges."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.catalog import seed_catalog
from app.models import (
    ActualUsageCost,
    DirectorJob,
    GenerationJob,
    Project,
    ProjectInput,
    ProjectSettings,
    Scene,
    SceneVersion,
    Storyboard,
    User,
)
from app.reports import usage_report
from test_generation_postgres import postgres_generation as postgres_generation

WAN = "fal-ai/wan/v2.7/text-to-video"
KLING = "fal-ai/kling-video/v3/pro/text-to-video"


def money(value):
    return Decimal(str(value))


@pytest.fixture
def reports(api, login):
    login()
    with api.sessions() as db:
        seed_catalog(db)

        def project(name, owner, budget, cap, committed, *, archived=False, status="draft"):
            row = Project(
                owner_id=api.user_ids[owner],
                name=name,
                budget_usd=Decimal(budget),
                approved_cap_usd=Decimal(cap),
                committed_cost_usd=Decimal(committed),
                archived=archived,
                status=status,
            )
            db.add(row)
            db.flush()
            db.add(ProjectSettings(project_id=row.id))
            return row

        active = project("Owned active", "editor", "10", "6", "3", status="generating")
        archived = project("Owned archived", "editor", "20", "8", "4", archived=True)
        foreign = project("Another owner's project", "other", "100", "50", "7")

        def generation(project, model, reserved, *, status="succeeded"):
            scene = Scene(
                project_id=project.id,
                scene_key="scene_001",
                order_index=1,
                duration_seconds=5,
                visual_prompt="Offline reporting scene",
                video_model_id=model,
            )
            db.add(scene)
            db.flush()
            version = SceneVersion(
                scene_id=scene.id,
                version_number=1,
                video_model_id=model,
                generation_mode="text_to_video",
                duration_seconds=5,
                visual_prompt=scene.visual_prompt,
            )
            db.add(version)
            db.flush()
            job = GenerationJob(
                project_id=project.id,
                scene_version_id=version.id,
                model_id=model,
                provider="fal",
                endpoint_id=model,
                idempotency_key=str(uuid4()),
                reserved_cost_usd=Decimal(reserved),
                status=status,
            )
            db.add(job)
            db.flush()
            return job, scene

        active_job, active_scene = generation(active, WAN, "2")
        archived_job, archived_scene = generation(
            archived, KLING, "3", status="reconciliation_required"
        )
        foreign_job, _ = generation(foreign, WAN, "7", status="reconciliation_required")

        def charge(project, provider, amount, status="confirmed", *, job=None, director=None):
            row = ActualUsageCost(
                project_id=project.id,
                provider=provider,
                amount_usd=Decimal(amount) if amount is not None else None,
                status=status,
                generation_job_id=job.id if job else None,
                director_job_id=director.id if director else None,
            )
            db.add(row)
            return row

        # Several charge rows must not multiply the one generation reservation.
        for amount in ("0.5", "0.25", "0"):
            charge(active, "fal", amount, job=active_job)
        charge(active, "fal", None, "unavailable", job=active_job)
        charge(active, "fal", None, "pending", job=active_job)
        charge(archived, "fal", "1.25", job=archived_job)
        charge(foreign, "fal", "5", job=foreign_job)
        charge(active, "edge", "0")
        source = ProjectInput(
            project_id=active.id,
            kind="prompt",
            text="Offline reporting fixture",
            sha256="a" * 64,
        )
        board = Storyboard(project_id=active.id)
        db.add_all([source, board])
        db.flush()
        director = DirectorJob(
            project_id=active.id,
            storyboard_id=board.id,
            input_id=source.id,
            board_revision=0,
            context_hash="a" * 64,
            model="offline-director-model",
            request_snapshot={},
            idempotency_key=str(uuid4()),
            approval_fingerprint="b" * 64,
            reserved_cost_usd=Decimal("0.4"),
            approved_at=datetime.now(UTC),
            status="reconciliation_required",
        )
        db.add(director)
        db.flush()
        charge(active, "openai", "0.1", director=director)
        db.commit()
        identifiers = {
            "active": str(active.id),
            "archived": str(archived.id),
            "foreign": str(foreign.id),
            "active_scene": str(active_scene.id),
            "archived_scene": str(archived_scene.id),
        }

    def report(**params):
        response = api.client.get("/api/v1/reports/usage", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    return SimpleNamespace(api=api, report=report, **identifiers)


def test_report_owner_scope_includes_archived_and_counts_reservations_once(reports, login):
    result = reports.report()
    assert result["basis"] == "cumulative_including_archived" and result["scope"] == "owned"
    assert {item["key"] for item in result["items"]} == {reports.active, reports.archived}
    totals = result["totals"]
    assert money(totals["reserved_usd"]) == Decimal("5.4")
    assert money(totals["confirmed_usd"]) == Decimal("2.1")
    assert totals["total_actual_usd"] is None
    assert totals["known_cost_count"] == 6
    assert totals["unknown_cost_count"] == totals["pending_cost_count"] == 1
    assert totals["generation_jobs"] == 2 and totals["director_jobs"] == 1
    assert totals["project_count"] == 2 and totals["job_count"] == 3
    assert money(totals["committed_usd"]) == 7
    assert money(totals["budget_usd"]) == 30
    assert money(totals["approved_cap_usd"]) == 14
    assert money(totals["remaining_budget_usd"]) == 23
    assert (
        reports.api.client.get(
            "/api/v1/reports/usage", params={"project_id": reports.foreign}
        ).status_code
        == 404
    )
    login("admin")
    admin = reports.report()
    assert admin["scope"] == "all" and admin["totals"]["project_count"] == 3
    assert money(admin["totals"]["reserved_usd"]) == Decimal("12.4")
    assert money(admin["totals"]["confirmed_usd"]) == Decimal("7.1")
    assert len(reports.report(project_id=reports.foreign)["items"]) == 1


@pytest.mark.parametrize("group_by", ["project", "scene", "model", "provider"])
def test_report_grouping_pagination_keeps_full_totals(reports, group_by):
    complete = reports.report(group_by=group_by)
    first = reports.report(group_by=group_by, limit=1)
    assert first["totals"] == complete["totals"] and first["total"] == complete["total"]
    keys = [first["items"][0]["key"]]
    for offset in range(1, complete["total"]):
        page = reports.report(group_by=group_by, limit=1, offset=offset)
        assert page["totals"] == complete["totals"]
        keys.append(page["items"][0]["key"])
    assert keys == [item["key"] for item in complete["items"]]
    assert len(keys) == len(set(keys))
    assert money(sum(money(item["reserved_usd"]) for item in complete["items"])) == Decimal("5.4")
    assert sum(item["job_count"] for item in complete["items"]) == 3
    if group_by == "scene":
        assert set(keys) == {reports.active_scene, reports.archived_scene, "unassigned"}
    if group_by == "model":
        assert set(keys) == {WAN, KLING, "offline-director-model", "unassigned"}
    if group_by == "provider":
        assert set(keys) == {"fal", "openai", "edge"}


def test_confirmed_zero_is_known_while_pending_unknown_and_empty_are_not_zero(reports, login):
    items = {item["key"]: item for item in reports.report(group_by="provider")["items"]}
    free = items["edge"]
    assert money(free["confirmed_usd"]) == money(free["total_actual_usd"]) == 0
    assert free["known_cost_count"] == 1 and free["job_count"] == 0
    unknown = items["fal"]
    assert money(unknown["confirmed_usd"]) == 2 and unknown["total_actual_usd"] is None
    assert unknown["unknown_cost_count"] == unknown["pending_cost_count"] == 1
    login("viewer")
    empty = reports.report()
    assert empty["totals"]["confirmed_usd"] is None
    assert empty["totals"]["total_actual_usd"] is None
    assert empty["totals"]["known_cost_count"] == 0 and empty["total"] == 0
    assert empty["items"] == []


def test_project_report_filter_does_not_inherit_other_projects_unknown_costs(reports):
    result = reports.report(project_id=reports.archived, group_by="model")
    assert result["totals"]["project_count"] == 1 and result["total"] == 1
    assert money(result["totals"]["total_actual_usd"]) == Decimal("1.25")
    assert money(result["totals"]["reserved_usd"]) == 3
    assert result["items"][0]["project_id"] == reports.archived
    assert result["items"][0]["key"] == KLING


def test_unconfirmed_amounts_are_not_promoted_to_confirmed_spend(reports):
    with reports.api.sessions() as db:
        db.add_all(
            [
                ActualUsageCost(
                    project_id=UUID(reports.active),
                    provider="offline-pending-provider",
                    amount_usd=Decimal("99"),
                    status="pending",
                ),
                ActualUsageCost(
                    project_id=UUID(reports.active),
                    provider="offline-unavailable-provider",
                    amount_usd=Decimal("88"),
                    status="unavailable",
                ),
            ]
        )
        db.commit()
    items = {item["key"]: item for item in reports.report(group_by="provider")["items"]}
    for provider in ("offline-pending-provider", "offline-unavailable-provider"):
        assert items[provider]["confirmed_usd"] is None
        assert items[provider]["total_actual_usd"] is None
    assert items["offline-pending-provider"]["pending_cost_count"] == 1
    assert items["offline-unavailable-provider"]["unknown_cost_count"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {"group_by": "invalid"},
        {"limit": 101},
        {"limit": 0},
        {"offset": -1},
        {"offset": 100001},
        {"project_id": "invalid"},
    ],
)
def test_report_rejects_invalid_filters_and_page_bounds(reports, params):
    assert reports.api.client.get("/api/v1/reports/usage", params=params).status_code == 422


def test_dashboard_totals_cover_all_owned_projects_beyond_recent_page(reports, login):
    p = reports
    with p.api.sessions() as db:
        for index in range(55):
            project = Project(
                owner_id=p.api.user_ids["editor"],
                name=f"Additional project {index}",
                budget_usd=Decimal("1"),
                approved_cap_usd=Decimal("1"),
                committed_cost_usd=Decimal("1"),
                status="completed" if index < 5 else "failed" if index < 8 else "draft",
            )
            db.add(project)
            db.flush()
            db.add(ProjectSettings(project_id=project.id))
        db.commit()
    response = p.api.client.get("/api/v1/dashboard")
    assert response.status_code == 200, response.text
    dashboard = response.json()
    assert dashboard["projects_total"] == 56 and dashboard["in_production"] == 1
    assert dashboard["completed"] == 5 and dashboard["failed"] == 3
    assert len(dashboard["recent_projects"]) == 8
    assert money(dashboard["costs"]["committed_usd"]) == 62
    assert money(dashboard["costs"]["confirmed_usd"]) == Decimal("2.1")
    assert dashboard["costs"]["unknown_cost_count"] == 1
    # Archived work remains financially unresolved even though it is hidden in the library.
    assert dashboard["unresolved_jobs"] == 2
    login("admin")
    admin = p.api.client.get("/api/v1/dashboard").json()
    assert admin["projects_total"] == 57 and admin["unresolved_jobs"] == 3
    assert money(admin["costs"]["committed_usd"]) == 69


def test_reports_and_dashboard_require_authentication(api):
    assert api.client.get("/api/v1/reports/usage").status_code == 401
    assert api.client.get("/api/v1/dashboard").status_code == 401


@pytest.mark.integration
def test_real_postgres_union_preserves_reservations_and_unknown_costs(postgres_generation):
    fixture = postgres_generation
    with fixture.sessions() as db:
        scene = Scene(
            project_id=fixture.project_id,
            scene_key="offline_scene",
            order_index=1,
            duration_seconds=5,
            visual_prompt="Offline reporting scene",
            video_model_id=WAN,
        )
        db.add(scene)
        db.flush()
        version = SceneVersion(
            scene_id=scene.id,
            version_number=1,
            video_model_id=WAN,
            generation_mode="text_to_video",
            duration_seconds=5,
            visual_prompt=scene.visual_prompt,
        )
        db.add(version)
        db.flush()
        job = GenerationJob(
            project_id=fixture.project_id,
            scene_version_id=version.id,
            model_id=WAN,
            provider="fal",
            endpoint_id=WAN,
            idempotency_key=str(uuid4()),
            reserved_cost_usd=Decimal("0.5"),
        )
        db.add(job)
        db.flush()
        for amount, status in (("0.2", "confirmed"), ("0", "confirmed"), (None, "unavailable")):
            db.add(
                ActualUsageCost(
                    project_id=fixture.project_id,
                    generation_job_id=job.id,
                    provider="fal",
                    amount_usd=Decimal(amount) if amount is not None else None,
                    status=status,
                )
            )
        db.commit()
        user = db.get(User, fixture.user_id)
        for group_by in ("project", "scene", "model", "provider"):
            result = usage_report(db, user, group_by=group_by)
            assert money(result["totals"]["reserved_usd"]) == Decimal("0.5")
            assert money(result["totals"]["confirmed_usd"]) == Decimal("0.2")
            assert result["totals"]["known_cost_count"] == 2
            assert result["totals"]["unknown_cost_count"] == 1
            assert result["totals"]["total_actual_usd"] is None
            assert result["total"] == 1
