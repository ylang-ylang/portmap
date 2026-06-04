from pathlib import Path


def test_gateway_corefile_forwards_non_portmap_dns_queries() -> None:
    corefile = Path("gateway/Corefile").read_text(encoding="utf-8")

    assert "{$PORTMAP_DNS_DOMAIN}:53" in corefile
    assert ".:53" in corefile
    assert "forward . {$PORTMAP_DNS_FORWARD}" in corefile
