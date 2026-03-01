import pytest
from datetime import datetime
import tempfile
import os

from core.models import Quote, Post, Analytics, PostStatus, init_db, get_engine, get_session, Base


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


def test_quote_creation(db_session):
    quote = Quote(
        content="Test quote about trading discipline",
        source="test_doc",
        topic="Discipline",
        quality_score=8.5
    )
    db_session.add(quote)
    db_session.commit()

    assert quote.id is not None
    assert quote.approved is False
    assert quote.used_count == 0


def test_post_creation(db_session):
    post = Post(
        platform="twitter",
        content="Test tweet content",
        status=PostStatus.PENDING.value
    )
    db_session.add(post)
    db_session.commit()

    assert post.id is not None
    assert post.status == "pending"


def test_post_status_transitions(db_session):
    post = Post(
        platform="twitter",
        content="Test content",
        status=PostStatus.PENDING.value
    )
    db_session.add(post)
    db_session.commit()

    post.status = PostStatus.APPROVED.value
    post.approved_at = datetime.utcnow()
    db_session.commit()

    assert post.status == "approved"
    assert post.approved_at is not None


def test_analytics_creation(db_session):
    analytics = Analytics(
        post_id=1,
        platform="twitter",
        impressions=100,
        engagements=10,
        likes=5
    )
    db_session.add(analytics)
    db_session.commit()

    assert analytics.id is not None
    assert analytics.engagement_rate == 0.0
