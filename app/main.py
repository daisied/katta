import logging
import os
import shutil
import subprocess

from dotenv import load_dotenv

from app.interfaces.discord_bot import KattaBot

# Load env
load_dotenv()

# Setup Logging
log_level = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

PACKAGES_FILE = "/app/app/data/packages.txt"
DATA_DIR = "/app/app/data"
MEMORY_FILE = f"{DATA_DIR}/memory.md"
SOURCES_FILE = f"{DATA_DIR}/sources.json"
PERMISSIONS_FILE = f"{DATA_DIR}/permissions.json"
STARTUP_SCRIPT = f"{DATA_DIR}/startup.sh"
MEMORY_TEMPLATE = f"{DATA_DIR}/memory.template.md"
SOURCES_TEMPLATE = f"{DATA_DIR}/sources.example.json"
PERMISSIONS_TEMPLATE = f"{DATA_DIR}/permissions.example.json"
STARTUP_TEMPLATE = f"{DATA_DIR}/startup.example.sh"

def restore_packages():
    """
    Reads packages.txt and installs any packages listed there.
    This enables persistence of apt packages across container restarts.
    """
    if not os.path.exists(PACKAGES_FILE):
        logger.info("No packages.txt found, skipping package restoration.")
        return
    
    try:
        with open(PACKAGES_FILE, 'r') as f:
            packages = [line.strip() for line in f if line.strip()]
        
        if not packages:
            logger.info("packages.txt is empty, no packages to restore.")
            return
        
        logger.info(f"Restoring {len(packages)} packages: {packages}")
        
        # Update apt cache first
        subprocess.run(
            ["apt-get", "update"],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        # Install all packages in one command
        cmd = ["apt-get", "install", "-y"] + packages
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode == 0:
            logger.info(f"Successfully restored packages: {packages}")
        else:
            logger.error(f"Failed to restore some packages: {result.stderr}")
            
    except Exception as e:
        logger.error(f"Error restoring packages: {e}")

def run_startup_script():
    """
    Runs startup.sh from the persistent data directory.
    Each non-comment, non-empty line is executed as a shell command.
    This enables persistence of npm packages and other setup across container restarts.
    """
    if not os.path.exists(STARTUP_SCRIPT):
        logger.info("No startup.sh found, skipping.")
        return
    
    try:
        with open(STARTUP_SCRIPT, 'r') as f:
            lines = f.readlines()
        
        commands = [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
        
        if not commands:
            logger.info("startup.sh is empty, nothing to run.")
            return
        
        logger.info(f"Running {len(commands)} startup command(s)...")
        
        for cmd in commands:
            logger.info(f"startup.sh: {cmd}")
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                if result.returncode == 0:
                    logger.info(f"startup.sh: OK - {cmd}")
                else:
                    logger.error(f"startup.sh: FAILED - {cmd}\n{result.stderr[:500]}")
            except subprocess.TimeoutExpired:
                logger.error(f"startup.sh: TIMEOUT - {cmd}")
            except Exception as e:
                logger.error(f"startup.sh: ERROR - {cmd}: {e}")
                
    except Exception as e:
        logger.error(f"Error running startup script: {e}")

def ensure_runtime_files():
    """
    Creates required runtime directories/files on first boot.
    If example templates exist, they are copied to active runtime files.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(f"{DATA_DIR}/logs", exist_ok=True)
    os.makedirs(f"{DATA_DIR}/history", exist_ok=True)
    os.makedirs("/app/app/plugins", exist_ok=True)
    os.makedirs("/app/app/scripts", exist_ok=True)

    bootstrap_map = [
        (MEMORY_FILE, MEMORY_TEMPLATE, "# Agent Memory\n\n## Known Commands\n\n## User Preferences\n\n## Notes\n\n## Journal\n"),
        (SOURCES_FILE, SOURCES_TEMPLATE, '{"rss_feeds": [], "subreddits": [], "github_dorks": [], "search_queries": []}\n'),
        (PERMISSIONS_FILE, PERMISSIONS_TEMPLATE, '{"allowed_users": [], "allowed_channels": []}\n'),
        (STARTUP_SCRIPT, STARTUP_TEMPLATE, "#!/usr/bin/env bash\n"),
        (PACKAGES_FILE, "", ""),
    ]

    for target, template, fallback_content in bootstrap_map:
        if os.path.exists(target):
            continue

        try:
            if template and os.path.exists(template):
                shutil.copyfile(template, target)
            else:
                with open(target, "w", encoding="utf-8") as f:
                    f.write(fallback_content)
            logger.info(f"Bootstrapped runtime file: {target}")
        except Exception as e:
            logger.error(f"Failed to bootstrap runtime file {target}: {e}")

    try:
        if os.path.exists(STARTUP_SCRIPT):
            os.chmod(STARTUP_SCRIPT, 0o700)
    except Exception as e:
        logger.warning(f"Could not set permissions on startup.sh: {e}")


def main():
    logger.info("Starting Katta...")
    ensure_runtime_files()
    
    # Restore persisted apt packages on startup
    restore_packages()
    
    # Run persistent startup script
    run_startup_script()
    
    # Run memory housekeeping (expire old journal entries, clean up)
    try:
        from app.core.tools import housekeep_memory
        result = housekeep_memory()
        logger.info(f"Boot housekeeping: {result}")
    except Exception as e:
        logger.error(f"Error during boot housekeeping: {e}")
    
    # Prune old session logs
    try:
        from app.core.session_logger import prune_old_logs
        pruned = prune_old_logs()
        logger.info(f"Boot log pruning: removed {pruned} old log(s)")
    except Exception as e:
        logger.error(f"Error pruning session logs: {e}")
    
    # Check config
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token or token.startswith("your_"):
        logger.error("DISCORD_BOT_TOKEN not configured properly!")
        return

    # Start Discord Bot
    bot = KattaBot()
    bot.run(token)

if __name__ == "__main__":
    main()
