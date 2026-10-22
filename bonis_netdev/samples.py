"""Canned device output, used by `netdev demo`, `netdev selftest` and the tests.
Shaped like what real RouterOS 6 / airOS print."""
from .transport import FakeTransport

NETWORKS = """\
 0 address=192.168.80.0/26 gateway=192.168.80.1 dns-server=192.168.6.1,8.8.8.8 comment=AP net
 1 address=192.168.88.0/24 gateway=192.168.88.1 dns-server=
 2 address=212.119.38.192/29 gateway=212.119.38.193 dns-server=192.168.238.119,192.168.6.1
 3 address=10.15.5.208/29 gateway=10.15.5.209 dns-server=192.168.6.1
"""
SERVERS = """\
 0   name=dhcp1 interface=bridge1 address-pool=dhcp_pool0
 1 I name=dhcp2 interface=bridge2 address-pool=dhcp_pool1
"""
DNS_PRINT = """\
                      servers: 192.168.6.1,8.8.8.8
              allow-remote-requests: no
"""
RESOURCE = "                   uptime: 2w1d\n                  version: 6.47.9 (long-term)\n"
SYSTEM_CFG = """\
resolv.nameserver.1.ip=192.168.6.1
resolv.nameserver.2.ip=
aaa.1.wpa.psk=SuperSecretPsk
users.1.password=$1$hash
"""


def ros_transport(host="10.0.0.23") -> FakeTransport:
    return FakeTransport(host, {
        "/system resource print": RESOURCE,
        "/ip dhcp-server network print detail without-paging": NETWORKS,
        "/ip dhcp-server print detail without-paging": SERVERS,
        "/ip dns print": DNS_PRINT,
    })


def airos_transport(host="192.168.1.20") -> FakeTransport:
    return FakeTransport(host, {"cat /tmp/system.cfg": SYSTEM_CFG})
