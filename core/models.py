from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, Enum, Index
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, UTC
import enum
import os

Base = declarative_base()


def utc_now():
    """Return current UTC time (timezone-aware)."""
    return datetime.now(UTC)


class PostStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"
    FAILED = "failed"


class Quote(Base):
    __tablename__ = 'quotes'

    id = Column(Integer, primary_key=True)
    content = Column(String(500), nullable=False)
    source = Column(String(200))
    topic = Column(String(100))
    quality_score = Column(Float, default=0.0, index=True)
    used_count = Column(Integer, default=0, index=True)
    last_used = Column(DateTime)
    approved = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=utc_now)

    def __repr__(self):
        return f"<Quote(id={self.id}, topic='{self.topic}', score={self.quality_score})>"


class Post(Base):
    __tablename__ = 'posts'

    id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, index=True)
    platform = Column(String(50))
    content = Column(Text)
    media_path = Column(String(500))
    scheduled_time = Column(DateTime, index=True)
    posted_time = Column(DateTime)
    status = Column(String(20), default=PostStatus.PENDING.value, index=True)
    post_id = Column(String(100))
    created_at = Column(DateTime, default=utc_now)
    approved_at = Column(DateTime)

    def __repr__(self):
        return f"<Post(id={self.id}, platform='{self.platform}', status='{self.status}')>"


class Analytics(Base):
    __tablename__ = 'analytics'

    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, index=True)
    platform = Column(String(50))
    impressions = Column(Integer, default=0)
    engagements = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    engagement_rate = Column(Float, default=0.0)
    fetched_at = Column(DateTime, default=utc_now)

    def __repr__(self):
        return f"<Analytics(post_id={self.post_id}, engagement_rate={self.engagement_rate})>"


def get_engine(db_url=None):
    if db_url is None:
        db_url = os.getenv('DATABASE_URL')

        if not db_url:
            db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'social_kanban.db')
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            db_url = f"sqlite:///{db_path}"
    return create_engine(db_url)


def get_session(engine=None):
    if engine is None:
        engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()


def init_db(engine=None):
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
    return engine
