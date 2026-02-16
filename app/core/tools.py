import json
import logging
import os
import re
import shlex
import subprocess
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MAX_OUTPUT_LENGTH = 4000
PACKAGES_FILE = "/app/app/data/packages.txt"
MEMORY_FILE = "/app/app/data/memory.md"
SCRIPTS_DIR = "/app/app/scripts"
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080")
SOURCES_PATH = "/app/app/data/sources.json"
PERMISSIONS_PATH = "/app/app/data/permissions.json"

# Domains that are NEVER worth fetching — dictionaries, travel booking, spam, etc.
# These pollute deep_research results when SearXNG returns them for common words.
_JUNK_DOMAINS = frozenset({
    # Dictionaries / reference (useless for research)
    "dictionary.com", "www.dictionary.com",
    "merriam-webster.com", "www.merriam-webster.com",
    "oxfordlearnersdictionaries.com", "www.oxfordlearnersdictionaries.com",
    "cambridge.org", "dictionary.cambridge.org",
    "wiktionary.org", "en.wiktionary.org",
    "thesaurus.com", "www.thesaurus.com",
    # Travel / booking (common false positives for "cheap", "deal", etc.)
    "cheapflights.com", "www.cheapflights.com", "www.cheapflights.co.uk",
    "kayak.com", "www.kayak.com",
    "booking.com", "www.booking.com",
    "going.com", "www.going.com",
    "skyscanner.com", "www.skyscanner.com",
    "expedia.com", "www.expedia.com",
    "tripadvisor.com", "www.tripadvisor.com",
    # Generic spam / irrelevant
    "pinterest.com", "www.pinterest.com",
    "facebook.com", "www.facebook.com",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com",
    "linkedin.com", "www.linkedin.com",
    # Translation services
    "translate.google.com", "translate.google.it",
    "deepl.com", "www.deepl.com",
    # Chinese sites that always 403
    "zhihu.com", "www.zhihu.com",
    "baidu.com", "www.baidu.com",
})

def _is_junk_url(url: str) -> bool:
    """Check if a URL is from a known junk/irrelevant domain."""
    try:
        domain = urlparse(url).netloc.lower()
        return domain in _JUNK_DOMAINS
    except Exception:
        return False

# Security: sensitive file patterns that must never be read or exposed
_SENSITIVE_PATTERNS = [
    '.env', '.env.', 'env.example',
    'token', 'secret', 'credential', 'password',
    '/etc/shadow', '/etc/passwd',
    'id_rsa', 'id_ed25519', '.ssh/',
    '.git/config',
]

# Security: env vars that must be redacted from any output
_SENSITIVE_ENV_VARS = {
    'DISCORD_BOT_TOKEN', 'OPENROUTER_API_KEY', 'GITHUB_TOKEN',
    'API_KEY', 'SECRET', 'PASSWORD', 'TOKEN',
}

# Security: shell commands that are blocked
_BLOCKED_COMMANDS = [
    'env', 'printenv', 'set ',
    'cat .env', 'cat /app/.env',
    'echo $DISCORD', 'echo $OPENROUTER', 'echo $GITHUB_TOKEN',
]

# Optional shell safety mode
# - trusted (default): current behavior, broad command support
# - safe: blocks dangerous shell patterns
_DANGEROUS_COMMAND_PATTERNS = [
    r"(^|\s)sudo(\s|$)",
    r"(^|\s)rm\s+-rf\s+/",
    r":\(\)\s*\{\s*:\|\:&\s*\};:",
    r"(^|\s)dd\s+if=",
    r"(^|\s)mkfs(\.| )",
    r"curl\s+[^|]*\|\s*(sh|bash)",
    r"wget\s+[^|]*\|\s*(sh|bash)",
]


def _is_sensitive_path(path: str) -> bool:
    """Check if a file path is sensitive and should be blocked."""
    path_lower = path.lower().strip()
    basename = os.path.basename(path_lower)
    for pattern in _SENSITIVE_PATTERNS:
        if pattern in path_lower or pattern in basename:
            return True
    return False


def _sanitize_output(output: str) -> str:
    """Redact sensitive env var values from tool output."""
    for var_name in _SENSITIVE_ENV_VARS:
        value = os.getenv(var_name, '')
        if value and len(value) > 4 and value in output:
            output = output.replace(value, '[REDACTED]')
    return output


def _command_mode() -> str:
    mode = os.getenv("KATTA_COMMAND_MODE", "trusted").strip().lower()
    return mode if mode in {"trusted", "safe"} else "trusted"


def _is_dangerous_command(command: str) -> bool:
    text = command.strip().lower()
    return any(re.search(pattern, text) for pattern in _DANGEROUS_COMMAND_PATTERNS)

# Valid sections the agent can update
VALID_MEMORY_SECTIONS = ["Known Commands", "User Preferences", "Notes", "Journal"]

def manage_access(type: str, action: str, id: int | None = None, name: str = "") -> str:
    """
    Args:
        type: 'user' or 'channel'
        action: 'allow', 'block', 'list'
        id: Discord ID (required for allow/block; omitted for list)
        name: Optional name for reference
    """
    if type not in ['user', 'channel']:
        return "Error: type must be 'user' or 'channel'"
    if action not in ['allow', 'block', 'list']:
        return "Error: action must be 'allow', 'block', or 'list'"
    if action in {'allow', 'block'} and id is None:
        return "Error: id is required for allow/block actions."

    try:
        if id is not None:
            id = int(id)
        logger.info(f"manage_access: {action} {type} {id} ({name})")
        
        # Load or Init
        if not os.path.exists(PERMISSIONS_PATH):
            data = {"allowed_users": [], "allowed_channels": []}
        else:
            with open(PERMISSIONS_PATH, 'r') as f:
                data = json.load(f)
                
        key = "allowed_users" if type == "user" else "allowed_channels"
        data.setdefault(key, [])
        
        # LIST
        if action == "list":
            items = data.get(key, [])
            if not items:
                return f"No allowed {type}s."
            return f"Allowed {type}s:\n" + "\n".join([f"- {i['name']} ({i['id']})" for i in items])
            
        # ALLOW
        if action == "allow":
            # Check duplicates
            for item in data[key]:
                if item['id'] == id:
                    return f"{type.title()} {id} is already allowed."
            
            data[key].append({"id": id, "name": name or "Unknown"})
            
            with open(PERMISSIONS_PATH, 'w') as f:
                json.dump(data, f, indent=2)
            return f"Successfully allowed {type}: {name} ({id})"
            
        # BLOCK (Remove)
        if action == "block":
            initial_len = len(data[key])
            data[key] = [i for i in data[key] if i['id'] != id]
            
            if len(data[key]) == initial_len:
                return f"{type.title()} {id} was not in the allow list."
                
            with open(PERMISSIONS_PATH, 'w') as f:
                json.dump(data, f, indent=2)
            return f"Successfully blocked/removed {type}: {id}"

    except Exception as e:
        return f"Error managing access: {e}"

