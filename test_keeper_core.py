import unittest
from unittest.mock import patch
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
import threading
import types

import keeper_core as core


app_module = types.ModuleType("keeper_app_test")
SourceFileLoader("keeper_app_test", str(Path(__file__).with_name("wifi_keeper.pyw"))).exec_module(app_module)


class FakeResponse:
    def __init__(self, status, body=b"", url="http://59.68.177.9/?nasId=1"):
        self.status = status
        self.body = body
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return self.body

    def geturl(self):
        return self.url


class CoreTests(unittest.TestCase):
    def test_second_probe_can_confirm_internet(self):
        responses = [FakeResponse(200, b"captive portal"), FakeResponse(204)]
        with patch.object(core.urllib.request, "urlopen", side_effect=responses) as opener:
            self.assertTrue(core.internet_works())
            self.assertEqual(opener.call_count, 2)

    def test_portal_redirect_supplies_nas_id(self):
        with patch.object(core, "_campus_open", return_value=FakeResponse(200)):
            self.assertEqual(core.discover_nas_id(), "1")
        with patch.object(core, "_campus_open", return_value=FakeResponse(
            200, url="http://other.example/?nasId=2",
        )):
            self.assertIsNone(core.discover_nas_id())

    def test_portal_config_api_supplies_nas_id_without_page_redirect(self):
        response = FakeResponse(200, b'{"data":{"config":{"default_nas":1}}}',
                                url=core.PORTAL_CONFIG_URL)
        with patch.object(core, "_campus_open", return_value=response) as opener:
            self.assertEqual(core.detect_nas_id(), ("1", ""))
            self.assertEqual(opener.call_count, 1)

    def test_portal_page_and_pasted_url_supply_nas_id(self):
        page = FakeResponse(200, b'<script>location.href="/login?nasId=2"</script>',
                            url="http://59.68.177.9/")
        with patch.object(core, "_campus_open", return_value=page):
            self.assertEqual(core.detect_nas_id(), ("2", ""))
        self.assertEqual(core.parse_nas_id("http://59.68.177.9/login?ip=10.0.0.1&nasId=1"), "1")
        self.assertEqual(core.parse_nas_id("2"), "2")
        self.assertIsNone(core.parse_nas_id("http://59.68.177.9/"))

    def test_login_sends_expected_form_without_equating_200_with_success(self):
        config = core.Config("student", "secret", "2")
        with patch.object(core, "_campus_open", return_value=FakeResponse(
            200, b'{"code":0,"online":{"Username":"student"}}',
        )) as opener:
            self.assertEqual(core.submit_login(config), core.LoginResult("accepted", 200))
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, core.LOGIN_URL)
            self.assertIn(b"username=student", request.data)
            self.assertIn(b"password=secret", request.data)
            self.assertIn(b"nasId=2", request.data)

    def test_http_200_business_errors_are_not_success(self):
        config = core.Config("student", "wrong", "2")
        for body, expected in (
            (b'{"code":1,"msg":"bad credentials"}', "rejected"),
            (b'{"code":2}', "captcha"),
            (b'{"code":0,"online":{"Username":"another_student"}}', "account_mismatch"),
            (b'{"code":0}', "unknown"),
            (b'<html>unexpected</html>', "unknown"),
        ):
            with self.subTest(expected=expected):
                with patch.object(core, "_campus_open", return_value=FakeResponse(200, body)):
                    self.assertEqual(core.submit_login(config).kind, expected)

    def test_dpapi_roundtrip(self):
        ciphertext = core._dpapi("测试密码".encode(), False)
        self.assertNotIn("测试密码".encode(), ciphertext)
        self.assertEqual(core._dpapi(ciphertext, True).decode(), "测试密码")

    def test_school_status_distinguishes_online_and_offline(self):
        for body, expected in (
            (b'{"code":0,"online":{"Username":"student"},"dialCode":"ok:dialup"}',
             core.PortalStatus("online", "student", "ok:dialup")),
            (b'{"code":1,"msg":"offline"}', core.PortalStatus("offline")),
            (b'{"code":0}', core.PortalStatus("unknown")),
        ):
            with self.subTest(expected=expected):
                with patch.object(core, "_campus_open", return_value=FakeResponse(200, body)):
                    self.assertEqual(core.get_portal_status(), expected)

    def test_campus_client_check_requires_private_ip_and_matching_nas(self):
        for url, expected in (
            ("http://59.68.177.9/tpl/wust_yys/login.html?ip=10.1.2.3&nasId=1", True),
            ("http://59.68.177.9/tpl/wust_yys/login.html?ip=8.8.8.8&nasId=1", False),
            ("http://59.68.177.9/tpl/wust_yys/login.html?ip=10.1.2.3&nasId=2", False),
        ):
            with self.subTest(url=url):
                with patch.object(core, "_campus_open", return_value=FakeResponse(200, url=url)):
                    self.assertEqual(core.is_campus_client("1"), expected)

    def test_previous_config_can_be_loaded_without_ssid(self):
        legacy = {"ssid": "WUST-WiFi6", "username": "student",
                  "password_dpapi": "dGVzdA==", "nas_id": "1"}
        with patch.object(core, "CONFIG_PATH") as path, patch.object(core, "_dpapi", return_value=b"secret"):
            path.exists.return_value = True
            path.read_text.return_value = json.dumps(legacy)
            self.assertEqual(core.load_config(), core.Config("student", "secret", "1"))

    def test_online_account_never_triggers_login(self):
        stop = threading.Event()

        class WakeOnce:
            def clear(self):
                pass

            def wait(self, _seconds):
                stop.set()

        app = types.SimpleNamespace(
            config=core.Config("wrong_student", "wrong_password", "1"),
            stop_event=stop, manual_check=threading.Event(), check_now=WakeOnce(),
            messages=[],
        )
        app._emit = app.messages.append
        with patch.object(app_module, "get_portal_status", return_value=core.PortalStatus("online", "real_student")), \
             patch.object(app_module, "internet_works", return_value=True), \
             patch.object(app_module, "submit_login") as login:
            app_module.KeeperApp._monitor(app)
            login.assert_not_called()
        self.assertIn("其他账号在线", app.messages[-1])

    def test_offline_account_checks_campus_before_login(self):
        stop = threading.Event()

        class WakeOnce:
            def clear(self):
                pass

            def wait(self, _seconds):
                stop.set()

        manual = threading.Event()
        manual.set()
        app = types.SimpleNamespace(
            config=core.Config("student", "secret", "1"),
            stop_event=stop, manual_check=manual, check_now=WakeOnce(),
            messages=[],
        )
        app._emit = app.messages.append
        with patch.object(app_module, "get_portal_status", return_value=core.PortalStatus("offline")), \
             patch.object(app_module, "discover_nas_id", return_value="1"), \
             patch.object(app_module, "is_campus_client", return_value=False), \
             patch.object(app_module, "submit_login") as login:
            app_module.KeeperApp._monitor(app)
            login.assert_not_called()
        self.assertIn("跳过登录", app.messages[-1])


if __name__ == "__main__":
    unittest.main()
