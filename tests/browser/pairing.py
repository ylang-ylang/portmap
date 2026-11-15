"""Real options-UI imports of codes emitted by the running node CLI fixtures."""
import base64
import json

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from runner import ARTIFACTS, require


def exercise(browser, case):
    codes = {}
    payloads = {}
    for node in ("node-a", "node-b"):
        path = ARTIFACTS / "pairing" / (node + ".code")
        WebDriverWait(browser.driver, 10).until(lambda _driver: path.exists() and path.stat().st_size > 0)
        code = path.read_text().strip()
        require(code.startswith("portmap-connect-v1:"), "Node CLI did not emit a supported connection code")
        body = code.split(":", 1)[1]
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        require(payload["proxyHost"] == node and payload["proxyPort"] == 18080, "Code advertised the wrong live endpoint")
        codes[node], payloads[node] = code, payload

    def preview(node):
        browser.preview_import(codes[node])
        payload = payloads[node]
        for field in ("name", "domain"):
            require(browser.driver.find_element(By.ID, "import-" + field).text == payload[field], "Preview did not show the actual node " + field)
        visible = browser.driver.find_element(By.TAG_NAME, "body").text
        require(payload["password"] not in visible and codes[node] not in visible, "Preview exposed credentials")
        require(browser.driver.find_element(By.ID, "connection-code").get_attribute("type") == "password", "Code input was not masked")

    def cancel():
        preview("node-a")
        require(not browser.cards("a.portmap"), "Preview saved without confirmation")
        browser.driver.find_element(By.ID, "cancel-import").click()
        require(browser.driver.find_element(By.ID, "connection-code").get_attribute("value") == "", "Cancel retained the pasted secret")
        require(not browser.driver.find_element(By.ID, "import-preview").is_displayed(), "Cancel retained the preview")
        browser.denied("a.portmap", "cancelled-code")
    case("pairing-cancel-does-not-create-connection", cancel, fatal=True)

    for node, domain in (("node-a", "a.portmap"), ("node-b", "b.portmap")):
        def imported(node=node, domain=domain):
            preview(node)
            require(not browser.cards(domain), "Preview created a connection before consent")
            browser.driver.execute_script("arguments[0].scrollIntoView({block: 'center'})", browser.driver.find_element(By.ID, "import-preview"))
            require(browser.driver.save_screenshot(str(ARTIFACTS / ("pairing-preview-" + node + ".png"))), "Preview screenshot failed")
            browser.confirm_import(domain)
            browser.page("http://app." + domain + "/imported-code", node)
        case("pairing-import-and-route-" + node, imported, fatal=True)

    def replace_disabled():
        browser.options()
        identity = browser.cards("a.portmap")[0].get_attribute("data-connection-id")
        require(bool(identity), "Connection card has no identity")
        browser.action("a.portmap", "toggle")
        preview("node-a")
        require(browser.driver.find_element(By.ID, "import-replacement").is_displayed(), "Replacement was not disclosed before confirmation")
        browser.confirm_import("a.portmap")
        require(browser.cards("a.portmap")[0].get_attribute("data-connection-id") == identity, "Reimport replaced the local connection identity")
        require(len(browser.cards("b.portmap")) == 1, "Reimport changed another connection")
        browser.denied("a.portmap", "reimport-kept-disabled")
        browser.page("http://b.portmap/independent-after-reimport", "node-b")
        browser.action("a.portmap", "toggle")
        browser.page("http://a.portmap/reimport-reenabled", "node-a")
    case("pairing-reimport-preserves-identity-and-disabled-state", replace_disabled, fatal=True)

    def rejected(code):
        browser.options()
        browser.driver.find_element(By.ID, "connection-code").send_keys(code)
        browser.driver.find_element(By.ID, "preview-import").click()
        WebDriverWait(browser.driver, 10).until(lambda driver: driver.find_element(By.ID, "error").is_displayed())
        require(not browser.driver.find_element(By.ID, "import-preview").is_displayed(), "Invalid code obtained a confirmable preview")
        require(len(browser.cards("a.portmap")) == len(browser.cards("b.portmap")) == 1, "Rejected import mutated connections")
        require(code not in browser.driver.find_element(By.ID, "error").text, "Import error echoed the secret code")
        browser.page("http://a.portmap/unchanged-after-rejection", "node-a")
        browser.page("http://b.portmap/unchanged-after-rejection", "node-b")
    case("pairing-malformed-code-is-atomic", lambda: rejected("portmap-connect-v1:%%%"))

    # Only this NEGATIVE input is synthesized: an adversarial overlapping claim.
    overlapping = dict(payloads["node-a"], domain="app.a.portmap")
    overlap_code = "portmap-connect-v1:" + base64.urlsafe_b64encode(json.dumps(overlapping).encode()).decode().rstrip("=")
    case("pairing-overlap-is-atomic", lambda: rejected(overlap_code))