def add_source(category: str, value: str, name: str = None) -> str:
    """
    Adds a new source to the Scout's configuration.
    
    Args:
        category: One of 'rss_feeds', 'subreddits', 'github_dorks', 'search_queries'.
        value: The URL, subreddit name, dork query, or search term.
        name: Optional name for RSS feeds.
    """
    valid_categories = ['rss_feeds', 'subreddits', 'github_dorks', 'search_queries']
    if category not in valid_categories:
        return f"Error: Invalid category. Must be one of: {valid_categories}"
    
    try:
        if not os.path.exists(SOURCES_PATH):
            data = {c: [] for c in valid_categories}
        else:
            with open(SOURCES_PATH, 'r') as f:
                data = json.load(f)
        
        # Initialize if missing
        if category not in data:
            data[category] = []
            
        # Add item
        if category == 'rss_feeds':
            if not name:
                return "Error: RSS feeds require a 'name' argument."
            # Check duplicates
            for item in data['rss_feeds']:
                if item['url'] == value:
                    return f"Source already exists: {value}"
            data['rss_feeds'].append({"url": value, "name": name})
        else:
            # Check duplicates (list of strings)
            if value in data[category]:
                return f"Source already exists: {value}"
            data[category].append(value)
            
        with open(SOURCES_PATH, 'w') as f:
            json.dump(data, f, indent=2)
            
        return f"Successfully added to {category}: {value}"
    except Exception as e:
        return f"Error adding source: {e}"

def remove_source(category: str, value: str) -> str:
    """
    Removes a source from the Scout's configuration.
    """
    try:
        if not os.path.exists(SOURCES_PATH):
            return "Error: sources.json not found."
            
        with open(SOURCES_PATH, 'r') as f:
            data = json.load(f)
            
        if category not in data:
            return f"Error: Category {category} not found."
            
        initial_len = len(data[category])
        
        if category == 'rss_feeds':
            data['rss_feeds'] = [x for x in data['rss_feeds'] if x['url'] != value]
        else:
            data[category] = [x for x in data[category] if x != value]
            
        if len(data[category]) == initial_len:
            return f"Source not found in {category}: {value}"
            
        with open(SOURCES_PATH, 'w') as f:
            json.dump(data, f, indent=2)
            
        return f"Successfully removed from {category}: {value}"
    except Exception as e:
        return f"Error removing source: {e}"

