"""
Prints the matrix of the test job of the CI workflow as JSON: one entry for each tox environment whose name gives a
suite and a Python, Django and PostgreSQL version.

An entry has the name of the environment and the Python and PostgreSQL versions that its name gives. The environment
that CODECOV_ENVIRONMENT names also uploads its coverage to Codecov.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

#: The checkout that this script is in, whose tox configuration is read.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: The environment that uploads its coverage, so that the badge shows the coverage of one run.
CODECOV_ENVIRONMENT = 'core-py315-dj61-pg18'

TEST_ENVIRONMENT_NAME = re.compile(r'^(?P<suite>.+)-py3(?P<python>\d+)-dj\d+-pg(?P<postgres>\d+)$')


def tox_environment_names():
    output = subprocess.run(
        [sys.executable, '-m', 'tox', 'list', '--no-desc'],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def matrix_entry(match):
    entry = {'toxenv': match.string, 'python': '3.{}'.format(match['python']), 'postgres': match['postgres']}

    if match.string == CODECOV_ENVIRONMENT:
        entry['codecov'] = True

    return entry


def main():
    entries = []

    for name in tox_environment_names():
        match = TEST_ENVIRONMENT_NAME.match(name)
        if match is not None:
            entries.append(matrix_entry(match))

    print(json.dumps(entries))


if __name__ == '__main__':
    main()
