import asyncio
import json
import logging
import os
import re

from openai import AsyncOpenAI

from app.core.session_logger import SessionLogger
from app.core.tools import CORE_TOOLS

logger = logging.getLogger(__name__)

# Maximum tool calls allowed per LLM response to prevent runaway behavior
MAX_TOOL_CALLS_PER_TURN = 10

MEMORY_FILE = "/app/app/data/memory.md"
CONVERSATION_STATE_FILE = "/app/app/data/conversation_state.json"

DEFAULT_MEMORY_TEMPLATE = """# Agent Memory
This file is your persistent memory.
Store strictly FACTS here:
- User preferences
- Installed tools/packages
- Important context
"""

# --- URL Grounding Validation ---
# Catches fabricated URLs that weren't returned by any tool.

_URL_PATTERN = re.compile(r'https?://[^\s<>\[\]()\"\'`,\u200b]+')

def _extract_urls(text: str) -> set[str]:
    """Extract all URLs from text, cleaning trailing punctuation."""
    urls = set()
    for match in _URL_PATTERN.findall(str(text)):
        clean = match.rstrip('.,;:!?\'\">)')
        if len(clean) > 12:
            urls.add(clean)
    return urls


def _check_ungrounded_urls(content: str, tool_urls: set) -> str:
    """Append a warning if the response contains URLs not from tool results."""
    if not content:
        return content
    response_urls = _extract_urls(content)
    if not response_urls:
        return content

    ungrounded = set()
    for url in response_urls:
        # Check if URL (or close variant) appeared in tool results
        matched = any(url in tool_url or tool_url in url for tool_url in tool_urls)
        if not matched:
            ungrounded.add(url)

    if ungrounded:
        logger.warning(f"Ungrounded URLs detected in response: {ungrounded}")
        content += "\n\n⚠️ unverified links detected (not returned by tools this chat): " + ", ".join(sorted(ungrounded))
    return content


_RESEARCH_HINTS = (
    "what is", "who is", "when did", "where is", "why is", "how does", "how do",
    "latest", "news", "price", "cost", "compare", "vs", "versus", "true", "fact check",
    "source", "citation", "verify", "research", "find", "look up", "is it", "are there",
    "leak", "rumor", "release", "announce", "launch", "drop", "new ", "update",
    "cheap", "deal", "discount", "exploit", "hack", "trick", "way to", "how to get",
    "alternative", "workaround", "loophole", "free", "budget", "save", "method",
    "underground", "hidden", "secret", "obscure", "niche",
)

# Topics that MUST use deep_research, not just web_search.
# This is intentionally broad — deep_research is always better than web_search
# for anything requiring real answers.
_DEEP_RESEARCH_HINTS = (
    # News / releases / leaks
    "leak", "rumor", "new ai", "new model", "latest", "what's new", "any new",
    "release", "announce", "drop", "update",
    # Opinions / recommendations
    "best ", "funniest", "worst ", "recommend", "opinion", "review", "worth",
    "favorite", "top ", "ranking",
    # Research / finding stuff
    "find", "looking for", "search", "dig", "discover", "uncover",
    "cheap", "deal", "discount", "exploit", "hack", "trick", "way to",
    "how to get", "how to find", "where to get", "where to find",
    "alternative", "workaround", "loophole", "free", "budget",
    "underground", "hidden", "secret", "obscure", "niche",
    # Comparisons / analysis
    "compare", "vs", "versus", "difference between", "which is better",
    "pros and cons", "advantage",
    # Controversy / current events
    "controversy", "scandal", "drama", "debate", "true",
    "is it true", "fact check", "legit", "scam",
)


def _requires_research(user_message: str) -> bool:
    """Heuristic: determine if the user asked for factual/research-backed output."""
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if any(text.startswith(prefix) for prefix in ("write ", "code ", "fix ", "refactor ", "implement ")):
        return False
    # Questions about the bot itself don't need web research
    if any(w in text for w in ("which model are you", "what model are you", "who are you", "what are you",
                                "your name", "about yourself", "introduce yourself")):
        return False
    if any(hint in text for hint in _RESEARCH_HINTS):
        return True
    if "?" in text and any(
        token in text for token in ("what", "who", "when", "where", "why", "how", "is ", "are ", "does ", "did ")
    ):
        return True
    return False


