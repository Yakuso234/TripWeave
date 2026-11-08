"""Opt-in integration against a dedicated PostgreSQL test database.

Set TRIPWEAVE_TEST_POSTGRES_URL in the process environment. Each test creates and
removes only its own UUID-named schema. It never touches the public schema.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema
from test_storage import (
    test_requirement_update_racing_acceptance_does_not_lose_versions as check_update_race,
)
from test_storage import (
    test_simultaneous_same_candidate_acceptance_is_idempotent as check_idempotent_race,
)
from test_storage import (
    test_two_different_candidates_cannot_both_be_accepted as check_competing_candidates,
)

from tripweave.storage import Store


@pytest.fixture
def postgres_store():
    database_url = os.environ.get("TRIPWEAVE_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("No dedicated PostgreSQL test database configured")
    storage = Store(database_url)
    if storage.backend != "postgresql":
        storage.dispose()
        pytest.fail("TRIPWEAVE_TEST_POSTGRES_URL must point to PostgreSQL")
    schema = "tripweave_test_" + uuid4().hex
    created = False
    try:
        with storage.engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        storage.engine = storage.engine.execution_options(schema_translate_map={None: schema})
        storage.initialize()
        yield storage
    finally:
        if created:
            # Only this test's newly created, unpredictable schema is removed.
            assert schema.startswith("tripweave_test_") and len(schema) == 47
            with storage.engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        storage.dispose()


@pytest.mark.parametrize(
    "check",
    [
        check_competing_candidates,
        check_idempotent_race,
        check_update_race,
    ],
)
def test_postgres_transaction_contract(postgres_store, monkeypatch, check):
    check(postgres_store, monkeypatch)
