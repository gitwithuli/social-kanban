#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from datetime import datetime

import click
from rich.console import Console
from rich.panel import Panel
from rich import box
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from core.models import init_db, get_session, Quote, Post, PostStatus
from core.document_parser import parse_document
from core.content_extractor import ContentExtractor
from core.approval_system import ApprovalSystem
from core.post_planner import PostPlanner
from integrations.twitter_client import TwitterClient

console = Console()


@click.group()
def cli():
    """Social Kanban — Content Management CLI"""
    pass


@cli.command()
@click.argument('source', type=click.Path(exists=True))
@click.option('--min-score', default=5.0, help='Minimum quality score to save')
def extract(source: str, min_score: float):
    """Extract quotes from a document (PDF, DOCX, TXT)"""
    console.print(f"\n[cyan]Extracting quotes from: {source}[/cyan]\n")

    try:
        extractor = ContentExtractor()
        extracted, saved = extractor.extract_and_save(source)

        console.print(f"[green]✓ Extracted {extracted} quotes[/green]")
        console.print(f"[green]✓ Saved {saved} new quotes to database[/green]")

        if extracted > saved:
            console.print(f"[dim]({extracted - saved} duplicates skipped)[/dim]")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@cli.command()
@click.option('--all', 'show_all', is_flag=True, help='Show all quotes (including approved)')
@click.option('--approved', is_flag=True, help='Show only approved quotes')
@click.option('--min-score', default=0.0, help='Minimum quality score filter')
@click.option('--limit', default=50, help='Maximum quotes to show')
def quotes(show_all: bool, approved: bool, min_score: float, limit: int):
    """List quotes in the database"""
    system = ApprovalSystem()

    if approved:
        system.list_quotes(approved=True, min_score=min_score, limit=limit)
    elif show_all:
        system.list_quotes(approved=None, min_score=min_score, limit=limit)
    else:
        system.list_quotes(approved=False, min_score=min_score, limit=limit)


@cli.command('review-quotes')
@click.option('--min-score', default=5.0, help='Minimum quality score to review')
def review_quotes(min_score: float):
    """Interactively review and approve quotes"""
    system = ApprovalSystem()
    system.review_quotes_interactive(min_score=min_score)


@cli.command('approve-quote')
@click.argument('quote_id', type=int)
def approve_quote(quote_id: int):
    """Approve a specific quote"""
    system = ApprovalSystem()
    system.approve_quote(quote_id)


@cli.command('batch-approve')
@click.option('--min-score', default=7.0, help='Minimum quality score')
def batch_approve(min_score: float):
    """Batch approve quotes above a quality threshold"""
    system = ApprovalSystem()
    system.batch_approve_quotes(min_score=min_score)


@cli.command()
@click.option('--days', default=7, help='Number of days to generate posts for')
@click.option('--posts-per-day', default=1, help='Posts per day')
@click.option('--no-ai', is_flag=True, help='Use template formatting instead of AI')
def generate(days: int, posts_per_day: int, no_ai: bool):
    """Generate posts from approved quotes"""
    console.print(f"\n[cyan]Generating {days * posts_per_day} posts for {days} days[/cyan]\n")

    planner = PostPlanner()
    posts = planner.generate_posts(
        days=days,
        posts_per_day=posts_per_day,
        use_ai=not no_ai
    )

    if posts:
        console.print(f"[green]✓ Generated {len(posts)} posts[/green]")
        console.print("[dim]Run 'python main.py posts' to see them[/dim]")
    else:
        console.print("[yellow]No approved quotes available to generate posts[/yellow]")


@cli.command()
@click.option('--status', type=click.Choice(['pending', 'approved', 'posted', 'rejected']), help='Filter by status')
@click.option('--limit', default=20, help='Maximum posts to show')
def posts(status: str, limit: int):
    """List scheduled posts"""
    system = ApprovalSystem()
    system.list_posts(status=status, limit=limit)


