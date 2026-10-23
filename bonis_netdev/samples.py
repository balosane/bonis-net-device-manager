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

# --- NTP / syslog fixtures: read-only captures of 2026-10-23 (addresses as recorded; `print detail` forms are derived)
NTP_PRINT_6 = """\
             enabled: yes
         primary-ntp: 192.168.6.1
       secondary-ntp: 0.0.0.0
    server-dns-names: 
                mode: unicast
       poll-interval: 15m
       active-server: 192.168.6.1
"""
NTP_DETAIL_6 = ("enabled=yes mode=unicast primary-ntp=192.168.6.1 secondary-ntp=0.0.0.0 server-dns-names=\"\" "
                "poll-interval=15m active-server=192.168.6.1\n")
NTP_PRINT_7 = """\
         enabled: yes
            mode: unicast
         servers: 192.168.6.1
                  195.113.144.201
             vrf: main
          status: synchronized
"""
NTP_DETAIL_7 = "enabled=yes mode=unicast servers=192.168.6.1,195.113.144.201 vrf=main status=synchronized\n"
RESOURCE_7 = "                   uptime: 3w2d\n                  version: 7.18.2 (stable)\n"
LOG_ACTIONS = """\
 0 * name="memory" target=memory memory-lines=1000 memory-stop-on-full=no
 1 * name="disk" target=disk disk-file-name="log" disk-lines-per-file=1000 disk-file-count=2 disk-stop-on-full=no
 2 * name="echo" target=echo remember=yes
 3 * name="remote" target=remote remote=192.168.6.1 remote-port=514 src-address=0.0.0.0 bsd-syslog=no syslog-facility=daemon syslog-severity=auto
"""
LOG_ACTIONS_NETDEV = LOG_ACTIONS + (
    ' 4   name="netdev-remote" target=remote remote=10.9.9.9 remote-port=1514 src-address=0.0.0.0 bsd-syslog=no '
    'syslog-facility=daemon syslog-severity=auto\n')
# airOS 5.6.11 (XW) excerpt of /tmp/system.cfg plus the kind of secret lines a real file holds
SYSTEM_CFG_XW = """\
ntpclient.1.server=tik.skamanet.cz
ntpclient.1.status=enabled
ntpclient.status=enabled
syslog.remote.ip=192.168.6.1
syslog.remote.port=514
syslog.remote.status=enabled
syslog.remote.tcp.status=disabled
syslog.status=enabled
system.timezone=GMT+12
aaa.1.wpa.psk=SuperSecretPsk
users.1.password=$1$hash
"""


def ros_ntp_transport(ntp_text: str, host="10.0.0.23", resource=RESOURCE) -> FakeTransport:
    return FakeTransport(host, {"/system resource print": resource, "/system ntp client print": ntp_text})


def ros_syslog_transport(actions_text: str, host="10.0.0.23", resource=RESOURCE_7) -> FakeTransport:
    return FakeTransport(host, {"/system resource print": resource,
                                "/system logging action print detail without-paging": actions_text})


def airos_cfg_transport(cfg_text: str, host="192.168.1.20") -> FakeTransport:
    return FakeTransport(host, {"cat /tmp/system.cfg": cfg_text})


def ros_transport(host="10.0.0.23") -> FakeTransport:
    return FakeTransport(host, {
        "/system resource print": RESOURCE,
        "/ip dhcp-server network print detail without-paging": NETWORKS,
        "/ip dhcp-server print detail without-paging": SERVERS,
        "/ip dns print": DNS_PRINT,
    })


def airos_transport(host="192.168.1.20") -> FakeTransport:
    return FakeTransport(host, {"cat /tmp/system.cfg": SYSTEM_CFG})
