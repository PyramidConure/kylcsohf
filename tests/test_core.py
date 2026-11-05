import os
import base64
import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from creator_control.credentials import Vault
from creator_control.db import Database, now, restore_database
from creator_control.planning import calendar, preparation_status
from creator_control.platforms import APIError, Instagram, YouTube, google_login, meta_login_finish, meta_login_url
from creator_control.publishing import Publisher, report, reporting_zone
from creator_control.tools import LDPlayer, import_video, parse_instances, probe, program, run, sha256, vertical


class MemoryVault(dict):
    def put(self, key, value):
        self[key] = value

    def remove(self, key):
        self.pop(key, None)


def account_data(platform="Instagram"):
    today = date.today().isoformat()
    return dict(platform=platform, name="Тест", profile="", created=today, start=today,
                kind="Новый", topic="Наука", country="US", language="English", timezone="America/New_York",
                duration=14, owner="Автор", emulator=0, subtopics="", keywords="", ideas="", prep_override="")


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "workspace.sqlite3")
        self.vault = MemoryVault()
        self.publisher = Publisher(self.db, self.vault)

    def create_post(self, platform):
        account = self.db.save_account(account_data(platform))
        remote = "12345" if platform == "Instagram" else "UC_test"
        self.db.bind(account, remote, "own_account")
        file = self.root / "video.mp4"
        file.write_bytes(b"mock-media")
        video = self.db.add_video(file, sha256(file), dict(
            width=1080, height=1920, video="h264", audio="aac", audio_rate=48000,
            seconds=10.0, fps=30, container="mov,mp4", video_bitrate=1_000_000,
        ))
        identifier = self.db.save_post(dict(account_id=account, video_id=video, title="Новый выпуск",
            caption="Текст", topic="Наука", due=now(), privacy="private", for_kids=False,
            synthetic=False, video_url="https://example.org/video.mp4"))
        return identifier, account, remote

    def test_default_calendar_and_shortest_variant(self):
        start = date(2026, 11, 4)
        for duration in (3, 14, 30, 365):
            tasks = list(calendar(start, duration, "US"))
            self.assertEqual(len(tasks), 22)
            phases = {}
            for key, phase, stage, title, due in tasks:
                phases.setdefault(phase, []).append((date.fromisoformat(due) - start).days + 1)
            self.assertEqual(phases["content"][-1], duration)
            self.assertEqual(phases["followup"][0], duration + 1)
            if duration == 14:
                self.assertEqual([(v[0], v[-1]) for v in phases.values()], [(1, 3), (4, 7), (8, 14), (15, 21)])
        self.assertEqual(len(list(calendar(start, 14, "DE"))), 21)

    def test_calendar_preserves_completed_tasks_and_backup(self):
        account = self.db.save_account(account_data())
        rows = self.db.tasks(account)
        first = rows[0]
        self.assertEqual(preparation_status(self.db.account(account), rows), "Не начато")
        self.db.complete(first["id"], True)
        self.db.save_account({**account_data(), "duration": 28, "country": "DE"}, account)
        rows = self.db.tasks(account)
        self.assertEqual(rows[0]["id"], first["id"])
        self.assertEqual(rows[0]["done"], 1)
        self.assertEqual(preparation_status(self.db.account(account), rows), "Настройка профиля")
        self.assertNotIn("captions_en", {r["template"] for r in rows})
        backup = self.db.backup(self.root / "backup.sqlite3")
        self.assertEqual(Database(backup).tasks(account), rows)
        with self.assertRaises(ValueError):
            self.db.backup(backup)

    def test_restoration_uses_fresh_credentials_and_test_mode(self):
        identifier, account, _ = self.create_post("YouTube")
        self.db.settings({"test_mode": "0", "workspace_id": "original"})
        backup = self.db.backup(self.root / "backup.sqlite3")
        destination = self.root / "restore.sqlite3"
        restore_database(backup, destination)
        restored = Database(destination)
        self.assertEqual(restored.post(identifier)["account_id"], account)
        self.assertEqual(restored.post(identifier)["state"], "restored")
        self.assertEqual(restored.setting("test_mode"), "1")
        self.assertNotEqual(restored.setting("workspace_id"), "original")
        with self.assertRaises(ValueError):
            Publisher(restored, self.vault).send(identifier)
        restored.release_restored_draft(identifier)
        self.assertEqual(restored.post(identifier)["state"], "planned")
        with self.assertRaises(ValueError):
            restore_database(backup, destination)

    def test_restored_upload_requires_reconciliation(self):
        identifier, _, remote = self.create_post("YouTube")
        self.db.settings({"test_mode": "0"})
        self.db.claim_post(self.db.post(identifier), remote)
        backup = self.db.backup(self.root / "backup.sqlite3")
        destination = self.root / "restore.sqlite3"
        restore_database(backup, destination)
        restored = Database(destination)
        with self.assertRaises(ValueError):
            restored.release_restored_draft(identifier)
        publisher = Publisher(restored, self.vault)
        client = Mock()
        with patch.object(publisher, "client", return_value=(client, remote)):
            publisher.associate(identifier, "Found_1")
        client.video.assert_called_once_with("Found_1", remote)
        client.start_upload.assert_not_called()
        self.assertEqual(restored.post(identifier)["state"], "submitted")

    def test_video_relocation_preserves_queue_and_rejects_other_content(self):
        identifier, _, _ = self.create_post("YouTube")
        post = self.db.post(identifier)
        invalid = self.root / "another.mp4"
        invalid.write_bytes(b"other-media")
        with self.assertRaisesRegex(ValueError, "Содержимое"):
            self.db.relocate_video(post["video_id"], invalid, sha256(invalid))
        self.assertEqual(self.db.post(identifier)["path"], post["path"])
        destination = Path(post["path"]).rename(self.root / "new-name.mp4")
        self.db.relocate_video(post["video_id"], destination, sha256(destination))
        self.assertEqual(Path(self.db.post(identifier)["path"]), destination)
        self.assertIn("Тест пройден", self.publisher.send(identifier))

    def test_test_mode_does_not_call_network_or_claim(self):
        identifier, _, _ = self.create_post("YouTube")
        with patch.object(self.publisher, "client") as client:
            self.assertIn("Тест пройден", self.publisher.send(identifier))
            client.assert_not_called()
        self.assertEqual(self.db.post(identifier)["state"], "planned")
        self.assertEqual(self.db.post_log(identifier), [])
        Path(self.db.post(identifier)["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "изменился"):
            self.publisher.send(identifier)

    def test_duplicate_video_queue_and_concurrent_claim(self):
        identifier, _, remote = self.create_post("YouTube")
        post = self.db.post(identifier)
        with self.assertRaises(ValueError):
            self.db.save_post(post)
        self.db.settings({"test_mode": "0"})

        def claim():
            try:
                self.db.claim_post(post, remote)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: claim(), range(2))), [False, True])

    def test_youtube_upload_resumes_same_session_after_network_error(self):
        identifier, _, remote = self.create_post("YouTube")
        self.db.settings({"test_mode": "0"})
        client = Mock()
        client.start_upload.return_value = "session"
        client.upload_state.side_effect = [(0, None), APIError("YouTube"), (4, None), (10, "Video_1")]
        with patch.object(self.publisher, "client", return_value=(client, remote)):
            with self.assertRaises(APIError):
                self.publisher.send(identifier)
            self.assertEqual(self.vault[f"upload/{identifier}"], "session")
            self.assertEqual(self.db.post(identifier)["state"], "sending")
            self.publisher.send(identifier, resume=True)
            with self.assertRaises(ValueError):
                self.publisher.send(identifier)
        client.start_upload.assert_called_once()
        self.assertEqual(client.upload_state.call_args.args, ("session", 10, 4, b"-media"))
        self.assertEqual(self.db.post(identifier)["remote_id"], "Video_1")
        self.assertNotIn(f"upload/{identifier}", self.vault)

    def test_youtube_status_recovers_result_without_sending_bytes(self):
        identifier, _, remote = self.create_post("YouTube")
        self.db.settings({"test_mode": "0"})
        self.db.claim_post(self.db.post(identifier), remote)
        self.vault.put(f"upload/{identifier}", "session")
        client = Mock()
        client.upload_state.side_effect = [(4, None), (10, "Video_1")]
        with patch.object(self.publisher, "client", return_value=(client, remote)):
            self.assertIn("4 из 10 байт", self.publisher.check(identifier))
            self.publisher.check(identifier)
        self.assertEqual([call.args for call in client.upload_state.call_args_list], [("session", 10)] * 2)
        client.start_upload.assert_not_called()
        self.assertEqual(self.db.post(identifier)["remote_id"], "Video_1")

    def test_meta_never_repeats_ambiguous_publish(self):
        identifier, _, remote = self.create_post("Instagram")
        self.db.settings({"test_mode": "0"})
        client = Mock()
        client.create_reel.return_value = "456"
        client.container_status.side_effect = ["IN_PROGRESS", "FINISHED", "FINISHED", "PUBLISHED"]
        client.publish_reel.side_effect = APIError("Instagram", 503)
        with patch.object(self.publisher, "client", return_value=(client, remote)):
            self.publisher.send(identifier)
            self.assertEqual(self.db.post(identifier)["container"], "456")
            with self.assertRaises(APIError):
                self.publisher.send(identifier, resume=True)
            with self.assertRaises(ValueError):
                self.publisher.send(identifier, resume=True)
            self.assertIn("заблокирована", self.publisher.check(identifier))
            self.publisher.check(identifier)
        self.assertEqual(self.db.post(identifier)["state"], "confirmed")
        self.assertIsNone(self.db.post(identifier)["remote_id"])
        with self.assertRaises(ValueError):
            self.db.reset_unpublished_reel(identifier)
        client.publish_reel.assert_called_once()
        client.create_reel.assert_called_once()

    def test_meta_expired_container_before_publish_can_be_reset(self):
        identifier, _, remote = self.create_post("Instagram")
        self.db.settings({"test_mode": "0"})
        client = Mock()
        client.create_reel.return_value = "456"
        client.container_status.return_value = "EXPIRED"
        with patch.object(self.publisher, "client", return_value=(client, remote)):
            self.publisher.send(identifier)
        self.assertEqual(self.db.post(identifier)["state"], "failed")
        client.publish_reel.assert_not_called()
        self.db.reset_unpublished_reel(identifier)
        self.assertEqual(self.db.post(identifier)["state"], "planned")
        self.assertIsNone(self.db.post(identifier)["container"])

    def test_meta_final_request_is_atomic_on_parallel_resume(self):
        identifier, _, remote = self.create_post("Instagram")
        self.db.settings({"test_mode": "0"})
        self.db.claim_post(self.db.post(identifier), remote)
        self.db.save_reel_container(identifier, "456")
        rendezvous = Barrier(2, timeout=5)
        client = Mock()
        client.publish_reel.return_value = "789"

        def status(_container):
            rendezvous.wait()
            return "FINISHED"

        def resume(_attempt):
            try:
                self.publisher.send(identifier, resume=True)
                return True
            except ValueError:
                return False

        client.container_status.side_effect = status
        with patch.object(self.publisher, "client", return_value=(client, remote)), ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(resume, range(2))), [False, True])
        client.publish_reel.assert_called_once_with(remote, "456")
        self.assertEqual(self.db.post(identifier)["state"], "confirmed")

    def test_provider_identity_cannot_change_saved_binding(self):
        _, account, remote = self.create_post("YouTube")
        with self.assertRaises(ValueError):
            self.db.bind(account, "other_channel", "other")
        self.assertEqual(self.db.account(account)["remote_id"], remote)

    def test_analytics_distinguishes_zero_from_unavailable(self):
        identifier, account, remote = self.create_post("YouTube")
        self.db.settings({"test_mode": "0"})
        self.db.claim_post(self.db.post(identifier), remote)
        self.db.post_state(identifier, "submitted", "Тест", remote_id="Video_1")
        client = Mock()
        client.account_metrics.return_value = {"views": 0, "subscribersGained": 2, "subscribersLost": 5}
        client.channel.return_value = {"statistics": {"hiddenSubscriberCount": True}}
        client.post_metrics.return_value = {"views": 0, "likes": None}
        with patch.object(self.publisher, "client", return_value=(client, remote)):
            self.publisher.collect(account, 7)
        metrics, posts, topics = report(self.db, account, 7)
        self.assertEqual(metrics["views"], 0)
        self.assertEqual(metrics["followers_change"], -3)
        self.assertIsNone(metrics.get("reach"))
        self.assertIsNone(metrics["followers"])
        self.assertEqual(posts[0]["views"], 0)
        self.assertEqual(topics["Наука"]["coverage"], 1)

    def test_analytics_retains_last_report_and_its_original_date(self):
        account = self.db.save_account(account_data("YouTube"))
        today = datetime.now(reporting_zone(self.db.account(account))).date()
        previous = (today - timedelta(days=1)).isoformat()
        self.db.metrics(account, "period:7", previous, {"sample": 1, "views": 0})
        self.db.metrics(account, "account", previous, {"followers": 3})
        self.db.metrics(account, "account", today.isoformat(), {"followers": 7})
        values, _, _ = report(self.db, account, 7)
        self.assertEqual((values["period_day"], values["views"], values["followers"]), (previous, 0, 3))
        self.db.metrics(account, "period:7", today.isoformat(), {"sample": 1, "views": None})
        values, _, _ = report(self.db, account, 7)
        self.assertEqual(values["period_day"], today.isoformat())
        self.assertIsNone(values.get("views"))


