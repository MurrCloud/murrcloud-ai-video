"""Operator-only provisioning and catalog maintenance; no password arguments."""

import argparse
import asyncio
from getpass import getpass

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select

from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.models import AuditEvent, User
from app.security import hash_password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user")
    create.add_argument("--email", required=True)
    create.add_argument("--role", choices=["admin", "editor", "viewer"], default="editor")
    commands.add_parser(
        "seed-models", help="Idempotently seed verified fal model metadata (no provider calls)"
    )
    pricing = commands.add_parser(
        "refresh-model-pricing", help="Read authenticated base pricing only; never generate"
    )
    pricing.add_argument("--model-id", required=True)
    args = parser.parse_args()
    if args.command in {"seed-models", "refresh-model-pricing"}:
        from app.catalog import model_spec, seed_catalog
        from app.models import VideoModel
        from app.runtime import make_provider

        settings = get_settings()
        engine = make_engine(settings.database_url)
        try:
            with make_session_factory(engine)() as db:
                if args.command == "seed-models":
                    models = seed_catalog(db)
                    print(f"Verified catalog ready: {len(models)} models; no generation submitted.")
                else:
                    model = db.scalar(
                        select(VideoModel).where(VideoModel.model_id == args.model_id)
                    )
                    if not model:
                        parser.error("Model not found; seed the catalog first")
                    metadata = asyncio.run(
                        make_provider(settings).refresh_pricing(model_spec(model))
                    )
                    model.pricing_details = {
                        **model.pricing_details,
                        "account_base_pricing": metadata,
                    }
                    print(
                        "Base pricing metadata refreshed; request variant pricing remains unverified."
                    )
                db.commit()
        finally:
            engine.dispose()
        return
    email = str(TypeAdapter(EmailStr).validate_python(args.email)).lower()
    password = getpass("New password (12–256 characters): ")
    if password != getpass("Confirm password: "):
        parser.error("Passwords did not match")
    try:
        digest = hash_password(password)
    except ValueError as exc:
        parser.error(str(exc))
    engine = make_engine(get_settings().database_url)
    try:
        with make_session_factory(engine)() as db:
            if db.scalar(select(User).where(User.email == email)):
                parser.error("User already exists")
            user = User(email=email, password_hash=digest, role=args.role)
            db.add(user)
            db.flush()
            db.add(
                AuditEvent(
                    event_type="user.provisioned",
                    actor_user_id=user.id,
                    entity_type="user",
                    entity_id=user.id,
                    details={"role": args.role},
                )
            )
            db.commit()
        print("User provisioned. No password was logged.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
