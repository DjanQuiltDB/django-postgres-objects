FROM fedora:44

# Fedora 44 ships Python 3.14 as python3 and Python 3.15 as python3.15, so one image runs every tox environment. The
# postgresql package provides psql. No compiler or libpq headers are needed, because psycopg-binary ships wheels for
# both Pythons.
RUN dnf install -y \
    git \
    postgresql \
    python3 \
    python3.15 \
    && dnf clean all

# The development tools go in their own virtual environment, so that nothing is installed into the system Python.
RUN python3.14 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Copy the packaging metadata first so the dependency install layer is cached independently of the source.
COPY pyproject.toml README.rst LICENSE ./
COPY src ./src

RUN pip install --upgrade pip && \
    pip install -e .[dev]

COPY tests ./tests
COPY docs ./docs

# Compose runs the container as the host user, so that what it writes to the mounted checkout is not owned by root.
# That user has no home directory in the image, so HOME points at /tmp, where pip and virtualenv can keep their caches.
# Compose mounts an anonymous volume on each of these directories, and a new volume takes the owner and mode of the
# directory in the image, so the directories are made writable by any user.
ENV HOME=/tmp
RUN mkdir -p .tox .venv .ruff_cache && \
    chmod 1777 .tox .venv .ruff_cache

CMD ["tox"]
