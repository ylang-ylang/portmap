"""Exercise name-derived addresses through actual Firefox form input and routing."""
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from runner import ARTIFACTS, require


def exercise(browser, case):
    def fresh_form():
        browser.options()
        # Each scenario starts with an actual reload, not an assumption that
        # opening the native menu clears the current editor.
        browser.driver.refresh()
        WebDriverWait(browser.driver, 10).until(lambda driver:
            driver.find_element(By.ID, "status").get_attribute("data-state") == "ready")

    def value(field):
        return browser.driver.find_element(By.ID, field).get_attribute("value")

    def enter(field, text):
        browser.enter(field, text)

    def addresses(domain, host):
        require(value("domain") == domain and value("host") == host,
                "Name-derived addresses did not match expected endpoints")

    def defaults():
        fresh_form()
        require(browser.driver.find_element(By.ID, "name").is_displayed(), "Name form is hidden")
        enter("name", "Voxmani")
        addresses("voxmani.portmap", "voxmani")
        enter("name", "OTHER.portmap")
        addresses("other.portmap", "other")
        browser.driver.find_element(By.ID, "connection-form").screenshot(str(ARTIFACTS / "name-autofill-form.png"))
        for label in ("Display only", "portmap", "节点"):
            enter("name", label)
            addresses("", "")
        enter("name", "x")
        browser.driver.find_element(By.ID, "name").send_keys(Keys.BACKSPACE)
        addresses("", "")
    case("name-autofill-normalization-and-invalid-labels", defaults, fatal=True)

    def overrides():
        fresh_form()
        enter("name", "first")
        enter("domain", "custom.portmap")
        enter("name", "second")
        addresses("custom.portmap", "second")
        enter("host", "100.64.0.10")
        enter("http-port", "8123")
        enter("https-port", "8443")
        enter("name", "third")
        addresses("custom.portmap", "100.64.0.10")
        require(value("http-port") == "8123" and value("https-port") == "8443", "Name changed explicit ingress ports")
    case("name-autofill-preserves-independent-manual-overrides", overrides, fatal=True)

    def save_from_name():
        fresh_form()
        enter("name", "a")
        addresses("a.portmap", "a")
        for field, text in (("http-port", "8080"), ("https-port", "8443")):
            enter(field, text)
        browser.driver.find_element(By.ID, "save").click()
        WebDriverWait(browser.driver, 10).until(lambda _driver: value("name") == "" and len(browser.cards("a.portmap")) == 1)
        enter("name", "fresh")
        addresses("fresh.portmap", "fresh")
        browser.page("http://a.portmap/name-only-addresses", "node-a")
    case("name-autofill-save-and-actual-node-route", save_from_name, fatal=True)

    def edit_and_cancel():
        browser.action("a.portmap", "toggle")
        browser.action("a.portmap", "edit")
        identity = value("connection-id")
        enter("name", "Renamed display label")
        addresses("a.portmap", "a")
        require(not browser.driver.find_element(By.ID, "enabled").is_selected(), "Edit enabled a disabled connection")
        browser.driver.find_element(By.ID, "save").click()
        WebDriverWait(browser.driver, 10).until(lambda _driver: value("name") == "")
        browser.action("a.portmap", "edit")
        require(value("connection-id") == identity and value("name") == "Renamed display label", "Rename replaced identity or was not saved")
        addresses("a.portmap", "a")
        browser.driver.find_element(By.ID, "cancel-edit").click()
        enter("name", "new-node")
        addresses("new-node.portmap", "new-node")
        browser.denied("a.portmap", "rename-kept-disabled")
        browser.action("a.portmap", "toggle")
        browser.page("http://a.portmap/renamed-same-endpoint", "node-a")
        browser.action("a.portmap", "delete")
    case("name-autofill-edit-preserves-saved-endpoints-and-state", edit_and_cancel, fatal=True)
