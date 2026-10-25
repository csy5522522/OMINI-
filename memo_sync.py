"""Upload each pending local action; preserve concurrent edits as conflict copies."""
import base64
import hashlib
import hmac
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
    outbox = store.memos.get_data(store.owner, "sync_outbox", {})
    branches = store.memos.get_data(store.owner, "sync_conflicts", {})
    uploaded_assets = set()
    conflicts = 0

    def persist():
        with store.memos.transaction():
            store.memos.set_data(store.owner, "sync", ack)
            store.memos.set_data(store.owner, "sync_outbox", outbox)
            store.memos.set_data(store.owner, "sync_conflicts", branches)

    def payload_for(target_id, source_id, version, remote_version, conflict=False):
        operation = hmac.new(store.memos.cipher.key,
            f"memo-operation/{source_id}/{version['version']}/{target_id}".encode(), hashlib.sha256).hexdigest()
        action = version["action"]
        if action not in {"create", "edit", "rename", "delete", "restore", "rollback", "import", "external_edit"}: action = "edit"
        return {"id": target_id, "title": (version["title"] + "（冲突副本）")[:240] if conflict else version["title"],
                "content": version["content"], "deleted": bool(version["deleted"]),
                "base_version": remote_version, "action": action, "operation_id": operation}

    def committed(payload, current):
        if not current or current["version"] <= payload["base_version"]: return None
        versions = client.api(f"/api/memos/{payload['id']}/history")["versions"]
        for version in versions:
            if version["version"] != payload["base_version"] + 1: continue
            if version.get("operation_id"):
                matches = version["operation_id"] == payload["operation_id"]
            else:
                # Older servers ignore operation_id. Only reconcile an exact, durably
                # recorded in-flight action at its expected history position.
                matches = (outbox.get(payload["id"]) == payload and
                    all(version[key] == payload[key] for key in ("title", "content", "deleted")) and
                    version["action"] == ("create" if payload["base_version"] == 0 else payload["action"]))
            if matches:
                return {**version, "id": payload["id"]}
        return None

    def send(payload):
        if outbox.get(payload["id"]) == payload:
            saved = committed(payload, remote.get(payload["id"]))
            if saved: return saved
        outbox[payload["id"]] = payload
        persist()  # The request intent survives a restart even if its response is lost.
        upload_images(payload["content"])
        return client.api("/api/memos", payload)["memo"]

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
        if since == current["version"]:
            return store.memos.get(store.owner, current["id"])
        versions = client.api(f"/api/memos/{current['id']}/history")["versions"]
        adopted = None
        for version in reversed(versions):
            if since < version["version"] <= current["version"]:
                download_images(version["content"])
                adopted, _ = store.write_snapshot({**version, "id": current["id"]}, version["action"])
                ack[current["id"]] = {"local": adopted["version"], "remote": version["version"]}
                persist()
        if adopted is None:
            download_images(current["content"])
            adopted, _ = store.write_snapshot(current)
        return adopted

    processed = set()
    for local in store.memos.listing(store.owner):
        memo_id = local["id"]
        if memo_id in processed: continue
        base = ack.get(memo_id, {"local": 0, "remote": 0})
        pending = [v for v in reversed(store.memos.history(store.owner, memo_id)) if v["version"] > base["local"]]
        current = remote.get(memo_id)
        # Recover committed actions before deciding whether there is a real conflict.
        while pending and (current["version"] if current else 0) != base["remote"] and memo_id not in branches:
            payload = payload_for(memo_id, memo_id, pending[0], base["remote"])
            saved = committed(payload, current)
            if not saved: break
            base = {"local": pending.pop(0)["version"], "remote": saved["version"]}
            ack[memo_id] = base
            outbox.pop(memo_id, None)
            persist()
        if memo_id in branches or pending and (current["version"] if current else 0) != base["remote"]:
            # Keep the cloud record and upload all pending actions under a new ID.
            branch = branches.setdefault(memo_id, {"id": uuid.uuid4().hex, "local": base["local"], "remote": 0})
            conflict_id = branch["id"]
            persist()
            for version in pending:
                if version["version"] <= branch["local"]: continue
                conflict = send(payload_for(conflict_id, memo_id, version, branch["remote"], True))
                branch.update(local=version["version"], remote=conflict["version"])
                outbox.pop(conflict_id, None)
                persist()
            conflict = client.api("/api/memos")["memos"]
            conflict = next(value for value in conflict if value["id"] == conflict_id)
            copied = adopt_remote(conflict, ack.get(conflict_id, {}).get("remote", 0))
            ack[conflict_id] = {"local": copied["version"], "remote": conflict["version"]}
            conflicts += 1
            adopted = adopt_remote(current, base["remote"])
            ack[memo_id] = {"local": adopted["version"], "remote": current["version"]}
            branches.pop(memo_id, None)
            outbox.pop(memo_id, None)
            remote.pop(conflict_id, None)
            processed.add(conflict_id)
        elif pending:
            remote_version = base["remote"]
            for version in pending:
                result = send(payload_for(memo_id, memo_id, version, remote_version))
                remote_version = result["version"]
                ack[memo_id] = {"local": version["version"], "remote": remote_version}
                outbox.pop(memo_id, None)
                persist()
        elif current and current["version"] != base["remote"]:
            adopted = adopt_remote(current, base["remote"])
            ack[memo_id] = {"local": adopted["version"], "remote": current["version"]}
        remote.pop(memo_id, None)
        persist()
    for memo_id, current in remote.items():
        adopted = adopt_remote(current)
        ack[memo_id] = {"local": adopted["version"], "remote": current["version"]}
        persist()
    return {"conflicts": conflicts, "count": len(store.files())}
