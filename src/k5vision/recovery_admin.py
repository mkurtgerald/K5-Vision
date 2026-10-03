"""Interactive local setup; never accept passwords through arguments or environment."""

from __future__ import annotations

import getpass
import secrets
import sys
import warnings
from pathlib import Path

from k5vision.domain.users import UserCreate, UserRole
from k5vision.identity_state import initialize_identity_state, validate_new_identity_directory

DEFAULT_RECOVERY_USERNAME = "recovery-admin"


def setup_administrator(directory: str | Path, *, username: str) -> None:
    """Let the local owner choose a unique installation password without echo."""
    validate_new_identity_directory(directory)
    UserCreate(
        username=username, display_name="Recovery administrator", role=UserRole.ADMINISTRATOR
    )
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("administrator setup requires an interactive local terminal")
    print("Choose a unique recovery password for this installation (12–256 characters).")
    print("Keep it in your own password manager; K5 cannot display or recover it later.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("New recovery password: ")
        confirmation = getpass.getpass("Confirm recovery password: ")
    if not secrets.compare_digest(password.encode("utf-8"), confirmation.encode("utf-8")):
        raise ValueError("password confirmation does not match")
    initialize_identity_state(directory, username=username, password=password)
    print("Recovery administrator initialized. Sign in through normal K5 authentication.")