def _load_memory() -> str:
    """Loads memory.md content. Creates default if missing."""
    try:
        if not os.path.exists(MEMORY_FILE):
            # Ensure directory exists
            os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
            with open(MEMORY_FILE, 'w') as f:
                f.write(DEFAULT_MEMORY_TEMPLATE)
            logger.info(f"Created default memory file: {MEMORY_FILE}")
            return DEFAULT_MEMORY_TEMPLATE
        
        with open(MEMORY_FILE, 'r') as f:
            content = f.read()
        logger.debug(f"Loaded memory ({len(content)} chars)")
        return content
    except Exception as e:
        logger.error(f"Error loading memory: {e}")
        return "(Error loading memory file)"




def _save_conversation_state(messages: list, turn: int) -> None:
    """Save conversation state for continuation."""
    try:
        # Convert messages to serializable format
        serializable = []
        for msg in messages:
            if hasattr(msg, 'model_dump'):
                serializable.append(msg.model_dump())
            elif isinstance(msg, dict):
                serializable.append(msg)
            else:
                serializable.append({"role": "unknown", "content": str(msg)})
        
        state = {"messages": serializable, "turn": turn}
        with open(CONVERSATION_STATE_FILE, 'w') as f:
            json.dump(state, f)
        logger.info(f"Saved conversation state ({len(messages)} messages, turn {turn})")
    except Exception as e:
        logger.error(f"Error saving conversation state: {e}")


def _load_conversation_state() -> tuple[list, int] | None:
    """Load saved conversation state for continuation."""
    try:
        if not os.path.exists(CONVERSATION_STATE_FILE):
            return None
        
        with open(CONVERSATION_STATE_FILE, 'r') as f:
            state = json.load(f)
        
        logger.info(f"Loaded conversation state ({len(state['messages'])} messages, turn {state['turn']})")
        return state['messages'], state['turn']
    except Exception as e:
        logger.error(f"Error loading conversation state: {e}")
        return None


def _clear_conversation_state() -> None:
    """Clear saved conversation state."""
    try:
        if os.path.exists(CONVERSATION_STATE_FILE):
            os.remove(CONVERSATION_STATE_FILE)
            logger.info("Cleared conversation state")
    except Exception as e:
        logger.error(f"Error clearing conversation state: {e}")




