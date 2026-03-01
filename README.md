# Social Kanban

A self-hostable social media content management dashboard. Plan, create, and schedule posts with a drag-and-drop kanban board.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and edit config
cp .env.example .env

# 3. Run
python dashboard.py
```

Open [http://localhost:5001](http://localhost:5001).

## Features

- Drag-and-drop kanban board for content pipeline
- Quote extraction from PDF/DOCX documents (via AI)
- AI-powered post formatting
- Image generation with multiple themes
- Daily Stoic card generator
- Multi-platform posting (Twitter/X, Facebook, Instagram)
- Optional password-protected dashboard

## Configuration

Edit `config/settings.yaml` to set your brand name, handle, tagline, and default hashtags. Environment variables override YAML settings — see `.env.example` for all options.

### Optional Services

| Service | Env Var | Purpose |
|---------|---------|---------|
| Anthropic API | `ANTHROPIC_API_KEY` | AI post formatting, stoic cards |
| Groq API | `GROQ_API_KEY` | Document quote extraction |
| Twitter/X | `TWITTER_API_KEY` + 3 more | Post to X |
| Facebook | `FACEBOOK_PAGE_ID` + token | Post to Facebook |
| Instagram | `INSTAGRAM_ACCOUNT_ID` | Post to Instagram |
| Cloudinary | `CLOUDINARY_CLOUD_NAME` + keys | Image hosting |
| PostgreSQL | `DATABASE_URL` | Production database |
| Auth | `DASHBOARD_PASSWORD` | Dashboard login |

All services are optional. The dashboard works locally with just SQLite (the default).

## Docker

```bash
cp .env.example .env
# Edit .env with your settings
docker compose up -d
```

## CLI Usage

```bash
python main.py init              # Initialize database
python main.py extract doc.pdf   # Extract quotes from document
python main.py quotes            # List quotes
python main.py review-quotes     # Interactively review quotes
python main.py generate          # Generate posts from approved quotes
python main.py posts             # List scheduled posts
python main.py status            # Show system status
```

## Project Structure

```
social-kanban/
├── dashboard.py              # Flask web dashboard
├── main.py                   # CLI interface
├── core/
│   ├── config.py             # Centralized config loader
│   ├── models.py             # SQLAlchemy models
│   ├── post_planner.py       # AI post formatting
│   ├── content_extractor.py  # Document quote extraction
│   ├── approval_system.py    # Quote/post approval workflow
│   └── document_parser.py    # PDF/DOCX/TXT parsing
├── integrations/
│   ├── twitter_client.py     # X/Twitter API
│   ├── facebook_client.py    # Facebook Page API
│   ├── instagram_client.py   # Instagram Graph API
│   └── cloudinary_client.py  # Image hosting
├── config/
│   └── settings.yaml         # Brand & app settings
├── data/
│   └── daily_stoic.json      # Stoic wisdom entries
├── tests/
└── seed_sample_data.py       # Sample data seeder
```

## License

MIT
