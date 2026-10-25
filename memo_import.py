"""Copy local memo actions into an account, retaining originals and retry identity."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import uuid

from text_notes import TextNotesStore

ASSET = re.compile(r"omini-asset://([a-f0-9]{64})")


def import_workspace(source, target):
    if source.directory == target.directory: return 0
    for path in source.files():
        source.for_path(path)
    imported = target.memos.get_data(target.owner, "local_imports", {})
    count = 0
    for memo in source.memos.listing(source.owner):
        progress = imported.get(memo["id"], {})
        since = progress.get("version", 0)
        target_id = progress.get("id") or uuid.uuid5(uuid.NAMESPACE_URL, "omini-local-memo:" + memo["id"]).hex
        versions = [v for v in reversed(source.memos.history(source.owner, memo["id"])) if v["version"] > since]
        if not versions: continue
        for version in versions:
            for asset_id in set(ASSET.findall(version["content"])):
                asset = source.memos.asset(source.owner, asset_id)
                target.memos.put_asset(target.owner, asset["name"], base64.b64encode(asset["data"]).decode())
            try: previous = target.memos.get(target.owner, target_id)
            except ValueError: previous = None
            pending = imported.get(memo["id"], {})
            retry = (pending.get("pending") == version["version"] and previous and
                     pending.get("target_version") == previous["version"] and
                     all(previous[k] == version[k] for k in ("title", "content", "deleted")))
            if not retry:
                planned = target.snapshot_path({**version, "id": target_id})
                imported[memo["id"]] = {"id": target_id, "version": version["version"] - 1,
                    "pending": version["version"], "target_version": (previous["version"] if previous else 0) + 1,
                    "path": planned.name}
                target.memos.set_data(target.owner, "local_imports", imported)
                target.write_snapshot({**version, "id": target_id}, version["action"])
            else:
                path = target.snapshot_path({**version, "id": target_id})
                mapping = target.mapping()
                if not version["deleted"]:
                    target._write(path, version["title"], version["content"])
                    mapping[target_id] = path.name
                else:
                    if path.exists():
                        trash = target.directory / ".trash"; trash.mkdir(exist_ok=True)
                        path.rename(trash / (uuid.uuid4().hex + ".ominote"))
                    mapping.pop(target_id, None)
                target.memos.set_data(target.owner, "paths", mapping)
            imported[memo["id"]] = {"id": target_id, "version": version["version"]}
            target.memos.set_data(target.owner, "local_imports", imported)
        count += 1
    return count


def adopt_local_workspace(config_path, config, account_key, target):
    base = Path(config_path).parent
    directory = Path(config.get("directory") or base / "text-notes")
    if not directory.is_dir() or directory.resolve() == target.directory: return 0
    source = TextNotesStore(directory)
    owner = source.memos.get_data(source.owner, "account_owner", "")
    if not owner:
        migration = base / "account-workspaces" / "migration.json"
        if migration.exists() and json.loads(migration.read_text(encoding="utf-8")).get("account") != account_key:
            return 0
        source.memos.set_data(source.owner, "account_owner", account_key)
    elif owner != account_key:
        return 0
    return import_workspace(source, target)