class Agent:
    def __init__(self):
        self.provider = os.getenv("LLM_API_PROVIDER", "openrouter").strip().lower()
        default_model = "x-ai/grok-4.1-fast" if self.provider == "openrouter" else "MiniMax-M2.5"
        self.model = os.getenv("LLM_MODEL", default_model)

        if self.provider == "minimax":
            self.base_url = os.getenv("LLM_BASE_URL", "https://api.minimax.io/v1")
            self.api_key = os.getenv("LLM_API_KEY") or os.getenv("MINIMAX_API_KEY") or os.getenv("OPENAI_API_KEY")
            minimax_model_aliases = {
                "minimax/minimax-m2.5": "MiniMax-M2.5",
                "minimax/minimax-m2.5-highspeed": "MiniMax-M2.5-highspeed",
                "minimax/minimax-m2.1": "MiniMax-M2.1",
                "minimax/minimax-m2.1-highspeed": "MiniMax-M2.1-highspeed",
            }
            self.model = minimax_model_aliases.get(self.model, self.model)
        else:
            self.base_url = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
            self.api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY")

        if not self.api_key:
            logger.warning(f"API key not set for provider '{self.provider}'")

        try:
            self.request_timeout = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120"))
        except ValueError:
            self.request_timeout = 120.0

        headers = {}
        if self.provider == "openrouter":
            provider_order = os.getenv("OPENROUTER_PROVIDER")
            if provider_order:
                if self.model.startswith("minimax/"):
                    logger.info("Skipping OpenRouter provider pin for MiniMax model for compatibility")
                else:
                    headers["X-OpenRouter-Provider-Order"] = provider_order
                    logger.info(f"Using OpenRouter provider order: {provider_order}")

        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=headers if headers else None
        )
        
        if self.provider == "openrouter":
            default_reasoning = "true" if self.model.startswith("minimax/") else "false"
            reasoning_enabled = os.getenv("REASONING_ENABLED", default_reasoning).lower() == "true"
            self.extra_body = {"reasoning": {"enabled": reasoning_enabled}}
            logger.info(f"OpenRouter reasoning tokens {'enabled' if reasoning_enabled else 'disabled'}")
        elif self.provider == "minimax":
            reasoning_split = os.getenv("MINIMAX_REASONING_SPLIT", "true").lower() == "true"
            self.extra_body = {"reasoning_split": True} if reasoning_split else None
            logger.info(f"MiniMax reasoning_split {'enabled' if reasoning_split else 'disabled'}")
        else:
            self.extra_body = None
        
        # Lock to prevent concurrent agent calls
        self._chat_lock = asyncio.Lock()
        
        # Merge Core tools and Plugins
        self.tools_map = CORE_TOOLS.copy()

    async def _create_completion(self, **kwargs):
        kwargs.setdefault("timeout", self.request_timeout)
        try:
            return await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            err_text = str(e).lower()
            # Handle mandatory reasoning for both providers
            if "reasoning is mandatory" in err_text or "reasoning is required" in err_text:
                logger.warning("Provider requires reasoning; retrying with reasoning enabled")
                if self.provider == "minimax":
                    kwargs["extra_body"] = {"reasoning_split": True}
                else:
                    kwargs["extra_body"] = {"reasoning": {"enabled": True}}
                return await self.client.chat.completions.create(**kwargs)
            raise


    # Tools that non-admin users are allowed to use (read-only, public internet stuff)
    PUBLIC_TOOLS = {
        'web_search', 'fetch_url', 'deep_research',
        'github_search', 'github_trending', 'github_repo_info',
        'reddit_top', 'reddit_search', 'reddit_read_thread',
        'twitter_search',
        'hackernews_top',
    }

    def _get_openai_tools(self, is_admin: bool = False):
        """Generates the list of tools for the OpenAI API.
        Non-admin users only get public search/read tools.
        Admin users get everything."""
        all_tools = self._get_all_tool_definitions()
        
        if is_admin:
            return all_tools
        
        # Filter to only public tools
        return [t for t in all_tools if t['function']['name'] in self.PUBLIC_TOOLS]

    def _get_all_tool_definitions(self):
        """Full list of all tool definitions."""
        tools = []
        
        # Add Core Tools
        # We manually define schemas for core tools for better precision
        tools.append({
            "type": "function",
            "function": {
                "name": "run_shell_command",
                "description": "Executes a shell command on the host system. Use this to run CLI tools like curl, git, ping, etc. NOTE: Commands that dump environment variables (env, printenv) are blocked for security. Sensitive values are automatically redacted from output.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The command to execute (e.g. 'ping -c 4 google.com')"}
                    },
                    "required": ["command"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Reads content from a file. NOTE: Access to sensitive files (.env, tokens, credentials) is blocked for security.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path to the file."}
                    },
                    "required": ["path"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Writes content to a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path to the file."},
                        "content": {"type": "string", "description": "Content to write."}
                    },
                    "required": ["path", "content"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "Lists contents of a directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path."}
                    },
                    "required": ["path"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "install_package",
                "description": "Installs a system package using apt-get. The package persists across container restarts. Use for tools like ffmpeg, imagemagick, etc. IMPORTANT: nodejs, npm, python3 are ALREADY installed - do NOT use this for them. Use 'npm install -g <pkg>' or 'pip install <pkg>' instead. After installing, always document the tool in memory using update_memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "package_name": {"type": "string", "description": "Name of the apt package to install (e.g., 'ffmpeg', 'imagemagick'). Do NOT use for nodejs, npm, or python - those are pre-installed."}
                    },
                    "required": ["package_name"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "update_memory",
                "description": "Appends content to a section in your persistent memory (memory.md). Use this to document new tools, user preferences, or notes. Always check read_memory first to avoid duplicates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "enum": ["Known Commands", "User Preferences", "Notes", "Journal"],
                            "description": "The section to append to. Journal entries auto-get a date prefix. Use Journal for short-term observations and soft context."
                        },
                        "content": {
                            "type": "string",
                            "description": "Markdown content to append. For commands, use format: '### cmd_name\\n- Purpose: ...\\n- Usage: ...\\n- Notes: ...'"
                        }
                    },
                    "required": ["section", "content"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "read_memory",
                "description": "Reads your persistent memory file. NOTE: Your memory is ALREADY loaded in the system prompt above (between === YOUR MEMORY === markers). You do NOT need to call this unless you've just updated memory and want to verify the change. Do NOT call this at the start of a conversation - you already have it.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Quick web search for simple lookups. For anything requiring depth (opinions, deals, tricks, recommendations, current events, comparisons), use deep_research instead — it's much more thorough. Use web_search only for simple facts, package lookups, or as a follow-up to deep_research with different/creative query terms.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query (e.g., 'bird twitter cli github', 'python requests library docs')"},
                        "num_results": {"type": "integer", "description": "Number of results to return (1-20, default 10)"}
                    },
                    "required": ["query"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "Read the FULL content of a web page. Use this AFTER search tools to actually read promising pages — snippets are never enough. Essential for getting real details, instructions, guides, and the full story. Use on the top 2-3 URLs from search results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The URL to fetch"},
                        "extract_text": {"type": "boolean", "description": "If true (default), extracts readable text from HTML. If false, returns raw content."}
                    },
                    "required": ["url"]
                }
            }
        })
        
        # Script tools
        tools.append({
            "type": "function",
            "function": {
                "name": "create_script",
                "description": "Create a reusable Python script. Scripts are saved to /app/app/scripts/ and can be run later. Use this to build automation, data processing, API integrations, etc. ALWAYS document new scripts in your memory!",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Script name (no .py extension, e.g., 'fetch_tweets', 'daily_summary')"},
                        "code": {"type": "string", "description": "The Python code for the script"},
                        "description": {"type": "string", "description": "What the script does (becomes a docstring)"}
                    },
                    "required": ["name", "code"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "run_script",
                "description": "Run a Python script from the scripts directory. Use list_scripts to see available scripts first.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Script name (with or without .py)"},
                        "args": {"type": "string", "description": "Command line arguments to pass to the script"}
                    },
                    "required": ["name"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "list_scripts",
                "description": "List all available Python scripts you've created. Shows script names and descriptions.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        })
        
        # Startup command tools
        tools.append({
            "type": "function",
            "function": {
                "name": "add_startup_command",
                "description": "Add a shell command to startup.sh so it runs on every container boot. Use this after installing npm packages or other tools that don't persist. Example: add_startup_command('npm install -g @steipete/bird')",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run on boot (e.g., 'npm install -g @steipete/bird')"}
                    },
                    "required": ["command"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "remove_startup_command",
                "description": "Remove a command from startup.sh so it no longer runs on boot.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The exact command to remove"}
                    },
                    "required": ["command"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "list_startup_commands",
                "description": "List all commands in startup.sh that run on container boot.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        })
        
        # Memory housekeeping
        tools.append({
            "type": "function",
            "function": {
                "name": "housekeep_memory",
                "description": "Clean up your memory file. Expires journal entries older than 7 days and trims whitespace. Run this periodically or when memory feels cluttered.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        })
        
        # --- GitHub Tools ---
        tools.append({
            "type": "function",
            "function": {
                "name": "github_search",
                "description": "Search GitHub repositories. Great for finding projects, libraries, tools, etc. Results include stars, language, description, and URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (e.g. 'llm framework language:python', 'self-hosted dashboard', 'neovim plugin stars:>100')"},
                        "sort": {"type": "string", "enum": ["stars", "forks", "updated", "best-match"], "description": "Sort order (default: stars)"},
                        "limit": {"type": "integer", "description": "Number of results, 1-15 (default: 5)"}
                    },
                    "required": ["query"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "github_trending",
                "description": "Find trending GitHub repositories - newly created repos with the most stars. Great for discovering what's hot in the open source world.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "language": {"type": "string", "description": "Filter by language (e.g. 'python', 'rust', 'typescript'). Empty for all languages."},
                        "since": {"type": "string", "enum": ["daily", "weekly", "monthly"], "description": "Time window (default: daily)"}
                    },
                    "required": []
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "github_repo_info",
                "description": "Get detailed information about a specific GitHub repository - stars, forks, issues, latest release, topics, description, etc.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository in 'owner/repo' format (e.g. 'langchain-ai/langchain', 'astral-sh/ruff')"}
                    },
                    "required": ["repo"]
                }
            }
        })

        # --- Reddit & Hacker News Tools ---
        tools.append({
            "type": "function",
            "function": {
                "name": "reddit_top",
                "description": "Get posts from a subreddit. Great for browsing communities, finding discussions, seeing what's trending on Reddit.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subreddit": {"type": "string", "description": "Subreddit name without 'r/' (e.g. 'programming', 'selfhosted', 'MachineLearning', 'all'). Default: 'all'"},
                        "sort": {"type": "string", "enum": ["hot", "top", "new", "rising"], "description": "Sort order (default: hot)"},
                        "time_filter": {"type": "string", "enum": ["hour", "day", "week", "month", "year", "all"], "description": "Time filter for 'top' sort (default: day)"},
                        "limit": {"type": "integer", "description": "Number of posts, 1-25 (default: 10)"}
                    },
                    "required": []
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "reddit_search",
                "description": "Search Reddit for posts matching a query. Returns titles, scores, comment counts, and links. Use this to find what real people are saying about a topic — opinions, reviews, recommendations, controversies.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (e.g. 'funniest movie ever', 'best budget laptop 2026', 'is X worth it')"},
                        "subreddit": {"type": "string", "description": "Limit to a subreddit (e.g. 'movies', 'politics'). Leave empty to search all of Reddit."},
                        "sort": {"type": "string", "enum": ["relevance", "hot", "top", "new", "comments"], "description": "Sort order (default: relevance)"},
                        "time_filter": {"type": "string", "enum": ["hour", "day", "week", "month", "year", "all"], "description": "Time filter (default: all)"},
                        "limit": {"type": "integer", "description": "Number of results, 1-25 (default: 10)"}
                    },
                    "required": ["query"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "reddit_read_thread",
                "description": "Read a Reddit thread's comments — this is where the REAL gold is. Use this AFTER deep_research or reddit_search on the most interesting threads. Reddit comments contain tricks, workarounds, real opinions, and underground knowledge that Google snippets never show. ALWAYS read at least 2 threads when researching.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Reddit post URL (e.g. https://reddit.com/r/movies/comments/abc123/title/)"},
                        "comment_limit": {"type": "integer", "description": "Max comments to return (default: 15)"}
                    },
                    "required": ["url"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "twitter_search",
                "description": "Search Twitter/X for recent posts. ESSENTIAL for leaks, rumors, breaking news, real-time reactions, insider info, and hot takes. Use this when you need the absolute latest information, or when checking what insiders/influencers are saying about a topic.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (e.g. 'GPT-5 leak', 'funniest movie 2024', 'PS6 announcement')"},
                        "limit": {"type": "integer", "description": "Number of tweets (default: 10, max: 20)"}
                    },
                    "required": ["query"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "hackernews_top",
                "description": "Get stories from Hacker News. Great source for tech news, interesting links, and technical discussions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "story_type": {"type": "string", "enum": ["top", "best", "new", "ask", "show"], "description": "Type of stories (default: top)"},
                        "limit": {"type": "integer", "description": "Number of stories, 1-25 (default: 10)"}
                    },
                    "required": []
                }
            }
        })

        # --- Code Runner ---
        tools.append({
            "type": "function",
            "function": {
                "name": "run_code",
                "description": "Run a code snippet and see the output. Useful for testing ideas, verifying behavior, quick calculations, or demonstrating concepts. Runs in a sandboxed subprocess with a timeout.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "language": {"type": "string", "enum": ["python", "javascript", "shell"], "description": "The programming language to run"},
                        "code": {"type": "string", "description": "The code to execute"},
                        "timeout": {"type": "integer", "description": "Max execution time in seconds, 1-60 (default: 30)"}
                    },
                    "required": ["language", "code"]
                }
            }
        })

        # --- Deep Research ---
        tools.append({
            "type": "function",
            "function": {
                "name": "deep_research",
                "description": "Your PRIMARY research tool. Performs a massive multi-platform parallel operation: (1) runs ALL queries simultaneously on web search, (2) searches Reddit for real community discussions, (3) searches Twitter/X for real-time takes/leaks, (4) fetches and reads top pages in full. Returns a consolidated dossier. USE THIS as your FIRST tool for ANY question that needs real answers — not web_search. After getting results, follow up with reddit_read_thread and fetch_url on the best hits.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of 4-6 DIVERSE search queries covering different angles. Include 'reddit', 'forum', slang terms, specific communities. Example: ['topic overview', 'topic reddit discussion', 'topic trick workaround', 'topic community guide 2026']. BAD: all queries saying the same thing differently."
                        }
                    },
                    "required": ["queries"]
                }
            }
        })

        # --- Gatekeeper (Multi-User) ---
        tools.append({
            "type": "function",
            "function": {
                "name": "manage_access",
                "description": "Control who can talk to you. ADMIN-ONLY TOOL - only execute when the Admin (Alan) explicitly tells you to allow or block someone. NEVER call this on your own initiative. NEVER suggest calling it in a public channel. Use 'channel' type to allow everyone in a Discord channel to talk to you.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["user", "channel"], "description": "Type of entity to manage"},
                        "action": {"type": "string", "enum": ["allow", "block", "list"], "description": "Action to perform"},
                        "id": {"type": "integer", "description": "The Discord ID (snowflake)"},
                        # id is optional for list action, required for allow/block
                        "name": {"type": "string", "description": "Name/Alias for reference (e.g. 'Steve')"}
                    },
                    "required": ["type", "action"]
                }
            }
        })

        return tools

    def _build_system_prompt(self):
        """Constructs the dynamic system prompt with memory injection."""

        
        # Load persistent memory
        memory_content = _load_memory()
        
        # Load System & Personality prompts
        try:
            with open("/app/app/core/prompts/system.txt", "r") as f:
                system_prompt = f.read()
        except Exception:
            system_prompt = "You are Katta. Use tools. Verify everything."

        try:
            with open("/app/app/core/prompts/personality.txt", "r") as f:
                personality_prompt = f.read()
        except Exception:
            personality_prompt = "Vibe: Chill, competent."

        # Get current time info
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            tz_name = os.getenv("TIMEZONE", "UTC")
            tz = ZoneInfo(tz_name)
        except Exception:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        time_context = f"Current time: {now.strftime('%A, %B %d %Y, %I:%M %p')} ({os.getenv('TIMEZONE', 'UTC')})"
        
        # Combine
        full_prompt = f"""{system_prompt}

=== YOUR MEMORY (FACTS ONLY) ===
{memory_content}
=== END MEMORY ===

=== PERSONALITY (VIBE) ===
{personality_prompt}
=== END PERSONALITY ===

=== CONTEXT ===
{time_context}
=== END CONTEXT ===
"""
        return full_prompt


    async def chat(self, user_message: str, message_history: list = None, image_urls: list = None, is_admin: bool = False) -> str:
        """
        Main entrypoint for the Agent.
        Uses a ReAct loop to process tools.

        Args:
            user_message: The user's text message
            message_history: Optional conversation history
            image_urls: Optional list of image URLs to include (for vision models)
            is_admin: If True, user gets full tool access. Otherwise restricted to search/read-only tools.

        Special commands:
        - "continue" or "keep going" - Resume from saved conversation state
        """
        async with self._chat_lock:
            return await self._chat_inner(user_message, message_history, image_urls, is_admin)
    
    async def _chat_inner(self, user_message: str, message_history: list = None, image_urls: list = None, is_admin: bool = False) -> str:
        """Inner chat method, called under lock."""
        # Context Hygiene: Cap history per channel.
        max_history = 10
        if message_history and len(message_history) > max_history:
             logger.info(f"Truncating history from {len(message_history)} to last {max_history} messages")
             message_history = message_history[-max_history:]

        # Session logging
        slog = SessionLogger(session_type="chat", trigger=user_message[:200])

        max_turns = 25

        # Check for continue command
        continue_triggers = ["continue", "keep going", "go on", "carry on", "proceed"]
        is_continue = user_message.strip().lower() in continue_triggers

        if is_continue:
            saved_state = _load_conversation_state()
            if saved_state:
                messages, saved_turn = saved_state
                logger.info(f"Continuing from saved state (was at turn {saved_turn}, resetting to 0 for another {max_turns} turns)")
                messages.append({"role": "user", "content": "(User said to continue. Pick up where you left off.)"})
                turn = 0
            else:
                logger.info("No saved state to continue from")
                messages = [
                    {"role": "system", "content": self._build_system_prompt()}
                ]
                messages.append({"role": "user", "content": user_message})
                turn = 0
        else:
            _clear_conversation_state()

            messages = [
                {"role": "system", "content": self._build_system_prompt()}
            ]

            # Append history if provided
            if message_history:
                for msg in message_history:
                    messages.append({"role": msg["role"], "content": msg["content"]})

            # Build user message content (text + optional images)
            if image_urls:
                user_content = [{"type": "text", "text": user_message}]
                for img_url in image_urls:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": img_url}
                    })
                messages.append({"role": "user", "content": user_content})
                logger.info(f"Added {len(image_urls)} images to message")
            else:
                messages.append({"role": "user", "content": user_message})
            turn = 0

        effective_admin = is_admin
        available_tools = self._get_openai_tools(is_admin=effective_admin)
        research_required = _requires_research(user_message)
        deep_research_topic = any(hint in (user_message or "").lower() for hint in _DEEP_RESEARCH_HINTS)

        # Track URLs returned by tools so we can detect fabricated links in the response
        tool_result_urls = set()
        tool_calls_made = 0
        tools_used = set()
        forced_research_pass = False
        deep_research_nudged = False
        read_sources_nudged = False
        iterative_deepen_nudged = False

        while turn < max_turns:
            turn += 1
            logger.info(f"Agent Turn {turn} (admin={effective_admin})")
            slog.log_turn_start(turn)
            
            try:
                response = await self._create_completion(
                    model=self.model,
                    messages=messages,
                    tools=available_tools,
                    tool_choice="auto",
                    **({"extra_body": self.extra_body} if self.extra_body else {})
                )
                
                response_message = response.choices[0].message
                
                # Log what the model said/did
                slog.log_model_response(response_message.content, response_message.tool_calls)
                
                # Add assistant response to history
                messages.append(response_message)

                # Check for tool_calls
                if response_message.tool_calls:
                    tool_calls = response_message.tool_calls
                    logger.info(f"Tool calls detected: {len(tool_calls)}")
                    tool_calls_made += len(tool_calls)
                    
                    # Cap tool calls to prevent runaway behavior
                    if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
                        logger.warning(f"Too many tool calls ({len(tool_calls)}), capping at {MAX_TOOL_CALLS_PER_TURN}")
                        tool_calls = tool_calls[:MAX_TOOL_CALLS_PER_TURN]
                    
                    for tool_call in tool_calls:
                        function_name = tool_call.function.name
                        tools_used.add(function_name)
                        try:
                            raw_args = tool_call.function.arguments
                            # Some models send empty string for no-arg tools
                            if not raw_args or not raw_args.strip():
                                function_args = {}
                            else:
                                function_args = json.loads(raw_args)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse args for {function_name}, falling back to empty: {e}")
                            function_args = {}
                        
                        logger.info(f"Executing {function_name} with {function_args}")
                        
                        # --- STRICT ACCESS CONTROL ---
                        # Even if the LLM somehow gets a tool name it shouldn't,
                        # block non-admin users from running restricted tools.
                        if not effective_admin and function_name not in self.PUBLIC_TOOLS:
                            logger.warning(f"BLOCKED: non-admin tried to use restricted tool '{function_name}'")
                            result = f"Error: You don't have permission to use '{function_name}'. Only web search tools are available."
                            messages.append({
                                "tool_call_id": tool_call.id,
                                "role": "tool",
                                "name": function_name,
                                "content": result
                            })
                            continue
                        
                        # Execute Tool
                        try:
                            if function_name in self.tools_map:
                                result = self.tools_map[function_name](**function_args)
                            else:
                                result = f"Error: Tool {function_name} not found."
                        except Exception as e:
                            logger.error(f"Tool execution error: {e}")
                            result = f"Error executing {function_name}: {str(e)}"
                            
                        # Log and append result
                        result_str = str(result)
                        slog.log_tool_result(function_name, result_str)
                        tool_result_urls.update(_extract_urls(result_str))
                        messages.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": function_name,
                            "content": result_str
                        })
                    
                    # After first tool pass, remind model to ground its response and dig deeper
                    if tool_calls_made <= MAX_TOOL_CALLS_PER_TURN and research_required:
                        messages.append({
                            "role": "user",
                            "content": (
                                "(System: IMPORTANT — base your response ONLY on the tool results above. "
                                "Do NOT add claims, model names, features, or details from your training data. "
                                "If the tools didn't mention it, don't mention it. "
                                "If results are thin or surface-level, use MORE tools: try fetch_url to read full pages, "
                                "reddit_read_thread on Reddit links, or web_search with different/creative query terms. "
                                "The user wants DEPTH, not summaries of headlines.)"
                            )
                        })
                    
                    # Loop continues to next iteration to let LLM see result and respond
                    continue
                else:
                    content = response_message.content

                    # --- RESEARCH ENFORCEMENT PIPELINE ---
                    # Stage 1: If research is needed but no tools were used at all,
                    # force the agent to use deep_research (not just web_search).
                    if research_required and tool_calls_made == 0 and not forced_research_pass:
                        forced_research_pass = True
                        logger.info("Research-required prompt answered without tools; forcing deep_research")
                        messages.append({
                            "role": "user",
                            "content": (
                                "(System: this question requires research. Use deep_research with 3-5 diverse queries "
                                "covering different angles of the topic. Include queries that target Reddit discussions, "
                                "forum posts, and niche communities — not just mainstream results. "
                                "Then answer using only evidence from tool outputs. If evidence is thin, say so.)"
                            )
                        })
                        continue

                    # Stage 2: If the topic needs depth but only web_search was used, escalate to deep_research.
                    if (research_required and not deep_research_nudged
                        and 'deep_research' not in tools_used
                        and tool_calls_made > 0
                        and deep_research_topic):
                        deep_research_nudged = True
                        logger.info("Deep-research topic answered with shallow tools; nudging for deep_research")
                        messages.append({
                            "role": "user",
                            "content": (
                                "(System: web_search alone is NOT enough for this topic. Use deep_research with 3-5 queries "
                                "that cover different angles — include terms like 'reddit', 'forum', 'trick', 'workaround', "
                                "'community', 'guide' in your queries to find underground/niche results. "
                                "Then synthesize only what the tools found.)"
                            )
                        })
                        continue

                    # Stage 3: After deep_research, if there are interesting Reddit threads,
                    # nudge the agent to actually READ them with reddit_read_thread.
                    if (research_required and not read_sources_nudged
                        and 'deep_research' in tools_used
                        and 'reddit_read_thread' not in tools_used
                        and 'fetch_url' not in tools_used
                        and tool_calls_made > 0):
                        read_sources_nudged = True
                        logger.info("Deep research done but no sources were read; nudging to read top results")
                        messages.append({
                            "role": "user",
                            "content": (
                                "(System: you did deep_research but didn't read any sources in detail. "
                                "The snippets are not enough — use fetch_url on the 2-3 most promising web pages, "
                                "and use reddit_read_thread on any interesting Reddit threads from the results. "
                                "The real answers are in the full content, not snippets. Then give your final answer.)"
                            )
                        })
                        continue

                    # Stage 4: Iterative deepening — if we've done research + read sources but
                    # the answer is suspiciously short, nudge for one more round.
                    if (research_required and not iterative_deepen_nudged
                        and deep_research_topic
                        and 'deep_research' in tools_used
                        and ('reddit_read_thread' in tools_used or 'fetch_url' in tools_used)
                        and content and len(content.strip()) < 400):
                        iterative_deepen_nudged = True
                        logger.info("Answer is thin after research; nudging for iterative deepening")
                        messages.append({
                            "role": "user",
                            "content": (
                                "(System: your answer is very short given the depth of research available. "
                                "Did you find any specific leads, methods, communities, or terms mentioned in the sources "
                                "that you could search for more specifically? Try web_search or reddit_search with those "
                                "specific terms to dig deeper. If you truly found nothing more, explain what you tried.)"
                            )
                        })
                        continue

                    # No tool calls, final response - clear saved state since we completed
                    _clear_conversation_state()

                    if content and content.strip():
                        # Check for fabricated URLs (safety net)
                        if tool_result_urls:
                            content = _check_ungrounded_urls(content, tool_result_urls)
                        slog.close(final_response=content)
                        return content
                    
                    # LLM returned empty content — force a text response
                    logger.warning("LLM returned empty content, forcing response")
                    slog.log_event("Empty response, forcing text-only response")
                    messages.append({"role": "user", "content": "(System: you returned an empty response. Please respond to the user's question with what you know so far.)"})
                    try:
                        forced_response = await self._create_completion(
                            model=self.model,
                            messages=messages,
                            tools=None,  # No tools - force text
                            **({"extra_body": self.extra_body} if self.extra_body else {})
                        )
                        result_text = forced_response.choices[0].message.content or "(No response generated)"
                        slog.close(final_response=result_text)
                        return result_text
                    except Exception as e:
                        logger.error(f"Error forcing response: {e}")
                        slog.close(final_response=f"Error: {e}")
                        return "(Task completed but no response generated)"

            except Exception as e:
                logger.error(f"Error in chat loop: {e}")
                slog.log_event(f"Error: {e}")
                slog.close(final_response=f"Error: {e}")
                return f"An error occurred: {str(e)}"
        
        # If we hit the turn limit, save state for continuation and force a response
        logger.warning(f"Hit turn limit ({max_turns}), saving state for continuation")
        slog.log_event(f"Hit turn limit ({max_turns})")
        _save_conversation_state(messages, turn)
        
        try:
            response = await self._create_completion(
                model=self.model,
                messages=messages + [{"role": "user", "content": "(System: you're running out of turns. Respond with what you have so far. If your research isn't complete, mention the user can say 'continue'.)"}],
                tools=None,  # No tools - force text response
                **({"extra_body": self.extra_body} if self.extra_body else {})
            )
            result_text = response.choices[0].message.content or "(Turn limit reached - say 'continue' to resume)"
            slog.close(final_response=result_text)
            return result_text
        except Exception as e:
            logger.error(f"Error in final response: {e}")
            slog.close(final_response=f"Error: {e}")
            return "Hit turn limit. Say 'continue' to resume."