def _truncate_output(output: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """Truncates output if it exceeds max_length."""
    if len(output) > max_length:
        half = max_length // 2 - 50
        return output[:half] + f"\n\n... [TRUNCATED {len(output) - max_length} chars] ...\n\n" + output[-half:]
    return output

def run_shell_command(command: str) -> str:
    """
    Executes a shell command and returns the output (stdout + stderr).
    Output is truncated if it exceeds 4000 characters.
    Sensitive env variables are automatically redacted from output.
    """
    logger.info(f"Executing shell command: {command}")
    
    # Security: block commands that dump env vars or read sensitive files
    cmd_lower = command.strip().lower()
    for blocked in _BLOCKED_COMMANDS:
        if cmd_lower == blocked or cmd_lower.startswith(blocked):
            return "Error: This command is blocked for security reasons. Environment variables and credentials cannot be exposed."

    if _command_mode() == "safe" and _is_dangerous_command(command):
        return "Error: Command blocked by safe mode. Set KATTA_COMMAND_MODE=trusted to allow it."
    
    # Block cat/head/tail/less on sensitive files
    if any(reader in cmd_lower for reader in ['cat ', 'head ', 'tail ', 'less ', 'more ']):
        # Extract the file path being read
        parts = command.strip().split()
        for part in parts[1:]:
            if _is_sensitive_path(part):
                return f"Error: Cannot read sensitive file: {part}"
    
    try:
        result = subprocess.run(
            command, 
            shell=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            timeout=60
        )
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        output = _sanitize_output(output)  # Redact any leaked secrets
        return _truncate_output(output)
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 60 seconds."
    except Exception as e:
        return f"Error executing command: {str(e)}"

def read_file(path: str) -> str:
    """Reads the content of a file. Blocks access to sensitive files."""
    try:
        if _is_sensitive_path(path):
            return f"Error: Access denied. Cannot read sensitive file: {os.path.basename(path)}"
        if not os.path.exists(path):
            return f"Error: File not found: {path}"
        with open(path, 'r') as f:
            content = f.read()
        return _sanitize_output(content)
    except Exception as e:
        return f"Error reading file: {str(e)}"

def write_file(path: str, content: str) -> str:
    """Writes content to a file. Overwrites if exists."""
    try:
        # Ensure dir exists if within app/data or plugins
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            # Basic security check - only allow creation in known user dirs if we wanted to be strict
            # But requirement is utilitarian tool, so we allow it.
            os.makedirs(directory, exist_ok=True)
            
        with open(path, 'w') as f:
            f.write(content)
        return f"Successfully wrote to {path}"
    except Exception as e:
        return f"Error writing file: {str(e)}"

def list_directory(path: str) -> str:
    """Lists files and directories in the given path."""
    try:
        if not os.path.exists(path):
            return f"Error: Path not found: {path}"
        
        items = os.listdir(path)
        output = []
        for item in items:
            item_path = os.path.join(path, item)
            kind = "DIR" if os.path.isdir(item_path) else "FILE"
            output.append(f"[{kind}] {item}")
        return "\n".join(output) if output else "(Empty directory)"
    except Exception as e:
        return f"Error listing directory: {str(e)}"

STARTUP_SCRIPT = "/app/app/data/startup.sh"

def add_startup_command(command: str) -> str:
    """Adds a command to startup.sh so it runs on every container boot."""
    logger.info(f"Adding startup command: {command}")
    
    # Read existing commands to avoid duplicates
    existing = set()
    if os.path.exists(STARTUP_SCRIPT):
        with open(STARTUP_SCRIPT, 'r') as f:
            existing = {line.strip() for line in f if line.strip() and not line.strip().startswith('#')}
    
    if command.strip() in existing:
        return f"Command already in startup.sh: {command}"
    
    with open(STARTUP_SCRIPT, 'a') as f:
        f.write(f"{command.strip()}\n")
    
    return f"Added to startup.sh: {command}. It will run on every container boot."


def remove_startup_command(command: str) -> str:
    """Removes a command from startup.sh."""
    logger.info(f"Removing startup command: {command}")
    
    if not os.path.exists(STARTUP_SCRIPT):
        return "No startup.sh found."
    
    with open(STARTUP_SCRIPT, 'r') as f:
        lines = f.readlines()
    
    new_lines = [line for line in lines if line.strip() != command.strip()]
    
    if len(new_lines) == len(lines):
        return f"Command not found in startup.sh: {command}"
    
    with open(STARTUP_SCRIPT, 'w') as f:
        f.writelines(new_lines)
    
    return f"Removed from startup.sh: {command}"


def list_startup_commands() -> str:
    """Lists all commands in startup.sh."""
    if not os.path.exists(STARTUP_SCRIPT):
        return "No startup.sh found."
    
    with open(STARTUP_SCRIPT, 'r') as f:
        lines = f.readlines()
    
    commands = [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
    
    if not commands:
        return "startup.sh is empty."
    
    return "Startup commands:\n" + "\n".join(f"  {i+1}. {cmd}" for i, cmd in enumerate(commands))


# Packages that are pre-installed via Dockerfile and must not be overwritten by apt
_BLOCKED_PACKAGES = {
    'nodejs', 'npm', 'node', 'python3', 'python', 'python3-pip', 'pip',
    'python3-dev', 'python-dev',
}

def install_package(package_name: str) -> str:
    """
    Installs a system package using apt-get and persists it to packages.txt.
    The package will be automatically reinstalled on container restart.
    Use this to install tools like ffmpeg, imagemagick, etc.
    NOTE: nodejs, npm, python3 are pre-installed. Use npm/pip to install packages for those.
    """
    logger.info(f"Installing package: {package_name}")
    
    # Sanitize package name (basic check)
    if not package_name or any(c in package_name for c in [';', '&', '|', '`', '$', '(', ')']):
        return f"Error: Invalid package name: {package_name}"
    
    # Block packages that would break pre-installed runtimes
    if package_name.lower() in _BLOCKED_PACKAGES:
        return (f"Error: '{package_name}' is pre-installed and managed by the Dockerfile. "
                f"Do NOT install it via apt. Use 'npm install -g <pkg>' for Node.js packages "
                f"or 'pip install <pkg>' for Python packages.")
    
    try:
        # Update apt cache
        subprocess.run(
            ["apt-get", "update"],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        # Install the package
        install_result = subprocess.run(
            ["apt-get", "install", "-y", package_name],
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if install_result.returncode != 0:
            return f"Error installing {package_name}:\n{_truncate_output(install_result.stderr)}"
        
        # Persist to packages.txt
        # Read existing packages to avoid duplicates
        existing_packages = set()
        if os.path.exists(PACKAGES_FILE):
            with open(PACKAGES_FILE, 'r') as f:
                existing_packages = {line.strip() for line in f if line.strip()}
        
        if package_name not in existing_packages:
            with open(PACKAGES_FILE, 'a') as f:
                f.write(f"{package_name}\n")
            logger.info(f"Added {package_name} to {PACKAGES_FILE}")
        
        return f"Successfully installed {package_name}. It will persist across restarts.\n{_truncate_output(install_result.stdout)}"
        
    except subprocess.TimeoutExpired:
        return f"Error: Installation of {package_name} timed out."
    except Exception as e:
        return f"Error installing {package_name}: {str(e)}"

def update_memory(section: str, content: str) -> str:
    """
    Adds or updates content in a specific section of memory.md.
    If the content starts with a ### heading that already exists in the section,
    the existing entry will be REPLACED instead of duplicated.
    Journal entries are automatically prefixed with today's date if not already dated.
    
    Args:
        section: Must be one of "Known Commands", "User Preferences", "Notes", "Journal"
        content: Markdown content to add
    """
    if section not in VALID_MEMORY_SECTIONS:
        return f"Error: Invalid section '{section}'. Must be one of: {VALID_MEMORY_SECTIONS}"
    
    if not content or not content.strip():
        return "Error: Content cannot be empty"
    
    # Auto-prefix journal entries with date if not already dated
    if section == "Journal":
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y-%m-%d")
        stripped = content.strip()
        if not stripped.startswith(f"[{today}") and not re.match(r'^\[\d{4}-\d{2}-\d{2}\]', stripped):
            content = f"[{today}] {stripped}"
    
    try:
        # Read current memory
        if not os.path.exists(MEMORY_FILE):
            return f"Error: Memory file not found: {MEMORY_FILE}"
        
        with open(MEMORY_FILE, 'r') as f:
            memory = f.read()
        
        # Find the section header (## Section Name)
        section_pattern = rf"(## {re.escape(section)})"
        match = re.search(section_pattern, memory)
        
        if not match:
            return f"Error: Section '## {section}' not found in memory file"
        
        section_start = match.end()
        
        # Find the next section or end of file
        next_section = re.search(r"\n## ", memory[section_start:])
        if next_section:
            section_end = section_start + next_section.start()
        else:
            section_end = len(memory)
        
        section_content = memory[section_start:section_end]
        
        # Check if the new content has a ### heading that already exists
        heading_match = re.match(r'###\s+(.+)', content.strip())
        if heading_match:
            heading_name = heading_match.group(1).strip()
            # Find and remove existing entry with the same ### heading
            existing_pattern = rf'\n*###\s+{re.escape(heading_name)}\b.*?(?=\n###|\n## |\Z)'
            section_content = re.sub(existing_pattern, '', section_content, flags=re.DOTALL)
            logger.info(f"Replacing existing entry '### {heading_name}' in section '{section}'")
        
        # Append the new content
        formatted_content = f"\n\n{content.strip()}\n"
        new_section_content = section_content.rstrip() + formatted_content
        
        # Rebuild the full file
        new_memory = memory[:section_start] + new_section_content + memory[section_end:]
        
        # Write back
        with open(MEMORY_FILE, 'w') as f:
            f.write(new_memory)
        
        logger.info(f"Updated memory section '{section}' with {len(content)} chars")
        return f"Successfully updated '{section}' section in memory."
        
    except Exception as e:
        logger.error(f"Error updating memory: {e}")
        return f"Error updating memory: {str(e)}"


def read_memory() -> str:
    """
    Reads the current contents of memory.md.
    Use this to check what's already documented before adding new entries.
    """
    try:
        if not os.path.exists(MEMORY_FILE):
            return "Memory file does not exist yet."
        with open(MEMORY_FILE, 'r') as f:
            return f.read()
    except Exception as e:
        return f"Error reading memory: {str(e)}"


def web_search(query: str, num_results: int = 10) -> str:
    """
    Searches the web using SearXNG and returns results.
    Use this to find information, look up packages, find documentation, etc.

    Args:
        query: The search query
        num_results: Number of results to return (default 10, max 20)

    Returns:
        Search results with titles, URLs, and snippets
    """
    logger.info(f"Web search: {query}")

    num_results = min(max(1, num_results), 20)

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{SEARXNG_URL}/search",
                params={
                    "q": query,
                    "format": "json",
                    "categories": "general",
                },
                headers={"X-Forwarded-For": "127.0.0.1"},
            )
            response.raise_for_status()
            data = response.json()

        # Filter junk domains and collect up to num_results
        filtered = []
        for r in data.get("results", []):
            if len(filtered) >= num_results:
                break
            url = r.get("url", "")
            if not url or _is_junk_url(url):
                continue
            filtered.append(r)

        if not filtered:
            return f"No results found for: {query}"

        output = []
        for i, r in enumerate(filtered, 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            snippet = r.get("content", "No description")[:500]
            output.append(f"{i}. **{title}**\n   URL: {url}\n   {snippet}")

        return "\n\n".join(output)
        
    except httpx.ConnectError:
        return "Error: Could not connect to search service. SearXNG may not be running."
    except Exception as e:
        logger.error(f"Web search error: {e}")
        return f"Error searching: {str(e)}"


def fetch_url(url: str, extract_text: bool = True) -> str:
    """
    Fetches content from a URL. Use this to read documentation, READMEs, web pages, etc.
    
    Args:
        url: The URL to fetch
        extract_text: If True, extracts readable text from HTML. If False, returns raw content.
    
    Returns:
        The page content (text extracted from HTML, or raw for non-HTML)
    """
    logger.info(f"Fetching URL: {url}")
    
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Katta/1.0; +https://github.com)"
            })
            response.raise_for_status()
        
        content_type = response.headers.get("content-type", "")
        
        # For HTML, extract text
        if extract_text and "text/html" in content_type:
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Remove script and style elements
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()
            
            # Try to find main content
            main = soup.find("main") or soup.find("article") or soup.find("body")
            if main:
                text = main.get_text(separator="\n", strip=True)
            else:
                text = soup.get_text(separator="\n", strip=True)
            
            # Clean up whitespace
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            text = "\n".join(lines)
            
            return _truncate_output(text, 10000)

        # For markdown or plain text, return as-is
        elif "text/" in content_type:
            return _truncate_output(response.text, 10000)
        
        # For other types, just note the content type
        else:
            return f"Fetched {url} (content-type: {content_type}, {len(response.content)} bytes)"
            
    except httpx.HTTPStatusError as e:
        return f"HTTP Error {e.response.status_code}: {url}"
    except httpx.ConnectError:
        return f"Error: Could not connect to {url}"
    except Exception as e:
        logger.error(f"Fetch error: {e}")
        return f"Error fetching {url}: {str(e)}"


async def _async_search(client, query, limit=10, categories="general"):
    """Helper for parallel search across SearXNG."""
    try:
        resp = await client.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json", "categories": categories},
            headers={"X-Forwarded-For": "127.0.0.1"},
            timeout=15.0
        )
        resp.raise_for_status()
        data = resp.json()
        cleaned = []
        for item in data.get("results", []):
            if len(cleaned) >= limit:
                break
            url = item.get("url", "").strip()
            if not url or _is_junk_url(url):
                continue
            cleaned.append({
                "title": item.get("title", "No title"),
                "url": url,
                "snippet": item.get("content", "").strip()[:500],
            })
        return {"query": query, "results": cleaned, "error": None}
    except Exception as e:
        logger.warning(f"Deep research search failed for '{query}': {e}")
        return {"query": query, "results": [], "error": str(e)}

async def _async_fetch(client, url):
    """Helper for parallel fetch."""
    domain = urlparse(url).netloc or "unknown"
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Katta/DeepSearch"},
            timeout=15.0,
            follow_redirects=True
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        title = ""

        if "text/html" in content_type:
            soup = BeautifulSoup(resp.text, "html.parser")
            title = (soup.title.string or "").strip() if soup.title else ""
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()
            main = soup.find("main") or soup.find("article") or soup.find("body")
            text = main.get_text(separator="\n", strip=True) if main else soup.get_text(separator="\n", strip=True)
        else:
            text = resp.text

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned_text = "\n".join(lines)[:12000]

        return {
            "url": url,
            "domain": domain,
            "title": title or url,
            "content": cleaned_text,
            "status": "ok",
            "error": None,
        }
    except Exception as e:
        return {
            "url": url,
            "domain": domain,
            "title": url,
            "content": "",
            "status": "failed",
            "error": str(e),
        }

def deep_research(queries: list[str]) -> str:
    """
    Performs a massive multi-platform parallel research operation.
    1. Executes all queries on web search in parallel.
    2. Searches Reddit for the same topic.
    3. Searches Twitter/X for real-time takes and leaks.
    4. Fetches content from top unique URLs.
    5. Returns a consolidated dossier from ALL platforms.
    
    Args:
        queries: List of search strings (e.g. ["history of linux", "linux kernel architecture", "linus torvalds biography"])
    """
    import asyncio

    if not isinstance(queries, list) or not queries:
        return "Error: provide a non-empty list of search queries."

    normalized_queries = []
    for query in queries:
        if isinstance(query, str):
            q = query.strip()
            if q and q not in normalized_queries:
                normalized_queries.append(q)
    normalized_queries = normalized_queries[:8]

    if not normalized_queries:
        return "Error: no valid queries were provided."

    # Build topic strings for Reddit/Twitter searches — use first two for diversity
    topic_query = normalized_queries[0]
    alt_topic_query = normalized_queries[1] if len(normalized_queries) > 1 else topic_query

    async def run_deep_research():
        async with httpx.AsyncClient() as client:
            # Phase 1: Parallel web searches — hit MULTIPLE categories for diversity
            search_tasks = []
            for query in normalized_queries:
                search_tasks.append(_async_search(client, query, limit=10, categories="general"))
            # Also search "news" and "social media" categories with the primary query
            # to surface results that "general" misses
            search_tasks.append(_async_search(client, topic_query, limit=8, categories="news"))
            if alt_topic_query != topic_query:
                search_tasks.append(_async_search(client, alt_topic_query, limit=8, categories="news"))
            search_batches = await asyncio.gather(*search_tasks)

            # Phase 2: Reddit search (two queries for diversity)
            reddit_results = []
            try:
                reddit_raw = reddit_search(topic_query, sort="relevance", time_filter="year", limit=15)
                reddit_results.append({"query": topic_query, "output": reddit_raw})
            except Exception as e:
                reddit_results.append({"query": topic_query, "output": f"Reddit error: {e}"})
            if alt_topic_query != topic_query:
                try:
                    reddit_raw2 = reddit_search(alt_topic_query, sort="relevance", time_filter="year", limit=10)
                    reddit_results.append({"query": alt_topic_query, "output": reddit_raw2})
                except Exception as e:
                    reddit_results.append({"query": alt_topic_query, "output": f"Reddit error: {e}"})

            # Phase 3: Twitter search (two queries)
            twitter_results = []
            try:
                twitter_raw = twitter_search(topic_query, limit=15)
                twitter_results.append({"query": topic_query, "output": twitter_raw})
            except Exception as e:
                twitter_results.append({"query": topic_query, "output": f"Twitter error: {e}"})
            if alt_topic_query != topic_query:
                try:
                    twitter_raw2 = twitter_search(alt_topic_query, limit=10)
                    twitter_results.append({"query": alt_topic_query, "output": twitter_raw2})
                except Exception as e:
                    twitter_results.append({"query": alt_topic_query, "output": f"Twitter error: {e}"})

            # Phase 4: HackerNews search (great for tech/niche topics)
            hn_results = []
            try:
                hn_raw = hackernews_top(story_type="new", limit=10)
                hn_results.append(hn_raw)
            except Exception:
                pass

            # Phase 5: Collect and fetch top web URLs — with junk filtering
            discovered_urls = set()
            per_domain_count = {}
            selected_urls = []

            for batch in search_batches:
                for item in batch["results"]:
                    url = item["url"]
                    if url in discovered_urls or _is_junk_url(url):
                        continue
                    domain = urlparse(url).netloc or "unknown"
                    if per_domain_count.get(domain, 0) >= 3:
                        continue
                    discovered_urls.add(url)
                    per_domain_count[domain] = per_domain_count.get(domain, 0) + 1
                    selected_urls.append(url)

            selected_urls = selected_urls[:25]
            fetch_tasks = [_async_fetch(client, url) for url in selected_urls]
            fetched = await asyncio.gather(*fetch_tasks) if fetch_tasks else []

            return search_batches, fetched, reddit_results, twitter_results, hn_results

    # Run async coroutine safely even when called from within a running event loop
    # (e.g. Discord's async context). We spin up a new loop in a background thread.
    import concurrent.futures
    def _run_in_thread():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run_deep_research())
        finally:
            loop.close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_in_thread)
            search_batches, fetched_data, reddit_data, twitter_data, hn_data = future.result(timeout=120)
    except Exception as e:
        return f"Deep Research Error: {e}"

    report = ["# DEEP RESEARCH DOSSIER"]
    report.append(f"- Queries run: {len(normalized_queries)}")
    report.append(f"- Candidate sources found: {sum(len(batch['results']) for batch in search_batches)}")
    ok_sources = [item for item in fetched_data if item.get("status") == "ok"]
    failed_sources = [item for item in fetched_data if item.get("status") == "failed"]
    report.append(f"- Sources fetched successfully: {len(ok_sources)}")
    report.append(f"- Sources failed: {len(failed_sources)}")

    report.append("\n## Search Hits by Query")
    for batch in search_batches:
        report.append(f"\n### {batch['query']}")
        if batch["results"]:
            for idx, item in enumerate(batch["results"], 1):
                snippet = item["snippet"] or "(no snippet)"
                report.append(f"{idx}. {item['title']}\n   URL: {item['url']}\n   Snippet: {snippet}")
        elif batch["error"]:
            report.append(f"- Search failed: {batch['error']}")
        else:
            report.append("- No results")

    # Reddit section
    report.append("\n## Reddit Discussions")
    for rd in reddit_data:
        report.append(rd["output"][:6000])

    # Twitter section
    report.append("\n## Twitter/X Real-Time")
    for td in twitter_data:
        report.append(td["output"][:6000])

    # HackerNews section
    if hn_data:
        report.append("\n## Hacker News")
        for hn in hn_data:
            report.append(str(hn)[:3000])

    report.append("\n## Source Extracts")
    if ok_sources:
        for source in ok_sources:
            extract = source["content"][:3000]
            report.append(
                f"\n### {source['title']}\n"
                f"- URL: {source['url']}\n"
                f"- Domain: {source['domain']}\n"
                f"- Extract:\n{extract}\n"
            )
    else:
        report.append("No readable sources were fetched.")

    if failed_sources:
        report.append("\n## Failed Sources")
        for source in failed_sources:
            report.append(f"- {source['url']} ({source['error']})")

    return _truncate_output("\n".join(report), 30000)


