# Running mail-classify on a schedule

Every run is idempotent — Gmail is the queue and the sentinel label is the state
— so a missed, duplicated or failed run is harmless. The worst case is a delay.

`--apply` is required in all of these: without it the tool only prints a dry run.

## systemd (Linux)

User units, so no root:

```sh
mkdir -p ~/.config/systemd/user
cp contrib/systemd/mail-classify.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mail-classify.timer
```

Inspect:

```sh
systemctl --user list-timers mail-classify.timer   # when it next fires
systemctl --user status mail-classify.service      # last run, and any failure
journalctl --user -u mail-classify -n 50           # logs
```

If runs fail with `no value for OPENROUTER_API_KEY`, the key was being read from
a session that the service cannot see — use the `command` or `dotenv` secret
backend rather than `env`. A dry run under `systemd-run --user --pty` reproduces
the service environment for debugging:

```sh
systemd-run --user --pty ~/.local/bin/mail-classify --check
```

## launchd (macOS)

See `contrib/launchd/com.example.mail-classify.plist`. Edit the `REPLACE_ME`
paths and the `Label` namespace, then:

```sh
cp contrib/launchd/com.example.mail-classify.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.mail-classify.plist
```

launchd does not inherit your shell, so `PATH` in the plist must include wherever
`himalaya`, `ortie` and `mail-classify` live.

## cron

The simplest option, and fine on any Unix:

```cron
# every hour, at 17 past
17 * * * * /home/you/.local/bin/mail-classify --apply --quiet
```

cron mails you any output, so `--quiet` keeps it silent unless something warns.
Set `MAILTO=` to silence it entirely and rely on exit codes instead.

## How often?

Hourly is a sensible default: Jev bills per input token and an incremental run
only sees new mail, so the cost of an hourly run is near zero. Making it more
frequent buys latency, not accuracy.

The first run over a large backlog is the only expensive one. Bound it with
`--limit` if you would rather trickle through it:

```sh
mail-classify --apply --limit 200
```

## Exit codes

| code | meaning |
| --- | --- |
| `0` | clean; nothing to do, or everything applied |
| `1` | a thread failed (it stays unlabelled and is retried next run) |
| `2` | config, credential or connectivity problem; nothing was attempted |

`1` and `2` both mark the systemd unit as failed, which is what you want: the
run did not fully succeed.