@cli.command()
@click.option('--days', default=7, help='Show schedule for next N days')
def schedule(days: int):
    """Show posting schedule"""
    planner = PostPlanner()
    scheduled = planner.get_schedule(days=days)

    if not scheduled:
        console.print("[yellow]No posts scheduled[/yellow]")
        return

    console.print(f"\n[cyan]Schedule for next {days} days:[/cyan]\n")

    for post in scheduled:
        status_icon = "[green]✓[/green]" if post.status == PostStatus.APPROVED.value else "[yellow]○[/yellow]"
        time_str = post.scheduled_time.strftime('%Y-%m-%d %H:%M') if post.scheduled_time else "Not scheduled"
        content_preview = post.content[:50] + "..." if len(post.content) > 50 else post.content

        console.print(f"{status_icon} [{time_str}] {content_preview}")


@cli.command()
def review():
    """Interactively review pending posts"""
    system = ApprovalSystem()
    system.review_posts_interactive()


@cli.command('approve-post')
@click.argument('post_id', type=int)
def approve_post(post_id: int):
    """Approve a specific post"""
    system = ApprovalSystem()
    system.approve_post(post_id)


@cli.command('dry-run')
@click.option('--post-id', type=int, help='Specific post ID to dry-run')
@click.option('--all', 'run_all', is_flag=True, help='Dry-run all pending posts')
def dry_run(post_id: int, run_all: bool):
    """Test posting without actually posting"""
    client = TwitterClient(dry_run=True)

    if post_id:
        client.post_by_id(post_id, confirm=False)
    elif run_all:
        client.dry_run_all()
    else:
        client.post_next_approved(confirm=False)


@cli.command()
@click.option('--post-id', type=int, help='Specific post ID to post')
@click.option('--next', 'post_next', is_flag=True, help='Post next approved post')
@click.option('--confirm/--no-confirm', default=True, help='Require confirmation')
@click.option('--dry-run', 'dry', is_flag=True, help='Run in dry-run mode')
def post(post_id: int, post_next: bool, confirm: bool, dry: bool):
    """Post to Twitter/X"""
    client = TwitterClient(dry_run=dry)

    if not client.is_configured() and not dry:
        console.print("[red]Twitter API not configured. Add credentials to .env file.[/red]")
        console.print("[dim]Or use --dry-run to test without posting[/dim]")
        return

    if post_id:
        result = client.post_by_id(post_id, confirm=confirm)
    elif post_next:
        result = client.post_next_approved(confirm=confirm)
    else:
        console.print("[yellow]Specify --post-id or --next[/yellow]")
        return

    if result.get("status") == "error":
        console.print(f"[red]Error: {result.get('message')}[/red]")
    elif result.get("url"):
        console.print(f"[blue]View at: {result['url']}[/blue]")


@cli.command()
def status():
    """Show system status and stats"""
    from core.config import brand_name, tagline
    console.print(Panel(
        f"[cyan]{brand_name()}[/cyan]\n"
        f"[dim]{tagline()}[/dim]",
        box=box.ROUNDED
    ))

    system = ApprovalSystem()
    system.get_stats()

    client = TwitterClient(dry_run=True)
    if client.is_configured():
        console.print("\n[green]✓ Twitter API configured[/green]")
    else:
        console.print("\n[yellow]○ Twitter API not configured[/yellow]")


@cli.command()
def init():
    """Initialize database and check configuration"""
    from core.config import brand_name as _brand_name
    console.print(f"[cyan]Initializing {_brand_name()}...[/cyan]\n")

    init_db()
    console.print("[green]✓ Database initialized[/green]")

    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        console.print("[green]✓ .env file found[/green]")
    else:
        console.print("[yellow]○ .env file not found - copy .env.example to .env[/yellow]")

    if os.getenv("ANTHROPIC_API_KEY"):
        console.print("[green]✓ Anthropic API key configured[/green]")
    else:
        console.print("[yellow]○ Anthropic API key not set[/yellow]")

    if os.getenv("TWITTER_API_KEY"):
        console.print("[green]✓ Twitter API key configured[/green]")
    else:
        console.print("[yellow]○ Twitter API not configured (optional for dry-run)[/yellow]")

    console.print("\n[dim]Run 'python main.py extract <document>' to start extracting quotes[/dim]")


if __name__ == '__main__':
    cli()
