"""Exercise actual Firefox, the installed XPI and actual adapter CLI over Docker.

No extension API is replaced or invoked from injected browser script. Scripts
below only inspect page DOM or generate genuine fetch/WebSocket traffic.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import traceback
import uuid
import zipfile

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait

EXTENSION_ID = "firefox@portmap.ylang-ylang.github.io"
EXTENSION_UUID = "181307fa-a3eb-420c-8fd9-a232d676672a"
OPTIONS_URL = "moz-extension://" + EXTENSION_UUID + "/options.html"
ARTIFACTS = Path("/artifacts")
ORIGINS = ("fixture-a", "fixture-b", "unrelated", "trap")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def records(origin, path="/requests"):
    connection = http.client.HTTPConnection(origin, 8080, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        require(response.status == 200, "Fixture evidence endpoint unavailable")
        return json.loads(response.read())
    finally:
        connection.close()


def socks_status(host="a.portmap", credentials=("user-a", "pass-a"), methods=b"\x02", port=80, command=1, address_type=3, payload=None):
    """Exchange real RFC1928/1929 frames with the actual adapter CLI."""
    with socket.create_connection(("node-a", 18080), timeout=5) as connection:
        def receive(size):
            data = bytearray()
            while len(data) < size:
                block = connection.recv(size - len(data))
                require(bool(block), "Actual SOCKS5 server truncated its reply")
                data.extend(block)
            return bytes(data)
        connection.sendall(b"\x05" + bytes((len(methods),)) + methods)
        selection = receive(2)
        require(selection[0] == 5, "Actual endpoint did not speak SOCKS5")
        if selection[1] == 255:
            return ("method", 255)
        require(selection[1] == 2, "Adapter did not require RFC1929 authentication")
        username, password = (value.encode("utf-8") for value in credentials)
        connection.sendall(b"\x01" + bytes((len(username),)) + username + bytes((len(password),)) + password)
        authentication = receive(2)
        require(authentication[0] == 1, "Invalid RFC1929 authentication reply")
        if authentication[1] != 0:
            return ("auth", authentication[1])
        address = bytes((len(host),)) + host.encode("ascii") if address_type == 3 else (b"\x7f\x00\x00\x01" if address_type == 1 else b"\0" * 16)
        connection.sendall(bytes((5, command, 0, address_type)) + address + port.to_bytes(2, "big"))
        reply = receive(4)
        require(reply[0] == 5 and reply[2] == 0, "Invalid RFC1928 CONNECT reply")
        require(reply[3] in (1, 3, 4), "Invalid SOCKS5 bound-address type")
        length = 4 if reply[3] == 1 else (16 if reply[3] == 4 else receive(1)[0])
        receive(length + 2)
        if reply[1] != 0 or payload is None:
            return ("connect", reply[1])
        connection.sendall(payload)
        response = http.client.HTTPResponse(connection)
        response.begin()
        response.read()
        return ("http", response.status)


class Browser:
    def __init__(self, profile, signed):
        self.profile = profile
        self.signed = signed
        self.driver = None
        self.start()

    def configuration(self):
        options = Options()
        options.set_capability("acceptInsecureCerts", False)
        options.set_preference("xpinstall.signatures.required", self.signed)
        options.set_preference("extensions.webextensions.uuids", json.dumps({EXTENSION_ID: EXTENSION_UUID}))
        options.set_preference("network.trr.mode", 5)
        options.set_preference("network.proxy.no_proxies_on", "")
        options.set_preference("browser.shell.checkDefaultBrowser", False)
        options.set_preference("browser.startup.page", 0)
        options.set_preference("datareporting.policy.dataSubmissionEnabled", False)
        options.set_preference("toolkit.telemetry.enabled", False)
        options.set_preference("signon.rememberSignons", False)
        return options

    def start(self):
        options = self.configuration()
        options.binary_location = "/opt/firefox/firefox"
        # Exact profile, never Selenium's automatically cloned one.
        options.add_argument("-profile")
        options.add_argument(str(self.profile))
        options.add_argument("-headless")
        self.driver = webdriver.Firefox(options=options, service=Service("/app/tests/browser/traced-geckodriver.sh", service_args=["--allow-system-access"], log_output=str(ARTIFACTS / "geckodriver.log")))
        self.driver.set_page_load_timeout(12)
        self.driver.set_script_timeout(12)

    def restart(self, first_url=None):
        self.driver.quit()
        self.driver = None
        self.start()
        if first_url:
            self.driver.get(first_url)

    def close(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    def assert_no_admission_connections(self):
        import network_evidence
        self.close()  # Finish actual syscall recording before inspecting it.
        network_evidence.desktop(ARTIFACTS)

    def options(self):
        self.driver.get(OPTIONS_URL)
        WebDriverWait(self.driver, 10).until(lambda driver: driver.find_element(By.ID, "status").get_attribute("data-state") == "ready")

    def cards(self, domain):
        return self.driver.find_elements(By.CSS_SELECTOR, 'article[data-domain="' + domain + '"]')

    def action(self, domain, action):
        self.options()
        cards = self.cards(domain)
        require(len(cards) == 1, "Expected one stored connection for " + domain)
        cards[0].find_element(By.CSS_SELECTOR, '[data-action="' + action + '"]').click()
        if action == "delete":
            try:
                self.driver.switch_to.alert.accept()
            except WebDriverException:
                pass  # UI may use an inline confirmation instead of a modal.
            WebDriverWait(self.driver, 5).until(lambda _driver: not self.cards(domain))
        elif action == "toggle":
            WebDriverWait(self.driver, 5).until(lambda driver: driver.find_element(By.ID, "status").get_attribute("data-state") == "ready")

    def save(self, domain, host, user, password, port=18080, edit=False):
        if edit:
            self.action(domain, "edit")
        else:
            self.options()
        values = {"name": domain, "domain": domain, "proxy-host": host,
                  "proxy-port": str(port), "username": user, "password": password}
        for name, value in values.items():
            element = self.driver.find_element(By.ID, name)
            element.clear()
            element.send_keys(value)
        enabled = self.driver.find_element(By.ID, "enabled")
        if not enabled.is_selected():
            enabled.click()
        self.driver.find_element(By.ID, "save").click()
        WebDriverWait(self.driver, 5).until(lambda driver: driver.find_element(By.ID, "name").get_attribute("value") == "")
        require(len(self.cards(domain)) == 1, "Connection was not saved through actual options UI")

    def page(self, url, node, navigate=True):
        if navigate:
            self.driver.get(url)
        require(self.driver.current_url == url, "Original address was replaced: " + self.driver.current_url)
        evidence = json.loads(self.driver.find_element(By.ID, "evidence").text)
        require(evidence["node"] == node, "Incorrect node: " + repr(evidence))
        require(not evidence["authorization"] and not evidence["proxyAuthorization"], "Credentials leaked to origin")
        return evidence

    def denied(self, host, label):
        # Firefox retains the prior document when webRequest cancels navigation.
        # Clear it first so its DOM cannot masquerade as this probe's response.
        self.driver.get("about:blank")
        marker = "/denied-" + label + "-" + uuid.uuid4().hex
        # An HTTP error document is also a rejection, but a
        # working origin page must never be accepted merely because get() did
        # not throw. Evidence from every actual origin and the DNS trap decides.
        try:
            self.driver.get("http://" + host + marker)
        except (TimeoutException, WebDriverException):
            try:
                self.driver.execute_script("window.stop()")
            except WebDriverException:
                pass
        require(not self.driver.find_elements(By.ID, "evidence"), "Blocked request rendered an origin document")
        for origin in ORIGINS:
            require(not any(marker in row["path"] for row in records(origin)), "Blocked request reached " + origin)

    def denied_websocket(self):
        self.page("http://unrelated:8080/unmapped-ws-context", "unrelated")
        host = "ws-unmapped-" + uuid.uuid4().hex + ".portmap"
        result = self.driver.execute_async_script("""
            const [url, done] = arguments;
            const ws = new WebSocket(url);
            const timer = setTimeout(() => { ws.close(); done({timeout:true}); }, 8000);
            ws.onopen = () => { clearTimeout(timer); done({opened:true}); ws.close(); };
            ws.onerror = () => { clearTimeout(timer); done({blocked:true}); };
        """, "ws://" + host + "/unmapped-ws-probe")
        require(result.get("blocked") is True, "Unmapped WebSocket did not reject: " + repr(result))
        for origin in ORIGINS:
            require(not any(row["host"].split(":")[0] == host for row in records(origin)), "Unmapped WebSocket reached " + origin)

    def fetch(self, path, method="GET", body=None, headers=None):
        result = self.driver.execute_async_script("""
            const [path, method, body, headers, done] = arguments;
            fetch(path, {method, body, headers, cache:'no-store', signal:AbortSignal.timeout(8000)})
              .then(async response => done({status:response.status, data:await response.json()}))
              .catch(error => done({error:String(error)}));
        """, path, method, body, headers or {})
        require(result.get("status") == 200, "Real page fetch failed: " + repr(result))
        return result["data"]

    def websocket(self, scheme, host):
        result = self.driver.execute_async_script("""
            const [url, done] = arguments;
            const ws = new WebSocket(url);
            const timeout = setTimeout(() => { ws.close(); done({error:'timeout'}); }, 8000);
            ws.onopen = () => ws.send('portmap-real-websocket');
            ws.onerror = () => { clearTimeout(timeout); done({error:'websocket error'}); };
            ws.onmessage = event => { clearTimeout(timeout); done(JSON.parse(event.data)); ws.close(); };
        """, scheme + "://" + host + "/ws")
        require(result.get("message") == "portmap-real-websocket", "Actual WebSocket echo failed: " + repr(result))
        require(not result["authorization"] and not result["proxyAuthorization"], "WebSocket credentials leaked")
        return result


def run(args, proof, browser_factory):
    ARTIFACTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portmap-firefox-isolated-") as work:
        work = Path(work)
        unsigned = work / "development.xpi"
        subprocess.run(["python", "/app/tools/build_firefox_extension.py", "build", "--output", str(unsigned)], check=True)
        xpi = Path(args.xpi) if args.xpi else unsigned
        raw = xpi.read_bytes()
        proof["extension_sha256"] = hashlib.sha256(raw).hexdigest()
        shutil.copyfile(xpi, ARTIFACTS / "extension-under-test.xpi")
        with zipfile.ZipFile(xpi) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        proof["extension_version"] = manifest["version"]
        require(manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id") == EXTENSION_ID, "XPI has unexpected identity")
        profile = work / "profile"
        profile.mkdir()
        subprocess.run(["certutil", "-N", "--empty-password", "-d", "sql:" + str(profile)], check=True)
        subprocess.run(["certutil", "-A", "-n", "Portmap isolated fixture CA", "-t", "C,,", "-i", "/certs/ca.pem", "-d", "sql:" + str(profile)], check=True)
        shutil.copyfile("/app/browser-installed.txt", ARTIFACTS / "python-versions.txt")
        shutil.copyfile("/app/browser-system-packages.txt", ARTIFACTS / "system-versions.txt")
        browser = browser_factory(profile, args.require_signed)
        driver = browser.driver
        proof["browser_version"] = driver.capabilities["browserVersion"]
        proof["driver_version"] = driver.capabilities.get("moz:geckodriverVersion")
        require(driver.capabilities["browserName"] == "firefox", "Not a real Firefox session")
        def case(name, action, fatal=False):
            row = {"name": name, "status": "failed"}
            proof["cases"].append(row)
            try:
                action()
            except Exception:
                proof.setdefault("case_errors", {})[name] = traceback.format_exc()
                try:
                    if browser.driver:
                        browser.driver.save_screenshot(str(ARTIFACTS / (name + "-failed.png")))
                except WebDriverException:
                    pass
                if fatal:
                    raise
            else:
                row["status"] = "passed"
        try:
            def external_isolation():
                try:
                    connection = socket.create_connection(("1.1.1.1", 443), timeout=2)
                except OSError:
                    return
                connection.close()
                raise AssertionError("Fixture container has external network access")
            case("fixture-external-network-isolation", external_isolation, fatal=True)
            case("ordinary-browsing-before-install", lambda: browser.page("http://unrelated:8080/baseline", "unrelated"))
            if args.require_signed:
                def signature_enforcement():
                    try:
                        installed = driver.install_addon(str(unsigned), temporary=False)
                    except WebDriverException as error:
                        require("signed" in str(error).lower() or "signature" in str(error).lower(), "Unsigned control failed for a reason other than signature verification")
                    else:
                        driver.uninstall_addon(installed)
                        raise AssertionError("Browser accepted unsigned control with --require-signed")
                    proof["signature_required"] = True
                case("browser-rejects-unsigned-control", signature_enforcement, fatal=True)
            case("actual-xpi-installed", lambda: require(driver.install_addon(str(xpi), temporary=False) == EXTENSION_ID, "Actual extension install failed"), fatal=True)
            case("unconfigured-fails-closed", lambda: browser.denied("a.portmap", "unconfigured"))
            case("unconfigured-websocket-fails-closed", browser.denied_websocket)
            case("save-two-independent-connections", lambda: (browser.save("a.portmap", "node-a", "user-a", "pass-a"), browser.save("b.portmap", "node-b", "user-b:β", "pass-b-密")))
            require(browser.driver.save_screenshot(str(ARTIFACTS / "options-configured.png")), "Could not capture actual configured extension UI")
            for domain, node in (("a.portmap", "node-a"), ("b.portmap", "node-b"), ("app.a.portmap", "node-a"), ("app.b.portmap", "node-b")):
                def routing(domain=domain, node=node):
                    evidence = browser.page("http://" + domain + "/original/path?keep=one%20two", node)
                    require(evidence["host"] == domain and evidence["path"] == "/original/path?keep=one%20two", "Original Host/path not retained")
                    require(driver.execute_script("return window.fixtureAsset") == node, "Wrong static JS asset")
                    require(driver.execute_script("return getComputedStyle(document.querySelector('#asset-proof')).color") == "rgb(1, 2, 3)", "Static CSS not loaded")
                    echoed = browser.fetch("/echo?post=original", "POST", "original POST body π")
                    require(echoed["node"] == node and echoed["host"] == domain and echoed["path"] == "/echo?post=original" and echoed["body"] == "original POST body π" and echoed["method"] == "POST", "POST not preserved")
                    require(not echoed["authorization"] and not echoed["proxyAuthorization"], "POST leaked credentials")
                    origin_auth = browser.fetch("/echo?origin-auth=yes", headers={"Authorization": "Bearer fixture-origin-token"})
                    require(origin_auth["originAuthorizationCorrect"] and not origin_auth["proxyAuthorization"], "Origin Authorization was altered or proxy credentials leaked")
                    require(browser.websocket("ws", domain)["node"] == node, "WS wrong node")
                case("routing-assets-post-ws-" + domain, routing)
            case("trailing-dot-host-routing", lambda: browser.page("http://a.portmap./trailing-dot", "node-a"))
            def redirects():
                driver.get("http://a.portmap/redirect")
                require(driver.current_url == "http://a.portmap/landing?via=redirect", "Redirect changed origin")
                require(json.loads(driver.find_element(By.ID, "evidence").text)["node"] == "node-a", "Redirect went to wrong node")
            case("redirect-original-origin", redirects)
            for domain, node in (("a.portmap", "node-a"), ("b.portmap", "node-b")):
                def tls(domain=domain, node=node):
                    evidence = browser.page("https://" + domain + "/verified-tls?original=yes", node)
                    require(evidence["host"] == domain and evidence["path"] == "/verified-tls?original=yes", "TLS original Host/path lost")
                    require(browser.fetch("/echo", "POST", "verified TLS body")["body"] == "verified TLS body", "HTTPS POST failed")
                    require(browser.websocket("wss", domain)["node"] == node, "WSS wrong node")
                case("verified-https-wss-" + domain, tls)
            case("ordinary-browsing-with-extension", lambda: browser.page("http://unrelated:8080/unchanged", "unrelated"))
            def rejection():
                require(socks_status(methods=b"\x00") == ("method", 255), "Adapter accepted no-auth SOCKS5")
                require(socks_status(credentials=("user-a", "wrong")) == ("auth", 1), "Adapter accepted bad RFC1929 credentials")
                require(socks_status("b.portmap") == ("connect", 2), "Adapter accepted another node domain")
                require(socks_status("a.portmap.evil.test") == ("connect", 2), "Adapter suffix boundary bypass")
                require(socks_status("b.portmap", port=443) == ("connect", 2), "TLS destination domain bypass")
                require(socks_status(port=22) == ("connect", 2), "Adapter accepted arbitrary destination port")
                for command in (2, 3):
                    require(socks_status(command=command) == ("connect", 7), "Adapter accepted BIND or UDP command")
                for address_type in (1, 4):
                    require(socks_status(address_type=address_type) == ("connect", 8), "Adapter accepted client-resolved destination IP")
                require(socks_status(payload=b"GET /denied-inner-host HTTP/1.1\r\nHost: b.portmap\r\nConnection: close\r\n\r\n") == ("http", 403), "Authorized SOCKS domain escaped through foreign inner Host")
                for origin in ORIGINS:
                    require(not any("/denied-" in row["path"] for row in records(origin)), "Rejected request reached origin")
            case("adapter-socks5-auth-domain-command-address-rejection", rejection)
            case("unknown-portmap-fails-closed", lambda: browser.denied("unknown.portmap", "unknown"))
            def persistence():
                target = "http://a.portmap/restarted-first-request"
                # Android opens this FIRST request through its real native VIEW
                # intent; desktop navigates once after full process startup.
                browser.restart(first_url=target)
                browser.page(target, "node-a", navigate=False)
                browser.denied("unknown.portmap", "restart-unknown")
                browser.options()
                require(len(browser.cards("a.portmap")) == len(browser.cards("b.portmap")) == 1, "Connections not persisted")
                browser.page("http://b.portmap/restarted-second-node", "node-b")
            case("browser-restart-storage-and-initialization", persistence)
            def disable():
                browser.action("a.portmap", "toggle")
                browser.denied("a.portmap", "disabled")
                browser.page("http://b.portmap/other-node-while-disabled", "node-b")
                browser.restart()
                browser.denied("a.portmap", "disabled-after-restart")
                browser.action("a.portmap", "toggle")
                browser.page("http://a.portmap/re-enabled", "node-a")
            case("disable-reenable-and-disabled-persistence", disable)
            def disconnected():
                browser.save("a.portmap", "node-a", "user-a", "pass-a", port=18081, edit=True)
                browser.denied("a.portmap", "endpoint-disconnected")
                browser.page("http://b.portmap/other-node-while-disconnected", "node-b")
                browser.save("a.portmap", "node-a", "user-a", "pass-a", edit=True)
                browser.page("http://a.portmap/reconnected", "node-a")
            case("unreachable-endpoint-no-other-node-fallback", disconnected)
            def bad_credentials():
                browser.save("a.portmap", "node-a", "user-a", "wrong", edit=True)
                browser.denied("a.portmap", "browser-wrong-auth")
                browser.save("a.portmap", "node-a", "user-a", "pass-a", edit=True)
                browser.page("http://a.portmap/auth-restored", "node-a")
            case("browser-auth-rejection", bad_credentials)
            def remove():
                browser.action("a.portmap", "delete")
                browser.denied("a.portmap", "removed")
                browser.restart()
                browser.denied("a.portmap", "removed-after-restart")
                browser.options()
                require(not browser.cards("a.portmap") and len(browser.cards("b.portmap")) == 1, "Removal not persisted independently")
                browser.page("http://b.portmap/retained-after-remove", "node-b")
            case("remove-and-removal-persistence", remove)
            case("ordinary-browsing-after-lifecycle", lambda: browser.page("http://unrelated:8080/still-unchanged", "unrelated"))
            case("no-admission-sentinel-connection-attempts", browser.assert_no_admission_connections)
            def no_direct_fallback():
                for row in records("trap"):
                    host = row["host"].split(":")[0].rstrip(".").lower()
                    # Android's own unrelated connectivity probes are normal
                    # browsing traffic, not leakage of a .portmap claim.
                    require(host != "portmap" and not host.endswith(".portmap"), "Firefox sent .portmap HTTP traffic to the direct-network trap")
            case("no-direct-http-traffic-fallback", no_direct_fallback)
            require(all(row["status"] == "passed" for row in proof["cases"]), "Actual browser cases failed: " + ", ".join(row["name"] for row in proof["cases"] if row["status"] == "failed"))
        except Exception:
            try:
                if browser.driver:
                    browser.driver.save_screenshot(str(ARTIFACTS / "failure.png"))
            except WebDriverException:
                pass
            raise
        finally:
            browser.close()
            for origin in ORIGINS:
                (ARTIFACTS / (origin + "-requests.json")).write_text(json.dumps(records(origin), indent=2))
            queries = records("dns", "/queries")
            (ARTIFACTS / "dns-queries.json").write_text(json.dumps(queries, indent=2))
            original = [query for query in queries if query["name"] == "portmap" or query["name"].endswith(".portmap")]
            types = {}
            for query in original:
                key = str(query["type"])
                types[key] = types.get(key, 0) + 1
            # Incidental Firefox DNS metadata is explicitly accepted by the
            # user, not suppressed or confused with actual HTTP/WS fallback.
            proof["dns_diagnostics"] = {
                "classification": "disclosed-incidental-metadata",
                "total_query_count": len(queries),
                "original_portmap_query_count": len(original),
                "original_portmap_query_types": types,
                "original_portmap_names": sorted({query["name"] for query in original}),
                "warning": "Firefox can disclose original .portmap hostnames in incidental DNS queries even when actual traffic uses the selected authenticated node.",
                "queries_file": "dns-queries.json",
            }
            (ARTIFACTS / "dns-diagnostics.json").write_text(json.dumps(proof["dns_diagnostics"], indent=2))


def main(platform="desktop-firefox", browser_factory=Browser, prepare=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--development", action="store_true", help="Allow unsigned XPI only inside disposable ESR profile")
    mode.add_argument("--require-signed", action="store_true", help="Enforce Firefox signature verification and permanent installation")
    parser.add_argument("--xpi", help="Exact XPI bytes under test; omit only in explicit development mode")
    args = parser.parse_args()
    proof = {"status": "failed", "platform": platform, "extension_sha256": None,
             "extension_version": None, "browser_version": None,
             "source_commit": os.environ.get("BROWSER_SOURCE_COMMIT") or None,
             "signature_required": False, "cases": []}
    try:
        require(not args.require_signed or args.xpi, "--require-signed requires --xpi")
        if prepare:
            prepare(args, proof)
        run(args, proof, browser_factory)
        proof["status"] = "passed"
        return 0
    except Exception:
        proof["error"] = traceback.format_exc()
        return 1
    finally:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / ("android-proof.json" if platform == "android-firefox" else "desktop-proof.json")).write_text(json.dumps(proof, indent=2))
        (ARTIFACTS / "result.json").write_text(json.dumps(proof, indent=2))
        print(json.dumps(proof, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
