"""RotatingFileHandler with gzip rotator + on_rollover callback.

Used by AuditWriter to bound audit.log size. The on_rollover hook lets
AuditIndex rebuild its byte-offset index after the file rotates (old
offsets point into the now-renamed audit.log.1.gz and become invalid).
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
from logging.handlers import RotatingFileHandler


def gzip_namer(name: str) -> str:
    """Append .gz to rotated file names."""
    return name + ".gz"


def gzip_rotator(source: str, dest: str) -> None:
    """Gzip source file into dest during rollover."""
    with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(source)


class RebuildingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler with gzip rotator + on_rollover callback.

    Defaults to gzip compression (`gzip_namer`/`gzip_rotator`) so callers
    only need to pass `on_rollover`. Override namer/rotator=None to
    disable gzip or supply custom rotator functions.

    The callback runs synchronously inside doRollover(). Exceptions are
    caught and logged (via a dedicated logger) so a buggy rebuild cannot
    crash the request thread that triggered the rotation.
    """

    def __init__(
        self,
        filename,
        mode="a",
        maxBytes=0,
        backupCount=0,
        encoding=None,
        delay=False,
        errors=None,
        on_rollover=None,
        namer=gzip_namer,
        rotator=gzip_rotator,
    ):
        super().__init__(
            filename, mode, maxBytes, backupCount,
            encoding, delay, errors,
        )
        self._on_rollover = on_rollover
        self.namer = namer
        self.rotator = rotator

    def doRollover(self):
        super().doRollover()
        if self._on_rollover is not None:
            try:
                self._on_rollover()
            except Exception:
                logging.getLogger("ekrs.audit.rollover").exception(
                    "on_rollover callback failed"
                )