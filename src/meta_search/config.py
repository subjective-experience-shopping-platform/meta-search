import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    config_path: Path
    data_dir: Path
    prompts_dir: Path
    schemas_dir: Path
    limits: dict
    agy: dict
    codex: dict
    remote_normalization: dict

    @property
    def project_root(self):
        return self.config_path.parent

    @property
    def db_path(self):
        return self.data_dir / "meta_search.sqlite"

    @property
    def initial_table_path(self):
        return self.project_root / self.remote_normalization.get(
            "initial_table", "bootstrap_exact_matches.json"
        )


def load_settings(path=None):
    path = Path(
        path or os.environ.get("META_SEARCH_CONFIG") or Path(__file__).resolve().parents[2] / "config.toml"
    ).resolve()
    with path.open("rb") as file:
        config = tomllib.load(file)
    paths = {key: (path.parent / value).resolve() for key, value in config["paths"].items()}
    return Settings(
        path, **paths, **{key: config[key] for key in ("limits", "agy", "codex", "remote_normalization")}
    )
