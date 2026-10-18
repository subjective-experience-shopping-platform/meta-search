"""Run the plan's live E2E scenario on an isolated DB, with three reviews per product."""

import json
import time
from pathlib import Path

from meta_search.config import load_settings
from meta_search.db import database, init_schema
from meta_search.jobs import get_registration_status, register_product
from meta_search.search.service import SearchService

root = Path(__file__).resolve().parents[1]
settings = load_settings(root / "data/live_e2e_config.toml")
with database(settings.db_path) as c:
    init_schema(c)
urls = (
    json.loads((root / "data/live_urls.json").read_text())
    if (root / "data/live_urls.json").exists()
    else [
        "https://www.keychron.com/products/keychron-k8-pro-qmk-via-wireless-mechanical-keyboard",
        "https://www.keychron.com/products/keychron-k2-pro-qmk-via-wireless-mechanical-keyboard",
        "https://www.keychron.com/products/keychron-k6-pro-qmk-via-wireless-custom-mechanical-keyboard",
    ]
)
for url in urls:
    job = register_product(settings, url)
    print("START", url, job["job_id"], flush=True)
    while True:
        status = get_registration_status(settings, job["job_id"])
        if status["status"] in ("done", "failed"):
            print("FINISHED", json.dumps(status, ensure_ascii=False), flush=True)
            break
        time.sleep(5)
    if status["status"] != "done":
        raise SystemExit(1)
service = SearchService(settings)
guide = service.get_search_guide()
settings.data_dir.joinpath("guide.json").write_text(json.dumps(guide, ensure_ascii=False, indent=2))
print("E2E registrations complete", flush=True)
