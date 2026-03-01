import pytest
import tempfile
import os

from core.models import Quote, Post, PostStatus, init_db, get_engine, get_session, Base
from core.approval_system import ApprovalSystem


@pytest.fixture
def db_session():
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    try:
        engine = get_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        session = get_session(engine)
        yield session
        session.close()
    finally:
        os.unlink(db_path)


@pytest.fixture
def approval_system(db_session):
    return ApprovalSystem(session=db_session)


@pytest.fixture
def sample_quotes(db_session):
    quotes = [
        Quote(content="Quote 1 about discipline", topic="Discipline", quality_score=8.0),
        Quote(content="Quote 2 about risk", topic="Risk Management", quality_score=7.5),
        Quote(content="Quote 3 about patience", topic="Patience", quality_score=6.0),
    ]
    for q in quotes:
        db_session.add(q)
    db_session.commit()
    return quotes


def test_approve_quote(approval_system, sample_quotes, db_session):
    quote_id = sample_quotes[0].id
    assert approval_system.approve_quote(quote_id) is True

    quote = db_session.query(Quote).filter(Quote.id == quote_id).first()
    assert quote.approved is True


def test_reject_quote(approval_system, sample_quotes, db_session):
    quote_id = sample_quotes[0].id
    approval_system.approve_quote(quote_id)
    approval_system.reject_quote(quote_id)

    quote = db_session.query(Quote).filter(Quote.id == quote_id).first()
    assert quote.approved is False


def test_approve_nonexistent_quote(approval_system):
    assert approval_system.approve_quote(9999) is False


def test_approve_post(approval_system, db_session):
    post = Post(platform="twitter", content="Test post", status=PostStatus.PENDING.value)
    db_session.add(post)
    db_session.commit()

    assert approval_system.approve_post(post.id) is True

    updated = db_session.query(Post).filter(Post.id == post.id).first()
    assert updated.status == PostStatus.APPROVED.value


def test_reject_post(approval_system, db_session):
    post = Post(platform="twitter", content="Test post", status=PostStatus.PENDING.value)
    db_session.add(post)
    db_session.commit()

    assert approval_system.reject_post(post.id) is True

    updated = db_session.query(Post).filter(Post.id == post.id).first()
    assert updated.status == PostStatus.REJECTED.value
