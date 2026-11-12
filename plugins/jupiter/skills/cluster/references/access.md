# Access to JUPITER

Sources: https://apps.fz-juelich.de/jsc/hps/jupiter/access.html and JSC's JuDoor
documentation, read 2026-11-12, plus what was seen on the system in November 2026.

## Contents

- What a login needs
- The key and its `from=` clause
- TOTP
- One shared connection
- What a failure means
- Without a TOTP code each time

## What a login needs

1. An account in JuDoor (https://judoor.fz-juelich.de), membership of a compute
   project, and the usage agreement for JUPITER accepted there.
2. An SSH key uploaded in JuDoor for JUPITER ("Manage SSH-keys" beside the system's
   name). Keys are per system: the data gateway JUDAC needs its own upload.
3. A TOTP app registered in JuDoor.

Password logins do not exist. JSC requires the private key to have a passphrase and
forbids storing private keys on its systems; agent forwarding is off on the server.

## The key and its `from=` clause

Ed25519 is the type JSC recommends: `ssh-keygen -a 100 -t ed25519 -f ~/.ssh/<name>`.
The user creates the key and types the passphrase themselves.

A key is uploaded with the addresses it may be used from in front of it:

```
from="192.0.2.42" ssh-ed25519 AAAAC3N...
from="2001:db8::2a,192.51.100.0/24,*.fz-juelich.de" ssh-ed25519 AAAAC3N...
```

- A comma-separated list of addresses, CIDR ranges, hostnames and wildcard hostnames.
- No `*` in the last position (`192.51.100.*` is refused); ranges no wider than `/8`;
  a wildcard must keep the provider's whole name (`*.example.net`, not `*.net`).
- A change takes about 15 minutes to reach the login nodes.
- JUPITER answers over IPv6 too, and ssh prefers it. A `from=` clause with IPv4
  addresses then needs `AddressFamily inet` in the host's ssh config.

When the user changes network (home, office, phone), the key is refused until the new
address is in the clause. `jup doctor --probe` prints the machine's public address (it
asks api.ipify.org); `whois <address>` gives the provider's range, which suits a phone
whose address moves. Only the user can change the clause: it needs their JuDoor login
and a TOTP code.

## TOTP

After the key, ssh asks `JSC TOTP Verification code:` for six digits. A code is valid
for 30 seconds and works once, so two new connections within the same half minute
cannot both succeed. The agent can neither read nor enter the code: every new
connection is the user's to open.

## One shared connection

JSC's access page describes routing all ssh sessions through one authenticated
connection and names automated workflows as a use for it. The host entry `jup`
expects:

```
Host hpc-jupiter
  HostName login.jupiter.fz-juelich.de
  User <login>
  IdentityFile ~/.ssh/<key>
  IdentitiesOnly yes
  AddressFamily inet
  ControlMaster auto
  ControlPath ~/.ssh/cm-%C
  ControlPersist 12h
  ServerAliveInterval 60
```

- The user opens it once with `ssh hpc-jupiter true` (in Claude Code: `! ssh
  hpc-jupiter true`); it then serves every `jup` command. `ControlPersist` counts from
  the last command, so a connection in use stays open; it closes when the machine
  sleeps for long or restarts, or the network changes.
- One connection carries a limited number of sessions at once (OpenSSH's default is
  10; JUPITER's value is not published). Run `jup` commands one after another.
- `ssh -O check hpc-jupiter` says whether it is open without touching the network.
- All sessions land on the same login node, picked at random at the first login.
- A session keeps the Unix groups it had at login. After the user is added to a group
  (a project, `container`), commands see it only through a new connection:
  `ssh -O exit hpc-jupiter`, then log in again. Jobs get the new groups at once.

## What a failure means

| What is seen | Cause | What to do |
|---|---|---|
| `jup: no SSH connection ... is open`, exit code 66 | the shared connection closed or never opened | ask the user to run `! ssh <host> true`; do nothing else meanwhile |
| the user's own `ssh` answers `Permission denied (publickey)` | their address is outside the key's `from=` clause, the key is not uploaded for JUPITER, or it is not loaded (`ssh-add`) | `jup doctor --probe`, once, prints the public address; they switch network or edit the clause in JuDoor |
| `jup: ssh to ... failed (...)`, exit code 68 | no route: offline, VPN, a login node down, or the connection dropped mid-command | check the network; https://status.jsc.fz-juelich.de/ shows outages |
| `jup: ... gave no answer within 120 s`, exit code 68 | the shared connection hangs | `ssh -O exit <host>`, then the user logs in again |
| `jup: no compute account is set`, exit code 65 | settings | `jup config set account <project>` |

Never answer a failure by trying again in a loop: JSC's firewall disables an address
that makes "too many accesses within a short amount of time", and the threshold is not
published. `jup` itself opens no connection; only `jup doctor --probe` makes a login
attempt, one per run.

## Without a TOTP code each time

All of these are the user's decisions, made in JuDoor or with JSC:

- A FIDO hardware key (for example a YubiKey) as the SSH key: the TOTP step is skipped.
- EuroHPC SSH certificates: skipped too, for EuroHPC users.
- UNICORE, JSC's REST interface (`https://unicore.fz-juelich.de/JUPITER/rest/core`,
  client `pyunicore`), accepts tokens and can submit jobs. The endpoint answers, but
  this plugin has never submitted through it; treat it as untested.
- Jacamar CI runners with the tags `jupiter, jacamar, compute, slurm`, only for
  projects on JSC's own GitLab.
