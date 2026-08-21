"""Logging setup.

The old ``setup_logging()`` ran at import time, called ``logging.basicConfig`` (which
hijacks the root logger for anything else in the process) and wrote
``attendance_system.log`` into whatever the current working directory happened to be --
a 528 KB copy of which ended up committed to the repository.  This version is called
from the app factory, writes a rotating file under the configured log folder, and
leaves the root logger alone.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask

_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"


def configure_logging(app: Flask) -> None:
    level = getattr(logging, str(app.config.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)
    formatter = logging.Formatter(_FORMAT)

    # Replace Flask's default handler rather than adding alongside it, otherwise every
    # record is emitted twice once a stream handler is attached.
    app.logger.handlers.clear()
    app.logger.setLevel(level)
    app.logger.propagate = False

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.setLevel(level)
    app.logger.addHandler(stream)

    if not app.config.get("TESTING"):
        log_dir = Path(app.config["LOG_FOLDER"])
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "attendance.log",
                maxBytes=app.config["LOG_MAX_BYTES"],
                backupCount=app.config["LOG_BACKUP_COUNT"],
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            app.logger.addHandler(file_handler)
        except OSError as exc:  # read-only filesystem, container without a volume
            app.logger.warning("File logging disabled: %s", exc)

    # Library loggers inside the package should land in the same handlers.
    package_logger = logging.getLogger("app")
    package_logger.handlers.clear()
    package_logger.setLevel(level)
    for handler in app.logger.handlers:
        package_logger.addHandler(handler)
    package_logger.propagate = False

    # SQLAlchemy's engine logger is noisy at INFO and echoes every statement.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
