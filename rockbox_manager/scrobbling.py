# SPDX-License-Identifier: GPL-2.0-only
"""Explicit Last.fm/ListenBrainz writes with account-scoped, conservative receipts."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

import requests

from .artwork_engine import write_private_text
from .common import USER_AGENT
from .listening import Listen

LASTFM = "https://ws.audioscrobbler.com/2.0/"
LISTENBRAINZ = "https://api.listenbrainz.org/1/"
SERVICES = ("lastfm", "listenbrainz")


class ServiceError(ValueError):
    pass


def request_json(url, *, data=None, payload=None, token=""):
    """Fixed endpoints only, no redirects/proxies/cookies or unbounded replies."""
    if url not in (LASTFM, LISTENBRAINZ + "validate-token", LISTENBRAINZ + "submit-listens"):
        raise ServiceError("Unsupported scrobbling endpoint.")
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.headers.update({"User-Agent": USER_AGENT})
            headers = {"Authorization": "Token " + token} if token else {}
            method = session.get if url.endswith("validate-token") else session.post
            kwargs = {} if url.endswith("validate-token") else {"data": data, "json": payload}
            with method(url, headers=headers, timeout=(10, 30), stream=True, allow_redirects=False, **kwargs) as reply:
                if reply.status_code != 200:
                    raise ServiceError(f"Service returned HTTP {reply.status_code}; no automatic retry.")
                raw = bytearray()
                for part in reply.iter_content(8192):
                    raw.extend(part)
                    if len(raw) > 256 * 1024:
                        raise ServiceError("Service response exceeded 256 KiB.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ServiceError("Unexpected service response.")
                return result
    except ServiceError:
        raise
    except Exception:
        # Never put credentials, a request body or arbitrary server text in logs/UI.
        raise ServiceError("The request could not be verified; no automatic retry.") from None


def signature(params, secret):
    text = "".join(k + str(params[k]) for k in sorted(params) if k not in ("format", "callback")) + secret
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def lastfm_call(method, credentials, **params):
    data = {"method": method, "api_key": credentials["api_key"], **params}
    data["api_sig"] = signature(data, credentials["api_secret"])
    data["format"] = "json"
    result = request_json(LASTFM, data=data)
    if "error" in result:
        code = result["error"]
        raise ServiceError(f"Last.fm API error {code if isinstance(code, int) else 'unknown'}; check authorization/rate limits.")
    return result


def auth_token(key, secret):
    if not re.fullmatch(r"[a-fA-F0-9]{32}", key) or not re.fullmatch(r"[a-fA-F0-9]{32}", secret):
        raise ServiceError("Enter your Last.fm application's 32-character API key and shared secret.")
    token = lastfm_call("auth.getToken", {"api_key": key, "api_secret": secret}).get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", token):
        raise ServiceError("Invalid authorization token response.")
    return token, "https://www.last.fm/api/auth/?" + urlencode({"api_key": key, "token": token})


def finish_auth(key, secret, token):
    result = lastfm_call("auth.getSession", {"api_key": key, "api_secret": secret}, token=token).get("session", {})
    username, session = result.get("name"), result.get("key")
    if not isinstance(username, str) or not username.strip() or len(username) > 200 or not isinstance(session, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", session):
        raise ServiceError("Invalid Last.fm session response.")
    return {"api_key": key, "api_secret": secret, "session": session, "username": username}


def validate_listenbrainz(token):
    if not re.fullmatch(r"[A-Za-z0-9-]{16,200}", token):
        raise ServiceError("Enter your ListenBrainz user token.")
    result = request_json(LISTENBRAINZ + "validate-token", token=token)
    name = result.get("user_name")
    if result.get("valid") is not True or not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ServiceError("ListenBrainz did not validate this token.")
    return {"token": token, "username": name}


def load_accounts(path):
    if path.is_symlink():
        raise ServiceError("The scrobbling credential file must not be a symlink.")
    if not path.exists():
        return {}
    if path.stat().st_size > 16384:
        raise ServiceError("Unexpected scrobbling credential file.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ServiceError("Invalid scrobbling settings.")
    for service, values in data.items():
        required = ("api_key", "api_secret", "session", "username") if service == "lastfm" else ("token", "username")
        if service not in SERVICES or not isinstance(values, dict) or any(not isinstance(values.get(k), str) or not values[k] for k in required):
            raise ServiceError("Invalid scrobbling account settings.")
    return data


def save_account(path, service, values):
    if service not in SERVICES:
        raise ServiceError("Unknown service.")
    data = load_accounts(path)
    if values is None:
        data.pop(service, None)
    else:
        data[service] = values
    write_private_text(path, json.dumps(data))


def account_id(service, credentials):
    name = credentials.get("username", "").strip().casefold()
    if service not in SERVICES or not name:
        raise ServiceError("Connect this service first.")
    return hashlib.sha256((service + ":" + name).encode()).hexdigest()


def eligibility(event, service, now):
    if event.rating != "L":
        return "Skipped track"
    if not event.artist or event.artist.casefold() in ("<untagged>", "untagged") or not event.title:
        return "Artist/title missing"
    if event.duration < 30:
        return "Track shorter than 30 seconds"
    if event.timestamp > now:
        return "Future timestamp; review the log timezone/clock"
    if service == "lastfm" and event.timestamp < now - 14 * 86400:
        return "Older than Last.fm's 14-day window"
    return ""


@dataclass(frozen=True)
class SubmissionPlan:
    service: str
    account: str
    events: tuple[Listen, ...]
    excluded: tuple[str, ...]


def prepare(store, events, service, credentials, now=None):
    now = int(time.time()) if now is None else now
    account = account_id(service, credentials)
    ready, excluded, seen = [], [], set()
    with store.connection() as con:
        for event in events:
            if event.id in seen:
                continue
            seen.add(event.id)
            row = con.execute("SELECT * FROM listens WHERE id=?", (event.id,)).fetchone()
            if not row or any(row[k] != v for k, v in asdict(event).items()):
                raise ServiceError("History selection changed; reload and review again.")
            receipt = con.execute("SELECT status FROM submissions WHERE event=? AND service=? AND account=?",
                                  (event.id, service, account)).fetchone()
            reason = eligibility(event, service, now) or (f"Existing receipt: {receipt[0]}" if receipt else "")
            if reason:
                excluded.append(f"{event.artist} — {event.title}: {reason}")
            else:
                ready.append(event)
    if len(ready) > 200:
        raise ServiceError("Select at most 200 events per reviewed submission.")
    return SubmissionPlan(service, account, tuple(ready), tuple(excluded))


def lastfm_outcomes(data, events):
    group = data.get("scrobbles", {})
    rows = group.get("scrobble", [])
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list) or len(rows) != len(events):
        raise ServiceError("Incomplete Last.fm receipts; check your profile before retrying.")
    outcomes = []
    for row, event in zip(rows, events):
        if str(row.get("timestamp")) != str(event.timestamp):
            raise ServiceError("Last.fm receipt timestamps do not match this submission.")
        code = str(row.get("ignoredMessage", {}).get("code", ""))
        if not code.isdigit():
            raise ServiceError("Missing Last.fm per-track receipt.")
        outcomes.append(("accepted", "Accepted") if code == "0" else ("ignored", f"Last.fm ignored code {code}"))
    attr = group.get("@attr", {})
    if int(attr.get("accepted", -1)) != sum(s == "accepted" for s, _ in outcomes) or int(attr.get("ignored", -1)) != sum(s == "ignored" for s, _ in outcomes):
        raise ServiceError("Last.fm receipt totals do not match.")
    return outcomes


def submit(store, plan, credentials, *, confirmed=False, cancel=None, progress=lambda _: None):
    if not confirmed or account_id(plan.service, credentials) != plan.account:
        raise ServiceError("Confirm a reviewed selection for this account before sending.")
    cancel = cancel or threading.Event()
    summary = {"accepted": 0, "ignored": 0, "unknown": 0, "skipped": 0}
    batch_size = 50 if plan.service == "lastfm" else 100
    for offset in range(0, len(plan.events), batch_size):
        if cancel.is_set():
            break
        attempt, chunk = uuid.uuid4().hex, []
        with store.connection() as con:
            for event in plan.events[offset:offset + batch_size]:
                if eligibility(event, plan.service, int(time.time())):
                    summary["skipped"] += 1
                    continue
                inserted = con.execute("INSERT OR IGNORE INTO submissions VALUES (?,?,?,?,?,?)",
                                       (event.id, plan.service, plan.account, "unknown", "Request may be in flight; review service before retrying", attempt))
                if inserted.rowcount:
                    chunk.append(event)
        if not chunk:
            continue
        try:
            if plan.service == "lastfm":
                fields = {"sk": credentials["session"]}
                for i, event in enumerate(chunk):
                    fields.update({f"artist[{i}]": event.artist, f"album[{i}]": event.album, f"track[{i}]": event.title,
                                   f"timestamp[{i}]": str(event.timestamp), f"duration[{i}]": str(event.duration)})
                outcomes = lastfm_outcomes(lastfm_call("track.scrobble", credentials, **fields), chunk)
            else:
                payload = {"listen_type": "import", "payload": [
                    {"listened_at": t.timestamp, "track_metadata": {"artist_name": t.artist, "track_name": t.title,
                     "release_name": t.album, "additional_info": {"duration": t.duration, "media_player": "Rockbox"}}} for t in chunk]}
                result = request_json(LISTENBRAINZ + "submit-listens", payload=payload, token=credentials["token"])
                if result.get("status") != "ok":
                    raise ServiceError("ListenBrainz did not confirm acceptance.")
                outcomes = [("accepted", "Accepted")] * len(chunk)
        except Exception:
            outcomes = [("unknown", "Unverified result. Check the service before explicitly allowing a retry.")] * len(chunk)
        with store.connection() as con:
            for t, (state, detail) in zip(chunk, outcomes):
                con.execute("UPDATE submissions SET status=?,detail=? WHERE event=? AND service=? AND account=? AND attempt=?",
                            (state, detail, t.id, plan.service, plan.account, attempt))
                summary[state] += 1
        progress(f"Scrobbling: {sum(summary.values())} reviewed events processed")
        if any(state == "unknown" for state, _ in outcomes):
            break  # Never compound an ambiguous request with automatic retries.
        cancel.wait(.25)
    return summary


def receipts(store, service, credentials):
    account = account_id(service, credentials)
    with store.connection() as con:
        return {r[0]: (r[1], r[2]) for r in con.execute("SELECT event,status,detail FROM submissions WHERE service=? AND account=?", (service, account))}


def allow_retry(store, ids, service, credentials):
    account = account_id(service, credentials)
    with store.connection() as con:
        for key in ids:
            con.execute("DELETE FROM submissions WHERE event=? AND service=? AND account=? AND status!='accepted'",
                        (key, service, account))
