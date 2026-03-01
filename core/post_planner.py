from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict
import os
import json
import random

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

from .models import Quote, Post, PostStatus, get_session, init_db
from .config import get_config


def _build_format_prompt():
    cfg = get_config()
    return f"""You are a social media content creator for {cfg['brand_name']}.

Your task is to format quotes into engaging social media posts for X/Twitter.

Rules:
1. Keep the post under 280 characters total (CRITICAL)
2. The quote should be the focus
3. Add a brief tie-in to the brand's value proposition
4. Include 2-3 relevant hashtags
5. Keep it professional but approachable
6. No emojis in the quote itself, but 1-2 subtle emojis OK elsewhere

Format template:
"[Quote]"

[Brief tie-in - 1 short sentence]

{cfg['hashtags']}

Respond with just the formatted post text, nothing else."""


class PostPlanner:
    def __init__(self, api_key: Optional[str] = None, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.client = None
        if self.api_key and Anthropic:
            self.client = Anthropic(api_key=self.api_key)
        self.model = model
        init_db()
        self.session = get_session()

    def format_quote_for_twitter(self, quote: Quote, use_ai: bool = True) -> str:
        if use_ai and self.client:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                messages=[
                    {
                        "role": "user",
                        "content": f"Format this quote for X/Twitter:\n\n\"{quote.content}\"\n\nTopic: {quote.topic}"
                    }
                ],
                system=_build_format_prompt()
            )
            return response.content[0].text.strip()

        cfg = get_config()
        hashtags = self._get_hashtags_for_topic(quote.topic)
        template = f'"{quote.content}"\n\n{cfg["tagline"]}\n\n{hashtags}'

        if len(template) > 280:
            max_quote_len = 280 - len(f'"\n\n{cfg["tagline"]}\n\n{hashtags}') - 3
            truncated = quote.content[:max_quote_len] + "..."
            template = f'"{truncated}"\n\n{cfg["tagline"]}\n\n{hashtags}'

        return template

    def _get_hashtags_for_topic(self, topic: str) -> str:
        cfg = get_config()
        base = cfg['hashtags']
        topic_extra = {
            "Discipline": "#Discipline",
            "Risk Management": "#RiskManagement",
            "Edge Tracking": "#EdgeTracking",
            "Market Structure": "#MarketStructure",
            "Trading Psychology": "#Psychology",
            "Patience": "#Patience",
            "Model Following": "#Process",
            "Self-Improvement": "#Growth",
            "Strategy": "#Strategy",
            "Mindset": "#Mindset",
            "Leadership": "#Leadership",
            "Creativity": "#Creativity",
            "Philosophy": "#Philosophy",
            "Personal Growth": "#Growth",
            "Productivity": "#Productivity",
        }
        extra = topic_extra.get(topic, "")
        return f"{base} {extra}".strip() if extra else base

    def get_next_quote(self, min_score: float = 7.0, exclude_source: Optional[str] = None) -> Optional[Quote]:
        query = self.session.query(Quote).filter(
            Quote.approved == True,
            Quote.quality_score >= min_score
        )

        if exclude_source:
            query = query.filter(Quote.source != exclude_source)

        return query.order_by(
            Quote.used_count.asc(),
            Quote.quality_score.desc()
        ).first()

    def get_shuffled_quotes(self, count: int, min_score: float = 7.0) -> list[Quote]:
        """Get quotes shuffled across different sources to avoid consecutive posts from same document."""
        all_quotes = self.session.query(Quote).filter(
            Quote.approved == True,
            Quote.quality_score >= min_score
        ).order_by(
            Quote.used_count.asc(),
            Quote.quality_score.desc()
        ).all()

        if not all_quotes:
            return []

        by_source = defaultdict(list)
        for q in all_quotes:
            by_source[q.source].append(q)

        sources = list(by_source.keys())
        random.shuffle(sources)

        result = []
        source_idx = 0

        while len(result) < count and any(by_source.values()):
            source = sources[source_idx % len(sources)]

            if by_source[source]:
                result.append(by_source[source].pop(0))

            if not by_source[source]:
                sources = [s for s in sources if by_source[s]]
                if not sources:
                    break

            source_idx += 1

        return result[:count]

    def create_post(
        self,
        quote: Quote,
        platform: str = "twitter",
        scheduled_time: Optional[datetime] = None,
        use_ai: bool = True
    ) -> Post:
        if platform == "twitter":
            content = self.format_quote_for_twitter(quote, use_ai=use_ai)
        else:
            content = self.format_quote_for_twitter(quote, use_ai=use_ai)

        post = Post(
            quote_id=quote.id,
            platform=platform,
            content=content,
            scheduled_time=scheduled_time,
            status=PostStatus.PENDING.value,
            created_at=datetime.utcnow()
        )

        self.session.add(post)
        self.session.commit()

        return post

    def generate_posts(
        self,
        days: int = 7,
        posts_per_day: int = 1,
        platform: str = "twitter",
        start_time: Optional[datetime] = None,
        use_ai: bool = True,
        shuffle_sources: bool = True
    ) -> list[Post]:
        if start_time is None:
            start_time = datetime.utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
            if start_time < datetime.utcnow():
                start_time += timedelta(days=1)

        total_posts_needed = days * posts_per_day

        if shuffle_sources:
            quotes = self.get_shuffled_quotes(total_posts_needed)
        else:
            quotes = []
            for _ in range(total_posts_needed):
                q = self.get_next_quote()
                if q:
                    quotes.append(q)
                else:
                    break

        posts = []
        current_time = start_time
        quote_idx = 0

        for day in range(days):
            for post_num in range(posts_per_day):
                if quote_idx >= len(quotes):
                    break

                quote = quotes[quote_idx]
                quote_idx += 1

                post = self.create_post(
                    quote=quote,
                    platform=platform,
                    scheduled_time=current_time,
                    use_ai=use_ai
                )

                quote.used_count += 1
                quote.last_used = datetime.utcnow()
                self.session.commit()

                posts.append(post)

                if posts_per_day > 1 and post_num < posts_per_day - 1:
                    current_time += timedelta(hours=8)

            current_time = (current_time + timedelta(days=1)).replace(hour=9, minute=0)

        return posts

    def get_schedule(self, days: int = 7) -> list[Post]:
        end_date = datetime.utcnow() + timedelta(days=days)
        return self.session.query(Post).filter(
            Post.scheduled_time <= end_date,
            Post.status.in_([PostStatus.PENDING.value, PostStatus.APPROVED.value])
        ).order_by(Post.scheduled_time.asc()).all()

    def reschedule_post(self, post_id: int, new_time: datetime) -> bool:
        post = self.session.query(Post).filter(Post.id == post_id).first()
        if not post:
            return False

        post.scheduled_time = new_time
        self.session.commit()
        return True

    def cancel_post(self, post_id: int) -> bool:
        post = self.session.query(Post).filter(Post.id == post_id).first()
        if not post:
            return False

        post.status = PostStatus.REJECTED.value
        self.session.commit()
        return True
