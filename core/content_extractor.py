import os
import json
import requests
from typing import Optional
from datetime import datetime

from .document_parser import parse_document, get_document_name
from .models import Quote, get_session, init_db

EXTRACTION_PROMPT = """You are an expert at extracting impactful, memorable quotes from documents.

Your task is to extract quotes that:
1. Contain specific wisdom or insight (not generic motivation)
2. Are self-contained and understandable without context
3. Are 280 characters or less (Twitter-compatible)
4. Relate to useful topics like: discipline, strategy, mindset, leadership, creativity, or personal growth

For each quote, provide:
- The quote text (cleaned up and polished for social media)
- A topic category from: ["Discipline", "Strategy", "Mindset", "Leadership", "Creativity", "Personal Growth", "Productivity", "Philosophy"]
- A quality score from 1-10 based on:
  - Clarity (is it clear and well-expressed?)
  - Actionability (can someone apply this?)
  - Uniqueness (is it a fresh perspective?)
  - Shareability (would people want to share this?)

CRITICAL GRAMMAR RULES:
- Fix ALL mid-sentence capitalization — convert emphasis capitals to lowercase unless they start a sentence.
- Use proper punctuation: em dashes (—) instead of ellipsis (...) where appropriate
- Remove filler words and spoken artifacts ("you know", "like", "um")
- Ensure each quote is grammatically correct and reads professionally
- The quote should look like polished written content, not a transcript

IMPORTANT:
- Extract 15-25 high-quality quotes per document
- Avoid quotes that are too conversational or need context
- Prioritize quotes that are insightful and shareable

Respond with a JSON array of objects with this structure:
{
  "quotes": [
    {
      "content": "The quote text here",
      "topic": "Category name",
      "quality_score": 8.5
    }
  ]
}

Only respond with valid JSON, no other text."""


class ContentExtractor:
    def __init__(self, api_key: Optional[str] = None, model: str = "llama-3.3-70b-versatile"):
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        if not self.api_key:
            raise ValueError("GROQ_API_KEY not set. Save it in Settings to enable document extraction.")
        self.model = model
        self.api_url = "https://api.groq.com/openai/v1/chat/completions"

    def extract_quotes_from_text(self, text: str, source_name: str) -> list[dict]:
        text_preview = text[:12000] if len(text) > 12000 else text

        response = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": EXTRACTION_PROMPT},
                    {"role": "user", "content": f"Extract impactful quotes from this document. Source: {source_name}\n\n{text_preview}"}
                ],
                "max_tokens": 4096,
                "temperature": 0.3
            },
            timeout=120
        )

        if response.status_code != 200:
            raise Exception(f"Groq API error: {response.status_code} - {response.text}")

        response_text = response.json()["choices"][0]["message"]["content"]

        try:
            data = json.loads(response_text)
            quotes = data.get("quotes", [])
        except json.JSONDecodeError:
            start = response_text.find('[')
            end = response_text.rfind(']') + 1
            if start != -1 and end > start:
                quotes = json.loads(response_text[start:end])
            else:
                quotes = []

        for quote in quotes:
            quote["source"] = source_name

        return quotes

    def extract_from_document(self, file_path: str) -> list[dict]:
        text = parse_document(file_path)
        source_name = get_document_name(file_path)
        return self.extract_quotes_from_text(text, source_name)

    def save_quotes_to_db(self, quotes: list[dict], session=None) -> int:
        if session is None:
            init_db()
            session = get_session()

        saved_count = 0
        for quote_data in quotes:
            existing = session.query(Quote).filter(
                Quote.content == quote_data["content"]
            ).first()

            if existing:
                continue

            quote = Quote(
                content=quote_data["content"],
                source=quote_data.get("source", "Unknown"),
                topic=quote_data.get("topic", "General"),
                quality_score=quote_data.get("quality_score", 5.0),
                approved=False,
                created_at=datetime.utcnow()
            )
            session.add(quote)
            saved_count += 1

        session.commit()
        return saved_count

    def extract_and_save(self, file_path: str) -> tuple[int, int]:
        quotes = self.extract_from_document(file_path)
        saved = self.save_quotes_to_db(quotes)
        return len(quotes), saved
