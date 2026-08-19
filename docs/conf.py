#!/usr/bin/env python3
#
# django-postgres-objects documentation build configuration file.

import datetime
import os
import sys

sys.path.insert(0, os.path.abspath('../src/'))

from django.conf import settings  # noqa: E402

from postgres_objects import __version__  # noqa: E402

settings.configure()


# -- General configuration ------------------------------------------------

extensions = ['sphinx.ext.autodoc', 'sphinx.ext.coverage', 'sphinx.ext.githubpages']

templates_path = ['_templates']

source_suffix = '.rst'

master_doc = 'index'

project = 'django-postgres-objects'
copyright = '{} DjanQuiltDB Project'.format(datetime.date.today().year)  # noqa: A001
author = 'DjanQuiltDB Project'

release = version = __version__

language = 'en'

exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

pygments_style = 'sphinx'

todo_include_todos = False


# -- Options for HTML output ----------------------------------------------

html_theme = 'alabaster'

html_static_path = ['_static']


# -- Options for HTMLHelp output ------------------------------------------

htmlhelp_basename = project


# -- Options for LaTeX output ---------------------------------------------

latex_elements = {}

latex_documents = [
    (master_doc, 'django-postgres-objects.tex', '{} Documentation'.format(project), author, 'manual'),
]


# -- Options for manual page output ---------------------------------------

man_pages = [(master_doc, project, '{} Documentation'.format(project), [author], 1)]


# -- Options for Texinfo output -------------------------------------------

texinfo_documents = [
    (
        master_doc,
        project,
        '{} Documentation'.format(project),
        author,
        project,
        'Declarative PostgreSQL objects for handling by Django migration framework.',
        'Miscellaneous',
    ),
]
