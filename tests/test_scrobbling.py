"""No real account calls: per-track receipts, unknown outcomes and account isolation."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rockbox_manager import scrobbling as s
from rockbox_manager.listening import ListeningStore, preview_log
from test_listening import log_line


class ScrobblingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        log = base / "log"
        self.now = int(time.time())
        log.write_text("#TZ/UTC\n" + log_line(self.now-600) + log_line(self.now-300, title="Second"))
        self.store = ListeningStore(base / "history.sqlite3")
        preview = preview_log(log)
        self.store.import_preview(preview)
        self.events = preview.events
        self.credentials = {"username": "Example", "api_key": "a"*32, "api_secret": "b"*32, "session": "c"*32}
        self.plan = s.prepare(self.store, self.events, "lastfm", self.credentials)

    def reply(self, codes=("0", "0")):
        return {"scrobbles": {"@attr": {"accepted": str(codes.count("0")), "ignored": str(len(codes)-codes.count("0"))},
                "scrobble": [{"timestamp": str(t.timestamp), "ignoredMessage": {"code": c}} for t,c in zip(self.events,codes)]}}

    def test_partial_lastfm_acceptance_is_not_reported_as_all_success(self):
        with patch.object(s, "request_json", return_value=self.reply(("0", "3"))) as http:
            result = s.submit(self.store, self.plan, self.credentials, confirmed=True)
        self.assertEqual((result["accepted"], result["ignored"]), (1, 1))
        self.assertEqual(http.call_count, 1)
        states = s.receipts(self.store, "lastfm", self.credentials)
        self.assertEqual(states[self.events[1].id], ("ignored", "Last.fm ignored code 3"))
        self.assertEqual(s.prepare(self.store, self.events, "lastfm", self.credentials).events, ())

    def test_timeout_is_persisted_and_never_automatically_retried(self):
        with patch.object(s, "request_json", side_effect=TimeoutError("private-token-value")) as http:
            result = s.submit(self.store, self.plan, self.credentials, confirmed=True)
            again = s.submit(self.store, self.plan, self.credentials, confirmed=True)
        self.assertEqual(result["unknown"], 2)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(again["accepted"], 0)
        self.assertNotIn("private-token-value", str(s.receipts(self.store, "lastfm", self.credentials)))
        s.allow_retry(self.store, [self.events[0].id], "lastfm", self.credentials)
        self.assertEqual(len(s.prepare(self.store, self.events, "lastfm", self.credentials).events), 1)

    def test_accepted_receipts_cannot_be_reset_and_accounts_are_independent(self):
        with patch.object(s, "request_json", return_value=self.reply()):
            s.submit(self.store, self.plan, self.credentials, confirmed=True)
        s.allow_retry(self.store, [t.id for t in self.events], "lastfm", self.credentials)
        self.assertEqual(len(s.prepare(self.store, self.events, "lastfm", self.credentials).events), 0)
        other = {**self.credentials, "username": "Another account"}
        self.assertEqual(len(s.prepare(self.store, self.events, "lastfm", other).events), 2)
        with self.assertRaises(s.ServiceError):
            s.submit(self.store, self.plan, other, confirmed=True)

    def test_missing_mismatched_or_inconsistent_receipts_remain_unknown(self):
        for kind in ("missing", "time", "totals"):
            response = self.reply()
            if kind == "missing": response["scrobbles"]["scrobble"].pop()
            if kind == "time": response["scrobbles"]["scrobble"][0]["timestamp"] = "1"
            if kind == "totals": response["scrobbles"]["@attr"]["accepted"] = "0"
            with self.subTest(kind=kind), self.assertRaises((ValueError, s.ServiceError)):
                s.lastfm_outcomes(response, self.events)

    def test_listenbrainz_confirmation_and_manual_opt_in(self):
        credentials = {"username": "Example", "token": "dummy-token-value"}
        plan = s.prepare(self.store, self.events, "listenbrainz", credentials)
        with patch.object(s, "request_json", return_value={"status":"ok"}) as http:
            with self.assertRaises(s.ServiceError):
                s.submit(self.store, plan, credentials)
            http.assert_not_called()
            result = s.submit(self.store, plan, credentials, confirmed=True)
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(http.call_args.kwargs["payload"]["listen_type"], "import")

    def test_eligibility_skips_old_future_short_and_skipped_events(self):
        from dataclasses import replace
        for event in (replace(self.events[0],rating="S"), replace(self.events[0],duration=20),
                      replace(self.events[0],timestamp=self.now+10), replace(self.events[0],timestamp=self.now-15*86400)):
            self.assertTrue(s.eligibility(event,"lastfm", self.now))
        self.assertEqual(s.eligibility(replace(self.events[0],timestamp=self.now-15*86400),"listenbrainz", self.now), "")

    def test_credentials_are_separate_private_and_not_returned_in_errors(self):
        path = Path(self.tmp.name) / "scrobbling.json"
        s.save_account(path, "lastfm", self.credentials)
        self.assertEqual(s.load_accounts(path)["lastfm"], self.credentials)
        import os
        if os.name == "posix": self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with patch.object(s, "request_json", return_value={"error": 9, "message": "secret-token"}):
            with self.assertRaisesRegex(s.ServiceError, "API error 9") as raised:
                s.lastfm_call("track.scrobble", self.credentials)
        self.assertNotIn("secret-token", str(raised.exception))
        s.save_account(path, "lastfm", None)
        self.assertEqual(s.load_accounts(path), {})

    def test_auth_signature_and_real_transport_guards(self):
        import hashlib
        self.assertEqual(s.signature({"b":"é","a":"1","format":"json"},"secret"),
                         hashlib.md5("a1bésecret".encode()).hexdigest())
        class Reply:
            status_code = 200
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def iter_content(self,_): yield json.dumps({"valid": True,"user_name":"Example"}).encode()
        class Session:
            def __enter__(self): return self
            def __exit__(self,*_): pass
            headers = {}
            def get(inner,url,**kwargs):
                self.assertFalse(inner.trust_env)
                self.assertFalse(kwargs["allow_redirects"])
                self.assertTrue(kwargs["stream"])
                return Reply()
        with patch.object(s.requests,"Session",Session):
            self.assertEqual(s.validate_listenbrainz("a"*32)["username"], "Example")
        with self.assertRaises(s.ServiceError):
            s.request_json("https://example.org/", token="private")

    def test_receipt_is_durable_before_request_and_survives_process_interruption(self):
        def interrupted(*args, **kwargs):
            states=s.receipts(self.store,"lastfm",self.credentials)
            self.assertEqual({v[0] for v in states.values()},{"unknown"})
            self.assertEqual(len(states),2)
            raise SystemExit("simulated process exit")
        with patch.object(s,"request_json",side_effect=interrupted):
            with self.assertRaises(SystemExit):
                s.submit(self.store,self.plan,self.credentials,confirmed=True)
        self.assertEqual(s.prepare(self.store,self.events,"lastfm",self.credentials).events,())

    def test_desktop_auth_validates_tokens_and_keeps_secrets_out_of_browser_url(self):
        with patch.object(s,"request_json",return_value={"token":"d"*32}):
            token,url=s.auth_token("a"*32,"b"*32)
        self.assertEqual(token,"d"*32)
        self.assertTrue(url.startswith("https://www.last.fm/api/auth/?"))
        self.assertNotIn("b"*32,url)
        with patch.object(s,"request_json",return_value={"session":{"name":"Example","key":"c"*32}}):
            account=s.finish_auth("a"*32,"b"*32,token)
        self.assertEqual(account["username"],"Example")
