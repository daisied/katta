import asyncio
import json
import logging
import os
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import httpx
from openai import AsyncOpenAI

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scout")

DB_PATH = Path("/app/app/data/scout.db")
SOURCES_PATH = Path("/app/app/data/sources.json")

class Scout:
    """
    The Cortex Scout.
    Scans sources, filters for 'Utility/Advantage', and queues intel for Katta.
    """
    def __init__(self):
        self._init_db()
        self.sources = self._load_sources()
        self.client = AsyncOpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1"
        )

    def _init_db(self):
        """Initialize the SQLite database."""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Table: Seen Links (Deduplication)
            c.execute('''CREATE TABLE IF NOT EXISTS seen_links
                         (url TEXT PRIMARY KEY, timestamp TEXT, source TEXT)''')
            
            # Table: Intel Queue (Output for Katta)
            c.execute('''CREATE TABLE IF NOT EXISTS intel_queue
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          title TEXT,
                          summary TEXT,
                          url TEXT,
                          score INTEGER,
                          status TEXT DEFAULT 'PENDING',
                          timestamp TEXT)''')
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB Init Error: {e}")

    def _load_sources(self) -> Dict[str, Any]:
        """Load sources from JSON."""
        if not SOURCES_PATH.exists():
            return {
                "rss_feeds": [],
                "subreddits": [],
                "github_dorks": [],
                "search_queries": [],
            }
        try:
            with open(SOURCES_PATH, 'r') as f:
                data = json.load(f)
            data.setdefault("rss_feeds", [])
            data.setdefault("subreddits", [])
            data.setdefault("github_dorks", [])
            data.setdefault("search_queries", [])
            return data
        except Exception as e:
            logger.error(f"Failed to load sources: {e}")
            return {}

    def is_seen(self, url: str) -> bool:
        """Check if URL has been seen before."""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT 1 FROM seen_links WHERE url = ?", (url,))
            result = c.fetchone()
            conn.close()
            return result is not None
        except Exception as e:
            logger.error(f"DB Read Error: {e}")
            return False

    def mark_seen(self, url: str, source: str):
        """Mark URL as seen."""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO seen_links (url, timestamp, source) VALUES (?, ?, ?)",
                      (url, datetime.now().isoformat(), source))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB Write Error: {e}")

    def queue_intel(self, title: str, summary: str, url: str, score: int):
        """Add high-quality intel to the queue."""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO intel_queue (title, summary, url, score, timestamp) VALUES (?, ?, ?, ?, ?)",
                      (title, summary, url, score, datetime.now().isoformat()))
            conn.commit()
            conn.close()
            logger.info(f"Queued High-Signal Intel: {title}")
        except Exception as e:
            logger.error(f"DB Queue Error: {e}")

    async def fetch_rss(self, url: str) -> List[Dict]:
        """Simple XML RSS parser using httpx."""
        items = []
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(url, headers={'User-Agent': 'Katta-Scout/1.0'})
                response.raise_for_status()
                xml_content = response.content
                
                root = ET.fromstring(xml_content)
                
                # Handle Atom vs RSS (basic check)
                for item in root.findall(".//item") or root.findall(".//entry"):
                    title = item.find("title").text if item.find("title") is not None else "No Title"
                    link_elem = item.find("link")
                    link = ""
                    if link_elem is not None:
                        # RSS uses text content; Atom often uses href attribute.
                        link = (link_elem.text or "").strip() or (link_elem.attrib.get("href", "") or "").strip()
                    desc = (
                        (item.find("description").text if item.find("description") is not None else "")
                        or (item.find("summary").text if item.find("summary") is not None else "")
                    )
                    
                    if link and not self.is_seen(link):
                        items.append({"title": title, "link": link, "desc": desc})
        except Exception as e:
            logger.warning(f"RSS Fetch Error ({url}): {e}")
        return items

    def get_pending_intel(self) -> List[Dict]:
        """Get processed intel ready for notification."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM intel_queue WHERE status = 'PENDING' ORDER BY score DESC LIMIT 1")
            rows = c.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"DB Read Error: {e}")
            return []

    def mark_sent(self, item_id: int):
        """Mark intel as sent."""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE intel_queue SET status = 'SENT' WHERE id = ?", (item_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB Update Error: {e}")

    async def filter_content(self, title: str, content: str) -> int:
        """Score content for Actionable Utility."""
        prompt = f"""
        ANALYZE THIS INTEL (0-100 SCORE):
        Title: {title}
        Content: {content[:1000]}

        SCORING CRITERIA:
        - 100: ACTIONABLE EXPLOIT. Infinite money, item dupe, 90% off glitch, free pro tier. Usable by a normal person/gamer.
        - 80: High Utility. Tool to bypass restrictions, useful reversal tool, new jailbreak.
        - 20: General Tech News. "Company X acquired Y".
        - 0: Security Advisory / Patch Note / Vulnerability Warning. "Buffer overflow in LibXYZ", "CISA Alert".
        - 0: Spam / Ad / Affiliate Link.

        OUTPUT FORMAT: Just the number.
        """
        try:
            response = await self.client.chat.completions.create(
                model=os.getenv("LLM_MODEL", "gpt-4o"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5
            )
            score_text = response.choices[0].message.content.strip()
            # Handle cases where LLM might output non-digits
            import re
            match = re.search(r'\d+', score_text)
            if match:
                return int(match.group(0))
            return 0
        except Exception as e:
            logger.error(f"LLM Filter Error: {e}")
            return 0

    async def fetch_github(self):
        """Run GitHub Dorks using httpx."""
        dorks = self.sources.get("github_dorks", [])
        async with httpx.AsyncClient(timeout=10.0) as client:
            for dork in dorks:
                try:
                    # Use strict search for code
                    safe_dork = urllib.parse.quote(dork)
                    url = f"https://api.github.com/search/code?q={safe_dork}&sort=updated&order=desc"

                    headers = {
                        'User-Agent': 'Katta-Scout/1.0',
                        'Accept': 'application/vnd.github.v3+json',
                    }
                    github_token = os.getenv('GITHUB_TOKEN')
                    if github_token:
                        headers['Authorization'] = f"token {github_token}"

                    response = await client.get(url, headers=headers)
                    
                    if response.status_code == 200:
                        data = response.json()
                        for item in data.get('items', [])[:5]: # Top 5 only
                            repo_url = item.get('html_url', '')
                            desc = f"Found via dork: {dork}. File: {item.get('name')}"
                            
                            if repo_url and not self.is_seen(repo_url):
                                score = await self.filter_content(f"GitHub Hit: {item.get('name')}", desc)
                                if score >= 70:
                                    self.queue_intel(f"GitHub: {item.get('name')}", desc, repo_url, score)
                                self.mark_seen(repo_url, "github")
                    
                    await asyncio.sleep(2) # Rate limit nice
                except Exception as e:
                    logger.warning(f"GitHub Dork Error ({dork}): {e}")

    def reload_sources(self):
        """Reload sources from JSON."""
        self.sources = self._load_sources()
        logger.info(f"Sources reloaded. {len(self.sources.get('rss_feeds', []))} feeds, {len(self.sources.get('search_queries', []))} queries.")

    async def fetch_twitter(self):
        """Run Twitter/X searches via bird CLI (non-blocking)."""
        # Learnable queries from sources.json
        queries = self.sources.get("search_queries", ["glitch", "exploit"])
        
        for query in queries:
            try:
                # bird search "query" --limit 5
                process = await asyncio.create_subprocess_exec(
                    "bird", "search", query, "--limit", "5",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode == 0:
                    _ = stdout.decode()
                    # TODO: Parse bird output if we want to extract links.
                    # For now we just logged it in the old version, keeping it simple.
                    # If we wanted to parse, we'd look for urls here.
                    pass
                else:
                    logger.warning(f"Bird CLI failed for {query}: {stderr.decode()[:100]}")
            except Exception as e:
                logger.warning(f"Bird CLI Execution Error ({query}): {e}")

    async def run_cycle(self):
        """Run one full scan cycle."""
        self.reload_sources() # Hot-reload sources every cycle
        logger.info("Starting Scout Cycle...")
        
        # 1. RSS Feeds
        feeds = self.sources.get("rss_feeds", [])
        for feed in feeds:
            url = feed.get("url")
            logger.info(f"Checking {url}...")
            items = await self.fetch_rss(url)
            for item in items:
                score = await self.filter_content(item['title'], item['desc'])
                if score >= 70:
                    self.queue_intel(item['title'], item['desc'], item['link'], score)
                self.mark_seen(item['link'], "rss")
        
        # 2. Reddit (via RSS)
        subs = self.sources.get("subreddits", [])
        for sub in subs:
            url = f"https://www.reddit.com/r/{sub}/new.rss"
            logger.info(f"Checking r/{sub}...")
            items = await self.fetch_rss(url)
            for item in items:
                score = await self.filter_content(item['title'], item['desc'])
                if score >= 70:
                    self.queue_intel(item['title'], item['desc'], item['link'], score)
                self.mark_seen(item['link'], "reddit")

        # 3. GitHub
        logger.info("Checking GitHub Dorks...")
        await self.fetch_github()

        logger.info("Cycle complete.")

if __name__ == "__main__":
    scout = Scout()
    asyncio.run(scout.run_cycle())
