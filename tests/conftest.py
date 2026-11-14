"""Shared fixtures. Database tests SKIP unless ATLAS_REALMS_TEST_DSN names a server (see compose.yaml);
each test gets a fresh, migrated database that is dropped afterwards."""

import os
import uuid

import psycopg
import pytest

from atlas_realms.db import migrate

TEST_DSN = os.environ.get("ATLAS_REALMS_TEST_DSN")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="ATLAS_REALMS_TEST_DSN not set")


@pytest.fixture
def db_url():
    name = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = psycopg.conninfo.make_conninfo(TEST_DSN, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as c:
            migrate(c)
        yield url
    finally:
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url) as c:
        yield c
