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

extensions = ['sphinx.ext.autodoc', 'sphinx.ext.coverage', 'sphinx.ext.githubpages', 'sphinx_rtd_theme']

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

html_theme = 'sphinx_rtd_theme'

# The header the sidebar and the narrow-screen bar share, in the dark green the social preview card uses.
html_theme_options = {'style_nav_header_background': '#092e20'}

# docs/_templates/layout.html restyles that header; without this it is not picked up at all.
templates_path = ['_templates']

html_static_path = ['_static']

# The favicon is a raster rather than the logo itself: below about 32 pixels the icon's braces thin to less than a pixel
# and the disc seam closes up, so those sizes are rendered from assets/icon-small.svg, which is drawn for them.
html_logo = '../assets/icon.svg'
html_favicon = '../assets/icon-32.png'

# Only to style the sidebar header the template above replaces; see the file itself.
html_css_files = ['custom.css']


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
