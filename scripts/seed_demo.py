"""Create the plan's synthetic search fixture in an explicitly selected empty DB."""

import argparse
from dataclasses import replace
from pathlib import Path

from meta_search.config import load_settings
from meta_search.db import database, init_schema
from meta_search.demo import seed

parser = argparse.ArgumentParser()
parser.add_argument("--data-dir", required=True, help="Dedicated synthetic demo directory")
args = parser.parse_args()
settings = replace(load_settings(), data_dir=Path(args.data_dir).resolve())
with database(settings.db_path) as c:
    init_schema(c)
    if c.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
        raise SystemExit("Refusing to overwrite a nonempty catalog")
seed(settings, include_failed=False)
print(settings.db_path)
