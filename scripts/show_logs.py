"""Read job events without starting a server, model, browser, or registration."""

import argparse
import json

from meta_search.config import load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("--config", help="META_SEARCH_CONFIG override")
    parser.add_argument("--last", type=int, default=50)
    parser.add_argument("--event", help="Event name substring, e.g. llm / process / pipeline")
    parser.add_argument("--level", choices=("INFO", "WARNING", "ERROR"))
    parser.add_argument("--json", action="store_true", help="Print original JSONL records")
    args = parser.parse_args()
    if args.last < 1 or not args.job_id.isalnum():
        parser.error("--last must be positive; job_id must be alphanumeric")
    settings = load_settings(args.config)
    path = settings.data_dir / "jobs" / args.job_id / "events.jsonl"
    if not path.exists():
        parser.error(f"No structured log at {path}; check the job ID and config path")
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.event and args.event not in record["event"]:
            continue
        if args.level and args.level != record["level"]:
            continue
        records.append(record)
    for record in records[-args.last :]:
        if args.json:
            print(json.dumps(record, ensure_ascii=False))
        else:
            print(
                f"{record['timestamp']} {record['level']:7} {record['event']} "
                f"{json.dumps(record['data'], ensure_ascii=False)}"
            )
            if "traceback" in record:
                print(record["traceback"])


if __name__ == "__main__":
    main()