def create_script(name: str, code: str, description: str = "") -> str:
    """
    Creates a Python script in the scripts directory.
    
    Args:
        name: Script name (without .py extension)
        code: Python code to save
        description: What the script does (saved as docstring)
    
    Returns:
        Success message or error
    """
    # Sanitize name
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    if not safe_name:
        return "Error: Invalid script name"
    
    script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.py")
    
    # Ensure scripts directory exists
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    
    # Add docstring if description provided
    if description and not code.startswith('"""'):
        code = f'"""{description}"""\n\n{code}'
    
    try:
        with open(script_path, 'w') as f:
            f.write(code)
        logger.info(f"Created script: {script_path}")
        return f"Created script: {safe_name}.py\nPath: {script_path}\nRemember to document this in your memory!"
    except Exception as e:
        return f"Error creating script: {str(e)}"


def run_script(name: str, args: str = "") -> str:
    """
    Runs a Python script from the scripts directory.
    
    Args:
        name: Script name (with or without .py extension)
        args: Command line arguments to pass to the script
    
    Returns:
        Script output (stdout + stderr)
    """
    # Normalize name
    if not name.endswith('.py'):
        name = f"{name}.py"
    
    script_path = os.path.join(SCRIPTS_DIR, name)
    
    if not os.path.exists(script_path):
        # List available scripts
        available = []
        if os.path.exists(SCRIPTS_DIR):
            available = [f for f in os.listdir(SCRIPTS_DIR) if f.endswith('.py')]
        if available:
            return f"Error: Script '{name}' not found.\nAvailable scripts: {', '.join(available)}"
        return f"Error: Script '{name}' not found. No scripts exist yet."
    
    logger.info(f"Running script: {script_path} {args}")
    
    try:
        parsed_args = shlex.split(args) if args.strip() else []
        cmd = ["python3", script_path] + parsed_args
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,  # 2 minute timeout for scripts
            cwd=SCRIPTS_DIR
        )
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        if result.returncode != 0:
            output = f"(Exit code: {result.returncode})\n{output}"
        return _sanitize_output(_truncate_output(output))
    except subprocess.TimeoutExpired:
        return "Error: Script timed out after 120 seconds."
    except Exception as e:
        return f"Error running script: {str(e)}"