class AdapterTest(unittest.TestCase):
    def test_ldplayer_targets_selected_instance_and_checks_adb(self):
        instance, = parse_instances("3,Тест, имя,42,99,1,10,11")
        self.assertEqual(instance.name, "Тест, имя")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            for exe in ("ldconsole.exe", "adb.exe"):
                (Path(folder) / exe).touch()
            driver = LDPlayer(folder)
            with patch("creator_control.tools.run", side_effect=[
                "3,Тест,42,99,1,10,11", "",
                "List of devices attached\nemulator-5554\tdevice\n127.0.0.1:5561\tdevice\n",
            ]) as process:
                driver.action(3, "quit")
                adb, serial = driver.adb(instance)
                self.assertEqual(serial, "127.0.0.1:5561")
                self.assertEqual(process.call_args_list[1].args[1], ["quit", "--index", 3])
                self.assertEqual(Path(adb).name, "adb.exe")

    def test_google_upload_transport_uses_returned_offset_and_safe_host(self):
        http = Mock()
        http.send.return_value = Mock(status_code=308, headers={"Range": "bytes=0-5"})
        client = YouTube(http, "token")
        session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test"
        self.assertEqual(client.upload_state(session, 10), (6, None))
        self.assertEqual(http.send.call_args.kwargs["headers"]["Content-Range"], "bytes */10")
        with self.assertRaises(ValueError):
            client.upload_state("https://malicious.test/upload/youtube/v3/videos", 10)
        http.send.assert_called_once()

    def test_google_oauth_loopback_passes_matching_pkce(self):
        http = Mock()
        http.json.return_value = {"access_token": "fake-access", "refresh_token": "fake-refresh"}
        observed = {}
        with ThreadPoolExecutor(1) as pool:
            def open_browser(url):
                query = parse_qs(urlsplit(url).query)
                observed.update(query)
                callback = query["redirect_uri"][0] + "?" + urlencode({
                    "state": query["state"][0], "code": "fake-code",
                })
                observed["request"] = pool.submit(build_opener(ProxyHandler({})).open, callback, None, 5)
                return True

            result = google_login({"client_id": "dummy", "secret": "fake-secret"}, http, open_browser)
            with observed["request"].result(timeout=6) as response:
                self.assertEqual(response.status, 200)
        sent = http.json.call_args.kwargs["data"]
        digest = hashlib.sha256(sent["code_verifier"].encode()).digest()
        self.assertEqual(base64.urlsafe_b64encode(digest).decode().rstrip("="), observed["code_challenge"][0])
        self.assertEqual(sent["code"], "fake-code")
        self.assertEqual(result["refresh"], "fake-refresh")

    def test_meta_login_checks_state_before_exchange(self):
        http = Mock()
        config = {"client_id": "123", "secret": "fake", "redirect": "https://example.org/oauth"}
        url, state = meta_login_url(config)
        self.assertIn("instagram_business_content_publish", url)
        with self.assertRaises(ValueError):
            meta_login_finish(config, config["redirect"] + "?code=demo&state=bad", state, http)
        http.json.assert_not_called()
        http.json.side_effect = [{"access_token": "short"}, {"access_token": "long", "expires_in": 80000}]
        token = meta_login_finish(config, config["redirect"] + f"?code=demo&state={state}", state, http)
        self.assertEqual(token["access"], "long")

    def test_meta_unsupported_metric_is_unavailable(self):
        http = Mock()
        http.json.side_effect = [APIError("Instagram", 400, 100),
                                 {"data": [{"name": "reach", "total_value": {"value": 0}}]}]
        self.assertEqual(Instagram(http, "fake", "v25.0").insights("123", ("views", "reach")),
                         {"views": None, "reach": 0})

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager")
    def test_windows_credential_manager_round_trip(self):
        vault = Vault("test-" + uuid4().hex)
        self.addCleanup(vault.remove, "sample")
        self.assertIsNone(vault.get("sample"))
        vault.put("sample", {"token": "dummy-data", "owner": "Тест"})
        self.assertEqual(vault.get("sample"), {"token": "dummy-data", "owner": "Тест"})
        vault.remove("sample")
        self.assertIsNone(vault.get("sample"))

    def test_real_ffmpeg_import_convert_and_deduplicate(self):
        directory = os.environ.get("CCC_FFMPEG_DIR", "")
        if not directory:
            try:
                program(directory, "ffmpeg.exe")
                program(directory, "ffprobe.exe")
            except ValueError:
                self.skipTest("FFmpeg/FFprobe not installed. Set CCC_FFMPEG_DIR to test an external installation.")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            source, output = root / "исходник.mp4", root / "вертикальный.mp4"
            run(program(directory, "ffmpeg.exe"), [
                "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25",
                "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=44100",
                "-t", "4", "-c:v", "libx264", "-c:a", "aac", str(source),
            ])
            db = Database(root / "test.sqlite3")
            import_video(db, source, directory)
            self.assertEqual(probe(source, directory)["width"], 320)
            with self.assertRaises(ValueError):
                import_video(db, source, directory)
            result = vertical(source, output, directory)
            self.assertEqual((result["width"], result["height"], result["fps"]), (1080, 1920, 30))
            self.assertEqual((result["audio"], result["audio_rate"]), ("aac", 48000))
            import_video(db, output, directory)
            self.assertEqual(len(db.videos()), 2)
            with self.assertRaises(ValueError):
                vertical(source, output, directory)
