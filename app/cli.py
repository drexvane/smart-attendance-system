"""Flask CLI commands."""

from __future__ import annotations

import click
from flask import Flask
from flask.cli import with_appcontext

from app.extensions import db
from app.models import Role, User


def register_commands(app: Flask) -> None:
    app.cli.add_command(create_admin)
    app.cli.add_command(create_user)
    app.cli.add_command(list_users)
    app.cli.add_command(deactivate_user)
    app.cli.add_command(rebuild_faces)
    app.cli.add_command(recognition_info)


@click.command("create-admin")
@click.option("--username", prompt=True)
@click.option("--email", prompt=True)
@click.password_option("--password", confirmation_prompt=True)
@with_appcontext
def create_admin(username: str, email: str, password: str) -> None:
    """Create the first administrator.

    There are deliberately no default credentials: a fresh install has no usable
    account until this is run.
    """
    _create(username, email, password, Role.ADMIN.value)


@click.command("create-user")
@click.option("--username", prompt=True)
@click.option("--email", prompt=True)
@click.option(
    "--role",
    type=click.Choice([Role.ADMIN.value, Role.TEACHER.value]),
    default=Role.TEACHER.value,
)
@click.password_option("--password", confirmation_prompt=True)
@with_appcontext
def create_user(username: str, email: str, role: str, password: str) -> None:
    """Create an additional operator account."""
    _create(username, email, password, role)


def _create(username: str, email: str, password: str, role: str) -> None:
    username = username.strip()
    email = email.strip().lower()

    if len(password) < 12:
        raise click.ClickException("Password must be at least 12 characters")

    existing = db.session.execute(
        db.select(User).where(db.or_(User.username == username, User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise click.ClickException("A user with that username or email already exists")

    user = User(username=username, email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"Created {role} '{username}'.")


@click.command("list-users")
@with_appcontext
def list_users() -> None:
    """Show all operator accounts."""
    users = db.session.execute(db.select(User).order_by(User.username)).scalars().all()
    if not users:
        click.echo("No users yet. Run 'flask create-admin' to make the first one.")
        return
    for user in users:
        state = "active" if user.is_active else "disabled"
        click.echo(f"{user.username:<20} {user.role:<8} {state:<9} {user.email}")


@click.command("deactivate-user")
@click.argument("username")
@with_appcontext
def deactivate_user(username: str) -> None:
    """Disable an account without deleting its audit trail."""
    user = db.session.execute(db.select(User).filter_by(username=username)).scalar_one_or_none()
    if user is None:
        raise click.ClickException(f"No user named {username!r}")
    user.is_active_flag = False
    db.session.commit()
    click.echo(f"Deactivated '{username}'.")


@click.command("rebuild-faces")
@with_appcontext
def rebuild_faces() -> None:
    """Retrain the face recognizer from the stored samples."""
    from app.services.student_service import rebuild_face_model

    count = rebuild_face_model()
    click.echo(f"Trained on {count} enrolled student(s).")


@click.command("recognition-info")
@with_appcontext
def recognition_info() -> None:
    """Report which detector backend and thresholds are in effect."""
    from flask import current_app

    from app.recognition import get_detector, get_recognizer

    detector = get_detector()
    recognizer = get_recognizer()
    click.echo(f"Detector backend      : {detector.backend}")
    click.echo(f"Detector confidence   : {detector.min_confidence}")
    click.echo(f"Recognizer trained    : {recognizer.is_trained}")
    click.echo(f"Enrolled students     : {recognizer.enrolled_count}")
    click.echo(f"Max match distance    : {recognizer.max_distance}")
    click.echo(f"Min match margin      : {recognizer.min_margin}")
    click.echo(f"Confirmation frames   : {current_app.config['RECOGNITION_CONFIRM_FRAMES']}")
    click.echo(f"Cooldown (seconds)    : {current_app.config['RECOGNITION_COOLDOWN_SECONDS']}")