def list_scripts() -> str:
    """
    Lists all available scripts in the scripts directory.
    
    Returns:
        List of scripts with their descriptions
    """
    if not os.path.exists(SCRIPTS_DIR):
        return "No scripts directory yet. Create a script first!"
    
    scripts = [f for f in os.listdir(SCRIPTS_DIR) if f.endswith('.py')]
    
    if not scripts:
        return "No scripts created yet."
    
    output = []
    for script in sorted(scripts):
        script_path = os.path.join(SCRIPTS_DIR, script)
        # Try to extract docstring
        try:
            with open(script_path, 'r') as f:
                content = f.read()
            # Simple docstring extraction
            if content.startswith('"""'):
                end = content.find('"""', 3)
                if end != -1:
                    desc = content[3:end].strip().split('\n')[0][:50]
                    output.append(f"- {script}: {desc}")
                    continue
        except Exception:
            pass
        output.append(f"- {script}")
    
    return "Available scripts:\n" + "\n".join(output)


# ---------------------------------------------------------------------------
# GitHub Tools
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"

def _github_headers() -> dict:
    """Build headers for GitHub API requests, with optional token."""
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "Katta-Agent"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def github_search(query: str, sort: str = "stars", limit: int = 5) -> str:
    """
    Search GitHub repositories.
    
    Args:
        query: Search query (e.g. 'llm agent framework language:python')
        sort: Sort by 'stars', 'forks', 'updated', or 'best-match' (default: stars)
        limit: Number of results (1-15, default 5)
    """
    try:
        limit = max(1, min(15, limit))
        sort_param = sort if sort != "best-match" else ""
        params = {"q": query, "per_page": limit, "sort": sort_param, "order": "desc"}
        
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{GITHUB_API}/search/repositories", params=params, headers=_github_headers())
            resp.raise_for_status()
            data = resp.json()
        
        items = data.get("items", [])
        if not items:
            return f"No repositories found for: {query}"
        
        results = []
        for repo in items:
            stars = repo.get("stargazers_count", 0)
            stars_fmt = f"{stars/1000:.1f}k" if stars >= 1000 else str(stars)
            lang = repo.get("language") or "?"
            updated = repo.get("updated_at", "")[:10]
            desc = (repo.get("description") or "No description")[:120]
            results.append(
                f"- **{repo['full_name']}** ({stars_fmt} stars, {lang}) - updated {updated}\n"
                f"  {desc}\n"
                f"  {repo['html_url']}"
            )
        
        total = data.get("total_count", len(items))
        header = f"GitHub search: '{query}' ({total} total, showing {len(items)})\n"
        return header + "\n".join(results)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return "GitHub API rate limit exceeded. Set GITHUB_TOKEN env var for higher limits."
        return f"GitHub API error: {e.response.status_code}"
    except Exception as e:
        return f"Error searching GitHub: {e}"


