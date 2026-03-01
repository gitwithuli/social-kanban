from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich import box

from .models import Quote, Post, PostStatus, get_session, init_db


class ApprovalSystem:
    def __init__(self, session=None):
        self.console = Console()
        init_db()
        self.session = session or get_session()

    def display_quote(self, quote: Quote, show_actions: bool = True):
        status_icon = "[green]✓[/green]" if quote.approved else "[yellow]○[/yellow]"
        score_color = "green" if quote.quality_score >= 7 else "yellow" if quote.quality_score >= 5 else "red"

        panel_content = Text()
        panel_content.append(f'"{quote.content}"', style="italic")
        panel_content.append(f"\n\nTopic: ", style="dim")
        panel_content.append(f"{quote.topic}", style="cyan")
        panel_content.append(f"  |  Score: ", style="dim")
        panel_content.append(f"{quote.quality_score:.1f}", style=score_color)
        panel_content.append(f"  |  Source: ", style="dim")
        panel_content.append(f"{quote.source}", style="blue")

        if quote.used_count > 0:
            panel_content.append(f"\nUsed: {quote.used_count}x", style="dim")
            if quote.last_used:
                panel_content.append(f" (last: {quote.last_used.strftime('%Y-%m-%d')})", style="dim")

        title = f"{status_icon} Quote #{quote.id}"
        self.console.print(Panel(panel_content, title=title, border_style="blue", box=box.ROUNDED))

        if show_actions:
            self.console.print("[dim]Actions: [a]pprove  [r]eject  [e]dit  [n]ext  [q]uit[/dim]\n")

    def display_post(self, post: Post, show_actions: bool = True):
        status_icons = {
            PostStatus.PENDING.value: "[yellow]⏳[/yellow]",
            PostStatus.APPROVED.value: "[green]✓[/green]",
            PostStatus.POSTED.value: "[blue]📤[/blue]",
            PostStatus.REJECTED.value: "[red]✗[/red]",
            PostStatus.FAILED.value: "[red]⚠[/red]",
        }
        icon = status_icons.get(post.status, "[white]?[/white]")

        panel_content = Text()
        panel_content.append(post.content, style="white")
        panel_content.append(f"\n\nPlatform: ", style="dim")
        panel_content.append(f"{post.platform}", style="cyan")
        panel_content.append(f"  |  Status: ", style="dim")
        panel_content.append(f"{post.status}", style="yellow")

        if post.scheduled_time:
            panel_content.append(f"\nScheduled: ", style="dim")
            panel_content.append(f"{post.scheduled_time.strftime('%Y-%m-%d %H:%M UTC')}", style="green")

        char_count = len(post.content)
        char_color = "green" if char_count <= 280 else "red"
        panel_content.append(f"\nCharacters: ", style="dim")
        panel_content.append(f"{char_count}/280", style=char_color)

        title = f"{icon} Post #{post.id}"
        self.console.print(Panel(panel_content, title=title, border_style="cyan", box=box.ROUNDED))

        if show_actions:
            self.console.print("[dim]Actions: [a]pprove  [r]eject  [e]dit  [n]ext  [q]uit[/dim]\n")

    def list_quotes(self, approved: Optional[bool] = None, min_score: float = 0.0, limit: int = 50):
        query = self.session.query(Quote).filter(Quote.quality_score >= min_score)

        if approved is not None:
            query = query.filter(Quote.approved == approved)

        quotes = query.order_by(Quote.quality_score.desc()).limit(limit).all()

        table = Table(title="Quotes", box=box.ROUNDED)
        table.add_column("ID", style="dim", width=5)
        table.add_column("Status", width=3)
        table.add_column("Quote", max_width=60)
        table.add_column("Topic", style="cyan", width=15)
        table.add_column("Score", justify="right", width=6)
        table.add_column("Used", justify="right", width=5)

        for q in quotes:
            status = "[green]✓[/green]" if q.approved else "[yellow]○[/yellow]"
            content = q.content[:57] + "..." if len(q.content) > 60 else q.content
            score_color = "green" if q.quality_score >= 7 else "yellow" if q.quality_score >= 5 else "red"

            table.add_row(
                str(q.id),
                status,
                content,
                q.topic or "-",
                f"[{score_color}]{q.quality_score:.1f}[/{score_color}]",
                str(q.used_count)
            )

        self.console.print(table)
        self.console.print(f"\nTotal: {len(quotes)} quotes shown")

    def list_posts(self, status: Optional[str] = None, limit: int = 20):
        query = self.session.query(Post)

        if status:
            query = query.filter(Post.status == status)

        posts = query.order_by(Post.scheduled_time.asc()).limit(limit).all()

        table = Table(title="Posts", box=box.ROUNDED)
        table.add_column("ID", style="dim", width=5)
        table.add_column("Status", width=10)
        table.add_column("Platform", style="cyan", width=10)
        table.add_column("Content", max_width=50)
        table.add_column("Scheduled", width=18)
        table.add_column("Chars", justify="right", width=6)

        status_styles = {
            PostStatus.PENDING.value: "yellow",
            PostStatus.APPROVED.value: "green",
            PostStatus.POSTED.value: "blue",
            PostStatus.REJECTED.value: "red",
            PostStatus.FAILED.value: "red",
        }

        for p in posts:
            content = p.content[:47] + "..." if len(p.content) > 50 else p.content
            scheduled = p.scheduled_time.strftime('%Y-%m-%d %H:%M') if p.scheduled_time else "-"
            style = status_styles.get(p.status, "white")
            char_count = len(p.content)
            char_style = "green" if char_count <= 280 else "red"

            table.add_row(
                str(p.id),
                f"[{style}]{p.status}[/{style}]",
                p.platform,
                content,
                scheduled,
                f"[{char_style}]{char_count}[/{char_style}]"
            )

        self.console.print(table)
        self.console.print(f"\nTotal: {len(posts)} posts shown")

    def approve_quote(self, quote_id: int) -> bool:
        quote = self.session.query(Quote).filter(Quote.id == quote_id).first()
        if not quote:
            self.console.print(f"[red]Quote #{quote_id} not found[/red]")
            return False

        quote.approved = True
        self.session.commit()
        self.console.print(f"[green]✓ Quote #{quote_id} approved[/green]")
        return True

    def reject_quote(self, quote_id: int) -> bool:
        quote = self.session.query(Quote).filter(Quote.id == quote_id).first()
        if not quote:
            self.console.print(f"[red]Quote #{quote_id} not found[/red]")
            return False

        quote.approved = False
        self.session.commit()
        self.console.print(f"[yellow]○ Quote #{quote_id} marked as not approved[/yellow]")
        return True

    def approve_post(self, post_id: int) -> bool:
        post = self.session.query(Post).filter(Post.id == post_id).first()
        if not post:
            self.console.print(f"[red]Post #{post_id} not found[/red]")
            return False

        post.status = PostStatus.APPROVED.value
        post.approved_at = datetime.utcnow()
        self.session.commit()
        self.console.print(f"[green]✓ Post #{post_id} approved[/green]")
        return True

    def reject_post(self, post_id: int) -> bool:
        post = self.session.query(Post).filter(Post.id == post_id).first()
        if not post:
            self.console.print(f"[red]Post #{post_id} not found[/red]")
            return False

        post.status = PostStatus.REJECTED.value
        self.session.commit()
        self.console.print(f"[red]✗ Post #{post_id} rejected[/red]")
        return True

    def edit_quote(self, quote_id: int) -> bool:
        quote = self.session.query(Quote).filter(Quote.id == quote_id).first()
        if not quote:
            self.console.print(f"[red]Quote #{quote_id} not found[/red]")
            return False

        self.display_quote(quote, show_actions=False)
        self.console.print("\n[cyan]Edit quote content (press Enter to keep current):[/cyan]")
        new_content = Prompt.ask("Content", default=quote.content)

        if new_content and new_content != quote.content:
            quote.content = new_content
            self.session.commit()
            self.console.print(f"[green]✓ Quote #{quote_id} updated[/green]")
            return True

        self.console.print("[dim]No changes made[/dim]")
        return False

    def edit_post(self, post_id: int) -> bool:
        post = self.session.query(Post).filter(Post.id == post_id).first()
        if not post:
            self.console.print(f"[red]Post #{post_id} not found[/red]")
            return False

        self.display_post(post, show_actions=False)
        self.console.print("\n[cyan]Edit post content (press Enter to keep current):[/cyan]")
        new_content = Prompt.ask("Content", default=post.content)

        if new_content and new_content != post.content:
            post.content = new_content
            self.session.commit()
            self.console.print(f"[green]✓ Post #{post_id} updated[/green]")
            return True

        self.console.print("[dim]No changes made[/dim]")
        return False

    def review_quotes_interactive(self, min_score: float = 0.0):
        quotes = self.session.query(Quote).filter(
            Quote.approved == False,
            Quote.quality_score >= min_score
        ).order_by(Quote.quality_score.desc()).all()

        if not quotes:
            self.console.print("[yellow]No pending quotes to review[/yellow]")
            return

        self.console.print(f"\n[cyan]Reviewing {len(quotes)} pending quotes[/cyan]\n")

        for i, quote in enumerate(quotes):
            self.console.print(f"[dim]({i + 1}/{len(quotes)})[/dim]")
            self.display_quote(quote)

            action = Prompt.ask(
                "Action",
                choices=["a", "r", "e", "n", "q"],
                default="n"
            )

            if action == "a":
                self.approve_quote(quote.id)
            elif action == "r":
                self.reject_quote(quote.id)
            elif action == "e":
                self.edit_quote(quote.id)
            elif action == "q":
                self.console.print("[dim]Exiting review[/dim]")
                break

            self.console.print()

    def review_posts_interactive(self):
        posts = self.session.query(Post).filter(
            Post.status == PostStatus.PENDING.value
        ).order_by(Post.scheduled_time.asc()).all()

        if not posts:
            self.console.print("[yellow]No pending posts to review[/yellow]")
            return

        self.console.print(f"\n[cyan]Reviewing {len(posts)} pending posts[/cyan]\n")

        for i, post in enumerate(posts):
            self.console.print(f"[dim]({i + 1}/{len(posts)})[/dim]")
            self.display_post(post)

            action = Prompt.ask(
                "Action",
                choices=["a", "r", "e", "n", "q"],
                default="n"
            )

            if action == "a":
                self.approve_post(post.id)
            elif action == "r":
                self.reject_post(post.id)
            elif action == "e":
                self.edit_post(post.id)
            elif action == "q":
                self.console.print("[dim]Exiting review[/dim]")
                break

            self.console.print()

    def batch_approve_quotes(self, min_score: float = 7.0):
        quotes = self.session.query(Quote).filter(
            Quote.approved == False,
            Quote.quality_score >= min_score
        ).all()

        if not quotes:
            self.console.print("[yellow]No quotes matching criteria[/yellow]")
            return 0

        self.console.print(f"\n[cyan]Found {len(quotes)} quotes with score >= {min_score}[/cyan]")

        if Confirm.ask("Approve all?"):
            for q in quotes:
                q.approved = True
            self.session.commit()
            self.console.print(f"[green]✓ Approved {len(quotes)} quotes[/green]")
            return len(quotes)

        return 0

    def get_stats(self):
        total_quotes = self.session.query(Quote).count()
        approved_quotes = self.session.query(Quote).filter(Quote.approved == True).count()
        pending_quotes = total_quotes - approved_quotes

        total_posts = self.session.query(Post).count()
        pending_posts = self.session.query(Post).filter(Post.status == PostStatus.PENDING.value).count()
        approved_posts = self.session.query(Post).filter(Post.status == PostStatus.APPROVED.value).count()
        posted = self.session.query(Post).filter(Post.status == PostStatus.POSTED.value).count()

        table = Table(title="System Stats", box=box.ROUNDED)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        table.add_row("Total Quotes", str(total_quotes))
        table.add_row("Approved Quotes", f"[green]{approved_quotes}[/green]")
        table.add_row("Pending Quotes", f"[yellow]{pending_quotes}[/yellow]")
        table.add_row("", "")
        table.add_row("Total Posts", str(total_posts))
        table.add_row("Pending Posts", f"[yellow]{pending_posts}[/yellow]")
        table.add_row("Approved Posts", f"[green]{approved_posts}[/green]")
        table.add_row("Posted", f"[blue]{posted}[/blue]")

        self.console.print(table)
