"""Cloudflare published edge networks, verified 2026-11-03.

Sources: https://www.cloudflare.com/ips-v4 and /ips-v6.
Only a loopback reverse proxy's overwritten X-Real-IP or the actual TCP peer
can establish trust. Arbitrary forwarded headers on direct requests cannot.
"""
from ipaddress import ip_address, ip_network

NETWORKS = tuple(map(ip_network, '''
173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22
141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20
197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13
104.24.0.0/14 172.64.0.0/13 131.0.72.0/22
2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32
2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32
'''.split()))
PSEUDO_IPV4 = ip_network('240.0.0.0/4')


def valid_ip(value):
    try:
        return ip_address(value or '')
    except ValueError:
        return None


def resolve_client(peer, forwarded=None, cf_ip=None, cf_ipv6=None):
    transport = valid_ip(peer)
    if transport is None:
        return peer[:100], peer[:100], 'unknown'
    if transport.is_loopback:
        transport = valid_ip(forwarded) or transport
    edge = str(transport)
    if any(transport in network for network in NETWORKS):
        client = valid_ip(cf_ip)
        if client is None:
            return edge, edge, 'cloudflare_missing'
        ipv6 = valid_ip(cf_ipv6)
        if client.version == 4 and client in PSEUDO_IPV4 and ipv6 and ipv6.version == 6:
            client = ipv6
        return str(client), edge, 'cloudflare'
    return edge, edge, 'direct'