def github_trending(language: str = "", since: str = "daily") -> str:
    """
    Find trending repositories on GitHub.
    Uses the search API with date filters to approximate trending.
    
    Args:
        language: Filter by programming language (e.g. 'python', 'rust'). Empty for all.
        since: Time window - 'daily', 'weekly', or 'monthly' (default: daily)
    """
    try:
        from datetime import datetime, timedelta
        
        days_map = {"daily": 1, "weekly": 7, "monthly": 30}
        days = days_map.get(since, 1)
        date_cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        
        query = f"created:>{date_cutoff}"
        if language:
            query += f" language:{language}"
        
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": 10}
        
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{GITHUB_API}/search/repositories", params=params, headers=_github_headers())
            resp.raise_for_status()
            data = resp.json()
        
        items = data.get("items", [])
        if not items:
            lang_str = f" ({language})" if language else ""
            return f"No trending repos found{lang_str} for {since} timeframe."
        
        results = []
        for repo in items:
            stars = repo.get("stargazers_count", 0)
            stars_fmt = f"{stars/1000:.1f}k" if stars >= 1000 else str(stars)
            lang = repo.get("language") or "?"
            desc = (repo.get("description") or "No description")[:120]
            results.append(
                f"- **{repo['full_name']}** ({stars_fmt} stars, {lang})\n"
                f"  {desc}\n"
                f"  {repo['html_url']}"
            )
        
        lang_str = f" in {language}" if language else ""
        header = f"Trending repos{lang_str} ({since}):\n"
        return header + "\n".join(results)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return "GitHub API rate limit exceeded. Set GITHUB_TOKEN env var for higher limits."
        return f"GitHub API error: {e.response.status_code}"
    except Exception as e:
        return f"Error fetching trending repos: {e}"


def github_repo_info(repo: str) -> str:
    """
    Get detailed info about a GitHub repository.
    
    Args:
        repo: Repository in 'owner/repo' format (e.g. 'langchain-ai/langchain')
    """
    try:
        repo = repo.strip().strip("/")
        
        with httpx.Client(timeout=15) as client:
            # Fetch repo details
            resp = client.get(f"{GITHUB_API}/repos/{repo}", headers=_github_headers())
            resp.raise_for_status()
            data = resp.json()
            
            # Try to get latest release
            latest_release = None
            try:
                rel_resp = client.get(f"{GITHUB_API}/repos/{repo}/releases/latest", headers=_github_headers())
                if rel_resp.status_code == 200:
                    rel = rel_resp.json()
                    latest_release = f"{rel.get('tag_name', '?')} ({rel.get('published_at', '')[:10]})"
            except Exception:
                pass
        
        stars = data.get("stargazers_count", 0)
        stars_fmt = f"{stars/1000:.1f}k" if stars >= 1000 else str(stars)
        forks = data.get("forks_count", 0)
        issues = data.get("open_issues_count", 0)
        lang = data.get("language") or "?"
        desc = data.get("description") or "No description"
        created = data.get("created_at", "")[:10]
        updated = data.get("updated_at", "")[:10]
        license_name = (data.get("license") or {}).get("spdx_id", "None")
        topics = data.get("topics", [])
        
        info = [
            f"**{data['full_name']}**",
            f"  {desc}",
            f"  Stars: {stars_fmt} | Forks: {forks} | Open Issues: {issues}",
            f"  Language: {lang} | License: {license_name}",
            f"  Created: {created} | Last updated: {updated}",
        ]
        if latest_release:
            info.append(f"  Latest release: {latest_release}")
        if topics:
            info.append(f"  Topics: {', '.join(topics[:10])}")
        info.append(f"  URL: {data['html_url']}")
        
        return "\n".join(info)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Repository not found: {repo}"
        return f"GitHub API error: {e.response.status_code}"
    except Exception as e:
        return f"Error fetching repo info: {e}"


# ---------------------------------------------------------------------------
# Reddit & Hacker News Tools
# ---------------------------------------------------------------------------

