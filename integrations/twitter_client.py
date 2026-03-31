import os
from datetime import datetime, timezone
from typing import Optional

try:
    import tweepy
except ImportError:
    tweepy = None

from rich.console import Console
from rich.panel import Panel

from core.models import Post, PostStatus, get_session, init_db
from core.config import handle as config_handle


class TwitterClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        access_secret: Optional[str] = None,
        bearer_token: Optional[str] = None,
        dry_run: bool = True
    ):
        self.api_key = api_key or os.getenv("TWITTER_API_KEY")
        self.api_secret = api_secret or os.getenv("TWITTER_API_SECRET")
        self.access_token = access_token or os.getenv("TWITTER_ACCESS_TOKEN")
        self.access_secret = access_secret or os.getenv("TWITTER_ACCESS_SECRET")
        self.bearer_token = bearer_token or os.getenv("TWITTER_BEARER_TOKEN")

        self.dry_run = dry_run
        self.console = Console()
        self.client = None
        self.api = None

        if not self.dry_run:
            self._init_client()

        init_db()
        self.session = get_session()

    def _init_client(self):
        if tweepy is None:
            raise ImportError("tweepy is required for Twitter integration")

        if not all([self.api_key, self.api_secret, self.access_token, self.access_secret]):
            raise ValueError("Twitter API credentials not fully configured")

        self.client = tweepy.Client(
            consumer_key=self.api_key,
            consumer_secret=self.api_secret,
            access_token=self.access_token,
            access_token_secret=self.access_secret,
            bearer_token=self.bearer_token
        )

        auth = tweepy.OAuth1UserHandler(
            self.api_key,
            self.api_secret,
            self.access_token,
            self.access_secret
        )
        self.api = tweepy.API(auth)

    def is_configured(self) -> bool:
        return all([self.api_key, self.api_secret, self.access_token, self.access_secret])

    def verify_credentials(self) -> dict:
        if self.dry_run:
            return {"configured": True, "status": "dry_run", "message": "Running in dry-run mode"}

        if not self.is_configured():
            return {"configured": False, "error": "Twitter API credentials not fully configured"}
        if not self.client or not self.api:
            return {"configured": False, "error": "Client not initialized"}

        try:
            # OAuth1 verification matches the credential set we ask users to paste
            # and avoids relying on a bearer token for connection tests.
            me = self.api.verify_credentials(skip_status=True)
            if me is None:
                return {"configured": False, "error": "Twitter authentication failed"}
            return {
                "configured": True,
                "status": "ok",
                "username": getattr(me, "screen_name", None),
                "id": getattr(me, "id", None),
                "name": getattr(me, "name", None),
            }
        except Exception as e:
            return {"configured": False, "error": str(e)}

    def post_tweet(self, post: Post, confirm: bool = True) -> dict:
        if post.platform != "twitter":
            return {"status": "error", "message": "Post is not for Twitter"}

        if len(post.content) > 280:
            return {"status": "error", "message": f"Tweet too long: {len(post.content)} chars"}

        self.console.print(Panel(
            post.content,
            title="[cyan]Tweet Preview[/cyan]",
            subtitle=f"[dim]{len(post.content)}/280 chars[/dim]"
        ))

        if self.dry_run:
            self.console.print("[yellow]DRY RUN - Tweet would be posted:[/yellow]")
            self.console.print(f"[dim]Content: {post.content}[/dim]")
            self.console.print(f"[dim]Timestamp: {datetime.now(timezone.utc).isoformat()}[/dim]")

            return {
                "status": "dry_run",
                "message": "Dry run completed successfully",
                "content": post.content,
                "char_count": len(post.content)
            }

        if confirm:
            self.console.print(f"\n[yellow]About to post this tweet to @{config_handle()}[/yellow]")
            response = input("Type 'POST' to confirm: ")
            if response != "POST":
                self.console.print("[red]Cancelled[/red]")
                return {"status": "cancelled", "message": "User cancelled"}

        try:
            result = self.client.create_tweet(text=post.content)

            post.status = PostStatus.POSTED.value
            post.posted_time = datetime.now(timezone.utc)
            post.post_id = str(result.data['id'])
            self.session.commit()

            self.console.print(f"[green]✓ Tweet posted successfully![/green]")
            self.console.print(f"[dim]Tweet ID: {result.data['id']}[/dim]")

            return {
                "status": "posted",
                "tweet_id": result.data['id'],
                "url": f"https://x.com/{config_handle()}/status/{result.data['id']}"
            }

        except Exception as e:
            post.status = PostStatus.FAILED.value
            self.session.commit()

            self.console.print(f"[red]Failed to post tweet: {e}[/red]")
            return {"status": "error", "message": str(e)}

    def post_by_id(self, post_id: int, confirm: bool = True) -> dict:
        post = self.session.query(Post).filter(Post.id == post_id).first()
        if not post:
            return {"status": "error", "message": f"Post #{post_id} not found"}

        if post.status == PostStatus.POSTED.value:
            return {"status": "error", "message": "Post already published"}

        if post.status != PostStatus.APPROVED.value and not self.dry_run:
            return {"status": "error", "message": "Post must be approved before posting"}

        return self.post_tweet(post, confirm=confirm)

    def post_next_approved(self, confirm: bool = True) -> dict:
        post = self.session.query(Post).filter(
            Post.platform == "twitter",
            Post.status == PostStatus.APPROVED.value
        ).order_by(Post.scheduled_time.asc()).first()

        if not post:
            return {"status": "error", "message": "No approved posts available"}

        return self.post_tweet(post, confirm=confirm)

    def get_pending_posts(self, limit: int = 10) -> list[Post]:
        return self.session.query(Post).filter(
            Post.platform == "twitter",
            Post.status.in_([PostStatus.PENDING.value, PostStatus.APPROVED.value])
        ).order_by(Post.scheduled_time.asc()).limit(limit).all()

    def dry_run_all(self):
        posts = self.get_pending_posts()
        self.console.print(f"\n[cyan]Dry run for {len(posts)} pending posts:[/cyan]\n")

        for post in posts:
            self.console.print(f"[dim]Post #{post.id} - {post.status}[/dim]")
            self.post_tweet(post, confirm=False)
            self.console.print()
