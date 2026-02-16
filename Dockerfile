FROM python:3.14-slim

# Install system dependencies
# Including tools useful for a research agent
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Networking
    curl \
    wget \
    iputils-ping \
    dnsutils \
    # Version control
    git \
    # Text processing
    jq \
    lynx \
    html2text \
    # Build tools (for pip packages with native extensions)
    build-essential \
    # Process management
    procps \
    # File utilities
    file \
    unzip \
    # GPG for NodeSource
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 22 from NodeSource (needed for npm CLI tools like bird)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app /app/app
COPY .env.example .env.example 
# (Real .env provided by docker compose or ignored, but good to have the example)

# Create data directories
RUN mkdir -p /app/app/data /app/app/plugins

# NOTE: Running as root to allow apt-get install at runtime.
# The install_package tool requires root privileges.
# For production with untrusted users, consider alternative approaches.

# Runtime defaults
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.main"]