def reddit_top(subreddit: str = "all", sort: str = "hot", time_filter: str = "day", limit: int = 10) -> str:
    """
    Get top posts from a subreddit (or r/all).
    
    Args:
        subreddit: Subreddit name without r/ (default: 'all'). Examples: 'programming', 'selfhosted', 'MachineLearning'
        sort: Sort order - 'hot', 'top', 'new', 'rising' (default: hot)
        time_filter: For 'top' sort - 'hour', 'day', 'week', 'month', 'year', 'all' (default: day)
        limit: Number of posts (1-25, default 10)
    """
    try:
        limit = max(1, min(25, limit))
        subreddit = subreddit.strip().strip("/").replace("r/", "")
        
        url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
        params = {"limit": limit, "t": time_filter, "raw_json": 1}
        headers = {"User-Agent": "Katta-Agent/1.0"}
        
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        
        posts = data.get("data", {}).get("children", [])
        if not posts:
            return f"No posts found in r/{subreddit} ({sort})"
        
        results = []
        for i, post in enumerate(posts, 1):
            p = post.get("data", {})
            score = p.get("score", 0)
            score_fmt = f"{score/1000:.1f}k" if score >= 1000 else str(score)
            comments = p.get("num_comments", 0)
            title = p.get("title", "?")[:120]
            url_link = p.get("url", "")
            permalink = f"https://reddit.com{p.get('permalink', '')}"
            sub = p.get("subreddit", subreddit)
            
            # Determine if it's a self post, link, or image
            is_self = p.get("is_self", False)
            link_info = ""
            if not is_self and url_link and "reddit.com" not in url_link:
                link_info = f" -> {url_link}"
            
            results.append(
                f"{i}. [{score_fmt} pts, {comments} comments] r/{sub}\n"
                f"   {title}{link_info}\n"
                f"   {permalink}"
            )
        
        header = f"r/{subreddit} - {sort}" + (f" (top {time_filter})" if sort == "top" else "") + ":\n"
        return header + "\n".join(results)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Subreddit not found: r/{subreddit}"
        if e.response.status_code == 403:
            return f"r/{subreddit} is private or quarantined."
        return f"Reddit error: {e.response.status_code}"
    except Exception as e:
        return f"Error fetching Reddit posts: {e}"


def reddit_search(query: str, subreddit: str = "", sort: str = "relevance", time_filter: str = "all", limit: int = 10) -> str:
    """
    Search Reddit for posts matching a query. Returns titles, scores, comment counts, and links.
    
    Args:
        query: Search query
        subreddit: Limit to a specific subreddit (optional, empty = all of reddit)
        sort: Sort order - 'relevance', 'hot', 'top', 'new', 'comments' (default: relevance)
        time_filter: Time filter - 'hour', 'day', 'week', 'month', 'year', 'all' (default: all)
        limit: Number of results, 1-25 (default 10)
    """
    try:
        limit = max(1, min(25, limit))
        sub = subreddit.strip().strip("/").replace("r/", "") if subreddit else "all"
        
        url = f"https://www.reddit.com/r/{sub}/search.json"
        params = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": limit,
            "restrict_sr": "on" if subreddit else "off",
            "raw_json": 1,
        }
        headers = {"User-Agent": "Katta-Agent/1.0"}
        
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        
        posts = data.get("data", {}).get("children", [])
        if not posts:
            return f"No Reddit results for: {query}"
        
        results = []
        for i, post in enumerate(posts, 1):
            p = post.get("data", {})
            score = p.get("score", 0)
            score_fmt = f"{score/1000:.1f}k" if score >= 1000 else str(score)
            comments = p.get("num_comments", 0)
            title = p.get("title", "?")[:140]
            permalink = f"https://reddit.com{p.get('permalink', '')}"
            sub_name = p.get("subreddit", sub)
            selftext = (p.get("selftext") or "")[:300]
            
            entry = (
                f"{i}. [{score_fmt} pts, {comments} comments] r/{sub_name}\n"
                f"   {title}\n"
                f"   {permalink}"
            )
            if selftext:
                entry += f"\n   Preview: {selftext}"
            results.append(entry)
        
        return f"Reddit search: \"{query}\"\n" + "\n".join(results)
    except Exception as e:
        return f"Error searching Reddit: {e}"


