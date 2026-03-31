#!/usr/bin/env python3
"""Seed database with sample public domain quotes for dashboard preview."""

from datetime import datetime, timedelta, timezone
from core.models import Quote, Post, PostStatus, init_db, get_session
from core.config import get_config

SAMPLE_QUOTES = [
    # === Productivity & Discipline ===
    {
        "content": "The secret of getting ahead is getting started.",
        "source": "Mark Twain",
        "topic": "Productivity",
        "quality_score": 9.2,
    },
    {
        "content": "It is not enough to be busy. The question is: what are we busy about?",
        "source": "Henry David Thoreau",
        "topic": "Productivity",
        "quality_score": 9.0,
    },
    {
        "content": "Discipline is choosing between what you want now and what you want most.",
        "source": "Abraham Lincoln",
        "topic": "Discipline",
        "quality_score": 9.4,
    },
    {
        "content": "We are what we repeatedly do. Excellence, then, is not an act, but a habit.",
        "source": "Will Durant",
        "topic": "Discipline",
        "quality_score": 9.3,
    },
    {
        "content": "The only way to do great work is to love what you do.",
        "source": "Steve Jobs",
        "topic": "Creativity",
        "quality_score": 8.7,
    },

    # === Leadership ===
    {
        "content": "A leader is one who knows the way, goes the way, and shows the way.",
        "source": "John C. Maxwell",
        "topic": "Leadership",
        "quality_score": 9.1,
    },
    {
        "content": "The task of leadership is not to put greatness into people, but to elicit it, for the greatness is there already.",
        "source": "John Buchan",
        "topic": "Leadership",
        "quality_score": 8.9,
    },
    {
        "content": "Before you are a leader, success is all about growing yourself. When you become a leader, success is all about growing others.",
        "source": "Jack Welch",
        "topic": "Leadership",
        "quality_score": 8.8,
    },
    {
        "content": "Management is doing things right; leadership is doing the right things.",
        "source": "Peter Drucker",
        "topic": "Leadership",
        "quality_score": 9.0,
    },
    {
        "content": "The greatest leader is not the one who does the greatest things, but the one who gets people to do the greatest things.",
        "source": "Ronald Reagan",
        "topic": "Leadership",
        "quality_score": 8.6,
    },

    # === Creativity ===
    {
        "content": "Creativity is intelligence having fun.",
        "source": "Albert Einstein",
        "topic": "Creativity",
        "quality_score": 9.2,
    },
    {
        "content": "The chief enemy of creativity is good sense.",
        "source": "Pablo Picasso",
        "topic": "Creativity",
        "quality_score": 8.5,
    },
    {
        "content": "You can't use up creativity. The more you use, the more you have.",
        "source": "Maya Angelou",
        "topic": "Creativity",
        "quality_score": 8.8,
    },
    {
        "content": "Innovation distinguishes between a leader and a follower.",
        "source": "Steve Jobs",
        "topic": "Creativity",
        "quality_score": 9.0,
    },

    # === Philosophy & Stoicism ===
    {
        "content": "No man is free who is not master of himself.",
        "source": "Epictetus",
        "topic": "Philosophy",
        "quality_score": 9.3,
    },
    {
        "content": "The happiness of your life depends upon the quality of your thoughts.",
        "source": "Marcus Aurelius",
        "topic": "Philosophy",
        "quality_score": 9.1,
    },
    {
        "content": "It is not death that a man should fear, but he should fear never beginning to live.",
        "source": "Marcus Aurelius",
        "topic": "Philosophy",
        "quality_score": 9.0,
    },
    {
        "content": "Waste no more time arguing about what a good man should be. Be one.",
        "source": "Marcus Aurelius",
        "topic": "Philosophy",
        "quality_score": 9.4,
    },
    {
        "content": "He who has a why to live can bear almost any how.",
        "source": "Friedrich Nietzsche",
        "topic": "Philosophy",
        "quality_score": 9.2,
    },
    {
        "content": "The obstacle is the way.",
        "source": "Marcus Aurelius",
        "topic": "Philosophy",
        "quality_score": 8.9,
    },

    # === Strategy & Mindset ===
    {
        "content": "Strategy without tactics is the slowest route to victory. Tactics without strategy is the noise before defeat.",
        "source": "Sun Tzu",
        "topic": "Strategy",
        "quality_score": 9.5,
    },
    {
        "content": "Plans are nothing; planning is everything.",
        "source": "Dwight D. Eisenhower",
        "topic": "Strategy",
        "quality_score": 9.0,
    },
    {
        "content": "The best time to plant a tree was 20 years ago. The second best time is now.",
        "source": "Chinese Proverb",
        "topic": "Mindset",
        "quality_score": 9.1,
    },
    {
        "content": "Whether you think you can, or you think you can't — you're right.",
        "source": "Henry Ford",
        "topic": "Mindset",
        "quality_score": 9.3,
    },

    # === Personal Growth ===
    {
        "content": "The only person you are destined to become is the person you decide to be.",
        "source": "Ralph Waldo Emerson",
        "topic": "Personal Growth",
        "quality_score": 9.0,
    },
    {
        "content": "What we fear doing most is usually what we most need to do.",
        "source": "Tim Ferriss",
        "topic": "Personal Growth",
        "quality_score": 8.7,
    },
    {
        "content": "The only real mistake is the one from which we learn nothing.",
        "source": "Henry Ford",
        "topic": "Personal Growth",
        "quality_score": 8.8,
    },
    {
        "content": "In the middle of difficulty lies opportunity.",
        "source": "Albert Einstein",
        "topic": "Personal Growth",
        "quality_score": 8.6,
    },
    {
        "content": "Do not wait to strike till the iron is hot, but make it hot by striking.",
        "source": "William Butler Yeats",
        "topic": "Personal Growth",
        "quality_score": 8.9,
    },
    {
        "content": "Success is not final, failure is not fatal: it is the courage to continue that counts.",
        "source": "Winston Churchill",
        "topic": "Mindset",
        "quality_score": 9.2,
    },
    {
        "content": "The measure of intelligence is the ability to change.",
        "source": "Albert Einstein",
        "topic": "Personal Growth",
        "quality_score": 8.5,
    },
    {
        "content": "Done is better than perfect.",
        "source": "Sheryl Sandberg",
        "topic": "Productivity",
        "quality_score": 8.8,
    },
    {
        "content": "If you want to go fast, go alone. If you want to go far, go together.",
        "source": "African Proverb",
        "topic": "Leadership",
        "quality_score": 9.0,
    },
    {
        "content": "Simplicity is the ultimate sophistication.",
        "source": "Leonardo da Vinci",
        "topic": "Creativity",
        "quality_score": 9.1,
    },
]


