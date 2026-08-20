"""
Settings for the test project. This is not a template for a real deployment.
"""

import os

import dj_database_url

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

SECRET_KEY = 'test-project-only-do-not-use-this-anywhere-real'  # nosec B105

DEBUG = True

ALLOWED_HOSTS = []

INSTALLED_APPS = (
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'postgres_objects',
    'example',
    'bakery',
)

DATABASES = {
    'default': dj_database_url.parse(
        os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/test_db')
    )
}

DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

USE_TZ = True

POSTGRES_OBJECTS = {
    'FUNCTIONS_MODULE_PATH': 'db_functions',
    'VIEWS_MODULE_PATH': 'db_views',
}