def reddit_read_thread(url: str, comment_limit: int = 15) -> str:
    """
    Read a Reddit thread: the post body + top comments. Use this to actually read
    what people are saying in a discussion.
    
    Args:
        url: Reddit post URL (e.g. https://reddit.com/r/movies/comments/abc123/title/)
        comment_limit: Max comments to return (default 15)
    """
    try:
        json_url = url.rstrip("/") + ".json"
        headers = {"User-Agent": "Katta-Agent/1.0"}
        
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(json_url, params={"raw_json": 1, "limit": comment_limit}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        
        if not isinstance(data, list) or len(data) < 2:
            return "Could not parse Reddit thread."
        
        # Post body
        post_data = data[0]["data"]["children"][0]["data"]
        title = post_data.get("title", "?")
        selftext = (post_data.get("selftext") or "")[:1500]
        score = post_data.get("score", 0)
        sub = post_data.get("subreddit", "?")
        
        output = [f"**r/{sub}: {title}** ({score} pts)\n"]
        if selftext:
            output.append(selftext + "\n")
        
        # Comments
        comments = data[1]["data"]["children"]
        output.append(f"--- Top Comments ({min(len(comments), comment_limit)}) ---")
        for c in comments[:comment_limit]:
            cd = c.get("data", {})
            if cd.get("body") is None:
                continue
            author = cd.get("author", "?")
            cscore = cd.get("score", 0)
            body = cd.get("body", "")[:400]
            output.append(f"\n**{author}** ({cscore} pts):\n{body}")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error reading Reddit thread: {e}"


def twitter_search(query: str, limit: int = 10) -> str:
    """
    Search Twitter/X for recent posts using the bird CLI tool.
    Great for leaks, rumors, breaking news, real-time opinions, and insider info.
    
    Args:
        query: Search query (e.g. 'GPT-5 leak', 'best comedy movie 2024')
        limit: Number of tweets to return (default 10, max 20)
    """
    try:
        limit = max(1, min(20, limit))
        result = subprocess.run(
            ["bird", "search", query, "-n", str(limit)],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            err = result.stderr.strip()
            if err:
                return f"Twitter search error: {err}"
            return f"Twitter search returned no results for: {query}"
        
        if not output:
            return f"No tweets found for: {query}"
        
        return f"Twitter/X search: \"{query}\"\n{output}"
    except FileNotFoundError:
        return "Error: bird CLI is not installed."
    except subprocess.TimeoutExpired:
        return "Twitter search timed out."
    except Exception as e:
        return f"Error searching Twitter: {e}"


def hackernews_top(story_type: str = "top", limit: int = 10) -> str:
    """
    Get top stories from Hacker News.
    
    Args:
        story_type: Type of stories - 'top', 'best', 'new', 'ask', 'show' (default: top)
        limit: Number of stories (1-25, default 10)
    """
    try:
        limit = max(1, min(25, limit))
        HN_API = "https://hacker-news.firebaseio.com/v0"
        
        type_map = {
            "top": "topstories",
            "best": "beststories",
            "new": "newstories",
            "ask": "askstories",
            "show": "showstories",
        }
        endpoint = type_map.get(story_type, "topstories")
        
        with httpx.Client(timeout=15) as client:
            # Get story IDs
            resp = client.get(f"{HN_API}/{endpoint}.json")
            resp.raise_for_status()
            story_ids = resp.json()[:limit]
            
            # Fetch each story's details
            stories = []
            for sid in story_ids:
                try:
                    s_resp = client.get(f"{HN_API}/item/{sid}.json")
                    if s_resp.status_code == 200:
                        stories.append(s_resp.json())
                except Exception:
                    continue
        
        if not stories:
            return f"No stories found for type: {story_type}"
        
        results = []
        for i, story in enumerate(stories, 1):
            title = story.get("title", "?")
            score = story.get("score", 0)
            comments = story.get("descendants", 0)
            url = story.get("url", "")
            hn_link = f"https://news.ycombinator.com/item?id={story.get('id', '')}"
            by = story.get("by", "?")
            
            link_info = f"\n   Link: {url}" if url else ""
            results.append(
                f"{i}. [{score} pts, {comments} comments] by {by}\n"
                f"   {title}{link_info}\n"
                f"   HN: {hn_link}"
            )
        
        header = f"Hacker News ({story_type}):\n"
        return header + "\n".join(results)
    except Exception as e:
        return f"Error fetching Hacker News: {e}"


# ---------------------------------------------------------------------------
# RSS Feed Tools
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Code Runner Sandbox
# ---------------------------------------------------------------------------

def run_code(language: str, code: str, timeout: int = 30) -> str:
    """
    Run a code snippet in a sandboxed subprocess with timeout.
    Returns stdout and stderr output.
    
    Args:
        language: 'python', 'javascript' (or 'js'), or 'shell' (or 'bash')
        code: The code to execute
        timeout: Max execution time in seconds (1-60, default 30)
    """
    try:
        timeout = max(1, min(60, timeout))
        language = language.strip().lower()
        
        lang_map = {
            "python": ["python3", "-c"],
            "python3": ["python3", "-c"],
            "javascript": ["node", "-e"],
            "js": ["node", "-e"],
            "node": ["node", "-e"],
            "shell": ["bash", "-c"],
            "bash": ["bash", "-c"],
            "sh": ["sh", "-c"],
        }
        
        if language not in lang_map:
            supported = "python, javascript, shell"
            return f"Unsupported language: '{language}'. Supported: {supported}"
        
        cmd = lang_map[language]
        
        result = subprocess.run(
            cmd + [code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/tmp",
            env={
                **os.environ,
                "HOME": "/tmp",
            }
        )
        
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")
        if result.returncode != 0:
            output_parts.append(f"[exit code: {result.returncode}]")
        
        output = "\n".join(output_parts) if output_parts else "(no output)"
        return _sanitize_output(_truncate_output(output))
    except subprocess.TimeoutExpired:
        return f"Code execution timed out after {timeout}s"
    except FileNotFoundError:
        return f"Runtime not found for '{language}'. Make sure it's installed."
    except Exception as e:
        return f"Error running code: {e}"


def housekeep_memory() -> str:
    """
    Cleans up memory.md:
    - Expires journal entries older than 7 days
    - Removes duplicate entries
    - Trims excess whitespace
    Returns a summary of what was cleaned.
    """
    from datetime import datetime, timedelta
    
    logger.info("Running memory housekeeping")
    
    if not os.path.exists(MEMORY_FILE):
        return "No memory file found."
    
    with open(MEMORY_FILE, 'r') as f:
        memory = f.read()
    
    changes = []
    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    
    # Find the Journal section
    journal_match = re.search(r'(## Journal\b.*?)(?=\n## |\Z)', memory, re.DOTALL)
    if journal_match:
        journal_section = journal_match.group(1)
        journal_lines = journal_section.split('\n')
        kept_lines = []
        expired_count = 0
        
        for line in journal_lines:
            # Check if this line has a date tag [YYYY-MM-DD]
            date_match = re.match(r'^-?\s*\[(\d{4}-\d{2}-\d{2})\]', line.strip())
            if date_match:
                entry_date = date_match.group(1)
                if entry_date < cutoff:
                    expired_count += 1
                    continue  # Skip expired entries
            kept_lines.append(line)
        
        if expired_count > 0:
            new_journal = '\n'.join(kept_lines)
            memory = memory[:journal_match.start()] + new_journal + memory[journal_match.end():]
            changes.append(f"Expired {expired_count} journal entries older than 7 days")
    
    # Remove excessive blank lines (more than 2 consecutive)
    original_len = len(memory)
    memory = re.sub(r'\n{4,}', '\n\n\n', memory)
    if len(memory) < original_len:
        changes.append("Trimmed excess whitespace")
    
    # Write back
    with open(MEMORY_FILE, 'w') as f:
        f.write(memory)
    
    if changes:
        summary = "Memory housekeeping complete:\n" + "\n".join(f"  - {c}" for c in changes)
    else:
        summary = "Memory housekeeping complete: nothing to clean up."
    
    logger.info(summary)
    return summary



# We will expose these tool definitions to the agent
CORE_TOOLS = {
    "run_shell_command": run_shell_command,
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
    "install_package": install_package,
    "update_memory": update_memory,
    "read_memory": read_memory,
    "web_search": web_search,
    "fetch_url": fetch_url,
    "create_script": create_script,
    "run_script": run_script,
    "list_scripts": list_scripts,
    "add_startup_command": add_startup_command,
    "remove_startup_command": remove_startup_command,
    "list_startup_commands": list_startup_commands,
    "housekeep_memory": housekeep_memory,
    "github_search": github_search,
    "github_trending": github_trending,
    "github_repo_info": github_repo_info,
    "reddit_top": reddit_top,
    "reddit_search": reddit_search,
    "reddit_read_thread": reddit_read_thread,
    "twitter_search": twitter_search,
    "hackernews_top": hackernews_top,
    "run_code": run_code,
    "deep_research": deep_research,
    "manage_access": manage_access,
}