def seed_quotes():
    init_db()
    session = get_session()

    session.query(Quote).delete()
    session.commit()

    for quote_data in SAMPLE_QUOTES:
        quote = Quote(
            content=quote_data["content"],
            source=quote_data["source"],
            topic=quote_data["topic"],
            quality_score=quote_data["quality_score"],
            approved=True,
            created_at=datetime.now(timezone.utc)
        )
        session.add(quote)

    session.commit()
    print(f"Seeded {len(SAMPLE_QUOTES)} quotes")
    return len(SAMPLE_QUOTES)


def seed_posts():
    session = get_session()

    session.query(Post).delete()
    session.commit()

    from core.post_planner import PostPlanner
    planner = PostPlanner()
    cfg = get_config()

    quotes = planner.get_shuffled_quotes(14, min_score=7.0)

    base_time = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
    if base_time < datetime.now(timezone.utc):
        base_time += timedelta(days=1)

    for i, quote in enumerate(quotes):
        scheduled = base_time + timedelta(days=i)
        post_hashtags = planner._get_hashtags_for_topic(quote.topic)

        content = f'"{quote.content}"\n\n{cfg["tagline"]}\n\n{post_hashtags}'
        if len(content) > 280:
            max_len = 280 - len(f'"\n\n{cfg["tagline"]}\n\n{post_hashtags}') - 6
            content = f'"{quote.content[:max_len]}..."\n\n{cfg["tagline"]}\n\n{post_hashtags}'

        post = Post(
            quote_id=quote.id,
            platform="twitter",
            content=content,
            scheduled_time=scheduled,
            status=PostStatus.APPROVED.value if i < 3 else PostStatus.PENDING.value,
            created_at=datetime.now(timezone.utc)
        )
        session.add(post)

    session.commit()
    print(f"Seeded {len(quotes)} shuffled posts from multiple sources")
    return len(quotes)


if __name__ == "__main__":
    seed_quotes()
    seed_posts()
