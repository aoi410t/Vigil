"""Shared pytest fixtures."""
from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from db.session import engine


@pytest.fixture
def db_session():
    """Session inside an outer transaction + auto-restarting savepoint.

    Each test's flush/rollback runs against a savepoint so an IntegrityError
    doesn't tear down the outer transaction. The outer transaction is rolled
    back at teardown so the DB stays clean. Skipped when DATABASE_URL is unset.
    """
    if engine is None:
        pytest.skip("DATABASE_URL not configured; skipping DB-bound test")
    connection = engine.connect()
    trans = connection.begin()
    session = Session(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
