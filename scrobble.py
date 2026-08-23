import json
import os
import re
import sys
import time
import hashlib

import pylast
import requests


OAUTH_PATH = "browser.json"
SNAPSHOT_PATH = "last_snapshot.json"


def fetch_history(auth_path: str, max_tracks: int = 200, max_pages: int = 10) -> list[dict]:
    with open(auth_path) as f:
        auth = json.load(f)
    cookie = auth["cookie"]
    authuser = auth.get("x-goog-authuser", "0")
    # Compute a fresh SAPISIDHASH at request time so it never ages out.
    sapsid = re.search(r"(?:^|;\s)SAPISID=([^;]+)", cookie).group(1)
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapsid} https://music.youtube.com".encode()).hexdigest()
    authorization = f"SAPISIDHASH {ts}_{h}"
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    headers = {
        "Authorization": authorization,
        "x-goog-authuser": authuser,
        "x-origin": "https://music.youtube.com",
        "origin": "https://music.youtube.com",
        "content-type": "application/json",
        "cookie": cookie,
        "user-agent": ua,
    }
    url = "https://music.youtube.com/youtubei/v1/browse?key=AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
    context = {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20260818.08.00"}}

    tracks: list[dict] = []
    body = {"context": context, "browseId": "FEmusic_history"}
    pages = 0
    while len(tracks) < max_tracks and pages <= max_pages:
        r = requests.post(url, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        resp = r.json()
        tracks.extend(parse_history(resp))
        cont = extract_continuation(resp)
        if not cont:
            break
        body = {"context": context, "continuation": cont}
        pages += 1

    # De-dup by videoId, keep first (newest) occurrence.
    seen = set()
    deduped = []
    for t in tracks:
        if t["videoId"] not in seen:
            seen.add(t["videoId"])
            deduped.append(t)
    return deduped[:max_tracks]


def parse_history(resp: dict) -> list[dict]:
    def get_runs(col):
        cr = col.get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [])
        return " ".join(x.get("text", "") for x in cr).strip()

    tracks = []
    for r0 in extract_items(resp):
        vid = r0.get("playlistItemData", {}).get("videoId")
        if not vid:
            continue
        fc = r0.get("flexColumns", [])
        title = get_runs(fc[0]) if len(fc) > 0 else "Unknown Title"
        artist = get_runs(fc[1]) if len(fc) > 1 else "Unknown Artist"
        tracks.append({"videoId": vid, "title": title, "artist": artist})
    return tracks


def extract_items(resp: dict) -> list[dict]:
    items = []
    def walk(o):
        if isinstance(o, dict):
            if "musicResponsiveListItemRenderer" in o:
                items.append(o["musicResponsiveListItemRenderer"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(resp)
    return items


def extract_continuation(resp: dict) -> str | None:
    found = []
    def walk(o):
        if isinstance(o, dict):
            if "nextContinuationData" in o:
                found.append(o["nextContinuationData"].get("continuation"))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(resp)
    return next((c for c in found if c), None)


def load_snapshot(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def diff_tracks(current: list[dict], snapshot: list[dict], min_seq: int = 3) -> list[dict]:
    if not snapshot:
        return []
    snap_ids = [t["videoId"] for t in snapshot]
    curr_ids = [t["videoId"] for t in current]
    join = len(current)
    for i in range(len(current) - min_seq + 1):
        if curr_ids[i : i + min_seq] == snap_ids[:min_seq]:
            join = i
            break
    return list(reversed(current[:join]))  # oldest first


def assign_timestamps(tracks: list[dict]) -> list[dict]:
    now = int(time.time())
    total = len(tracks)
    for i, track in enumerate(tracks):
        track["timestamp"] = now - (total - i) * 180
    return tracks


def scrobble(tracks: list[dict]) -> int:
    api_key = os.environ["LASTFM_API_KEY"]
    api_secret = os.environ["LASTFM_SECRET"]
    username = os.environ["LASTFM_USERNAME"]
    password = pylast.md5(os.environ["LASTFM_PASSWORD"])

    network = pylast.LastFMNetwork(
        api_key=api_key,
        api_secret=api_secret,
        username=username,
        password_hash=password,
    )

    scrobbled = 0
    for track in tracks:
        for attempt in range(3):
            try:
                network.scrobble(
                    artist=track["artist"],
                    title=track["title"],
                    timestamp=track["timestamp"],
                )
                print(f"Scrobbled: {track['artist']} - {track['title']}")
                scrobbled += 1
                time.sleep(1)
                break
            except (pylast.NetworkError, pylast.MalformedResponseError) as e:
                print(f"Attempt {attempt + 1} failed for {track['title']}: {e}")
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"Skipping: {track['title']} after 3 failed attempts")

    return scrobbled


def save_snapshot(tracks: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump(tracks, f, indent=2)


def prune_logs(log_path: str, keep_days: int = 365) -> None:
    if not os.path.exists(log_path):
        return
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    with open(log_path) as f:
        lines = f.readlines()
    kept = []
    for line in lines:
        try:
            ts = datetime.fromisoformat(line.split("|")[0].strip())
            if ts > cutoff:
                kept.append(line)
        except ValueError:
            kept.append(line)
    with open(log_path, "w") as f:
        f.writelines(kept)


def write_log(log_path: str, scrobbled: int, new_tracks: int) -> None:
    from datetime import datetime, timezone
    prune_logs(log_path)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(log_path, "a") as f:
        f.write(f"{ts} | scrobbled={scrobbled} | new_tracks={new_tracks}\n")


def main():
    try:
        current = fetch_history(OAUTH_PATH)
        snapshot = load_snapshot(SNAPSHOT_PATH)
        backfill = os.environ.get("BACKFILL", "").lower() == "true"

        if backfill:
            new_tracks = current
            print(f"BACKFILL mode: scrobbling {len(new_tracks)} tracks from history.")
        else:
            new_tracks = diff_tracks(current, snapshot)

        if new_tracks:
            new_tracks = assign_timestamps(new_tracks)
            scrobbled = scrobble(new_tracks)
        else:
            print("No new tracks to scrobble.")
            scrobbled = 0

        save_snapshot(current, SNAPSHOT_PATH)
        write_log("runs.log", scrobbled, len(new_tracks))
        print(f"Done. Scrobbled {scrobbled} track(s).")

    except Exception as e:
        import traceback
        print(f"Error: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
