FROM ubuntu:24.04

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    libpq-dev \
    postgresql-client \
    software-properties-common \
    && rm -rf /var/lib/apt/lists/*

# Install Python 3.14 from deadsnakes PPA
RUN add-apt-repository ppa:deadsnakes/ppa -y && \
    apt-get update && \
    apt-get install -y \
    python3.14 \
    python3.14-dev \
    python3.14-venv \
    && rm -rf /var/lib/apt/lists/*

# Install pip for Python 3.14. It has PEP 668 protection, hence --break-system-packages.
RUN curl https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && \
    python3.14 /tmp/get-pip.py --break-system-packages && \
    rm /tmp/get-pip.py

WORKDIR /app

# Copy the packaging metadata first so the dependency install layer is cached independently of the source.
COPY pyproject.toml README.rst LICENSE ./
COPY src ./src

RUN python3.14 -m pip install --upgrade pip setuptools wheel && \
    python3.14 -m pip install -e .[dev]

COPY tests ./tests
COPY docs ./docs

CMD ["tox"]
