"""Run unit tests or explicitly isolated PostgreSQL integration tests."""
import argparse
import os
import subprocess
import sys
from app.config import ROOT, validate_test_target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--integration', action='store_true')
    parser.add_argument('--database')
    parser.add_argument('--junitxml')
    args = parser.parse_args()
    scope = 'integration' if args.integration else 'unit'
    if args.integration:
        validate_test_target(args.database)
        os.environ['FACTORY_TEST_DB_CONFIRM'] = args.database
        os.environ['FACTORY_TEST_MODE'] = '1'
    elif args.database:
        parser.error('--database is only used with --integration')
    # Separate process prevents core registry/DTO identity leaking between runs.
    command = [sys.executable, '-m', 'pytest', 'tests/' + scope, '-q']
    if args.junitxml:
        command += ['--junitxml', args.junitxml]
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == '__main__':
    raise SystemExit(main())
