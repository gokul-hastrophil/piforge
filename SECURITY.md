# Security Policy

## Threat model

PiForge is a local system tool, not a network service — but it's a
security-sensitive one:

- The server must run as **root** to write raw block devices (`dd` to
  `/dev/sdX`) and mount/unmount partitions. It binds to `127.0.0.1` only
  and is never meant to be exposed to a network.
- It handles real secrets: Wi-Fi passwords, account passwords, SSH private
  key material (well, public keys — but treat the whole config path as
  sensitive), and static network configuration.
- Its device-selection logic is the main safety boundary: it must never
  offer a system disk, mounted filesystem, or non-removable device as a
  flashable target. A bug there is a data-loss bug, not just a
  correctness bug.
- It writes and executes a generated first-boot script (`firstrun.sh`) on
  every card — that generation path (`firstrun_gen.py`) is a shell
  injection surface if config values (hostname, SSID, etc.) aren't
  handled carefully.

If you're auditing this project, those are the four places to look hardest
at: device-listing filters in `server.py`'s `list_devices()`, the
capacity-check logic, `firstrun_gen.py`'s template substitution, and the
`pkexec`/`sudo` privilege boundary in `packaging/piforge` and `server.py`.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Use GitHub's private vulnerability reporting for this repository:
[Report a vulnerability](https://github.com/gokul-hastrophil/piforge/security/advisories/new)
(Security tab → "Report a vulnerability"). This opens a private
conversation with the maintainer only, and lets us coordinate a fix and
disclosure timeline before anything is public.

Include, if possible:
- What component is affected (device detection, firstrun generation,
  privilege escalation, the web UI's own trust boundary, packaging)
- Steps to reproduce, or a minimal example
- What you'd expect to happen vs. what actually happens
- Whether it requires local access, or is reachable some other way

## Supported versions

This project is young (single maintainer, pre-1.x releases). Security
fixes land on `main` and the latest tagged release; there's no long-term
support branch yet.

## Scope

Out of scope: issues that require the attacker to already have root, or
physical access to a machine with a card writer plugged in and PiForge
running — at that point the OS's own privilege model is what's protecting
you, not this app. In scope: anything that lets an unprivileged local
user, or a maliciously crafted config/profile file, escalate privileges,
write to a device it shouldn't, or exfiltrate secrets it shouldn't have
access to.
