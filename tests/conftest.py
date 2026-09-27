"""Shared fixtures. Shapes are built with the synthetic CAD toolkit, in-process."""

from __future__ import annotations

import pytest

from interlock.db.database import Database
from interlock.kernel import get_backend


@pytest.fixture(scope="session")
def backend():
    return get_backend("occt")


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "test.db", tmp_path / "blobs")
    yield database
    database.close()
