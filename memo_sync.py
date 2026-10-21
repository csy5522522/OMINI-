"""Upload each pending local action; preserve concurrent edits as conflict copies."""
import base64
import re
import uuid
from account_client import AccountClient
from text_notes import TextNotesStore, read_text_file

ASSET = re.compile(r"omini-asset://([a-f0-9]{64})")


def sync_memos(directory, url, token):
    store = TextNotesStore(directory)
    client = AccountClient(url, token)
    for path in store.files():
        previous = store.for_path(path)
        text, _ = read_text_file(path)
        if text != previous["content"]: store.record(path, "external_edit")
    remote = {memo["id"]: memo for memo in client.api("/api/memos")["memos"]}
    ack = store.memos.get_data(store.owner, "sync", {})
    uploaded_assets = set()
    conflicts = 0

    def upload_images(content):
        for asset_id in set(ASSET.findall(content)) - uploaded_assets:
            asset = store.memos.asset(store.owner, asset_id)
            client.api("/api/memos/assets", {"name": asset["name"], "data": base64.b64encode(asset["data"]).decode()})
            uploaded_assets.add(asset_id)

    def download_images(content):
        for asset_id in set(ASSET.findall(content)):
            try: store.memos.asset(store.owner, asset_id)
            except ValueError:
                asset = client.api("/api/memos/assets/" + asset_id)
                store.memos.put_asset(store.owner, asset["name"], asset["data"])

    def adopt_remote(current, since=0):
        versions = client.api(f"/api/memos/{current['id']}/history")["versions"]
        adopted = None
        for version in reversed(versions):
            if since < version["version"] <= current["version"]:
                download_images(version["content"])
                adopted, _ = store.write_snapshot({**version, "id": current["id"]}, version["action"])
        if adopted is None:
            download_images(current["content"])
            adopted, _ = store.write_snapshot(current)
        return adopted

    for local in store.memos.listing(store.owner):
        memo_id = local["id"]
        base = ack.get(memo_id, {"local": 0, "remote": 0})
        pending = [v for v in reversed(store.memos.history(store.owner, memo_id)) if v["version"] > base["local"]]
        current = remote.get(memo_id)
        if pending and (current["version"] if current else 0) != base["remote"]:
            # Keep the cloud record and upload all pending actions under a new ID.
            conflict_id, remote_version = uuid.uuid4().hex, 0
            for version in pending:
                upload_images(version["content"])
                conflict = client.api("/api/memos", {"id": conflict_id, "title": (version["title"] + "（冲突副本）")[:240],
                    "content": version["content"], "deleted": bool(version["deleted"]), "base_version": remote_version,
                    "action": version["action"]})["memo"]
                remote_version = conflict["version"]
            copied, _ = store.write_snapshot(conflict, "conflict_copy")
            ack[conflict_id] = {"local": copied["version"], "remote": conflict["version"]}
            conflicts += 1
            adopted = adopt_remote(current, base["remote"])
            ack[memo_id] = {"local": adopted["version"], "remote": current["version"]}
        elif pending:
            remote_version = base["remote"]
            for version in pending:
                upload_images(version["content"])
                result = client.api("/api/memos", {"id": memo_id, "title": version["title"], "content": version["content"],
                    "deleted": bool(version["deleted"]), "base_version": remote_version, "action": version["action"]})["memo"]
                remote_version = result["version"]
                ack[memo_id] = {"local": version["version"], "remote": remote_version}
                store.memos.set_data(store.owner, "sync", ack)
        elif current and current["version"] != base["remote"]:
            adopted = adopt_remote(current, base["remote"])
            ack[memo_id] = {"local": adopted["version"], "remote": current["version"]}
        remote.pop(memo_id, None)
        store.memos.set_data(store.owner, "sync", ack)
    for memo_id, current in remote.items():
        adopted = adopt_remote(current)
        ack[memo_id] = {"local": adopted["version"], "remote": current["version"]}
        store.memos.set_data(store.owner, "sync", ack)
    return {"conflicts": conflicts, "count": len(store.files())}
