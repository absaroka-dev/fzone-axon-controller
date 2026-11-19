# Security policy

Please report suspected security issues privately through GitHub's security
advisory feature rather than opening a public issue.

The controller intentionally exposes only a local Unix-domain socket. Do not
bind an unlock or light-control API to an untrusted network. Remote access
should use a dedicated account and a forced-command allowlist. Never commit BLE
addresses, private keys, passwords, bearer tokens, or site-specific network
details.

