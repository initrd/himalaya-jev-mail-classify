# mail-classify

Label and prioritise Gmail with a language model, from the command line.

Reads mail through [himalaya](https://github.com/pimalaya/himalaya)'s Gmail
backend, asks [Jev](https://docs.typesafe.ai) (TypeSafe's System One model, served
via [OpenRouter](https://openrouter.ai)) typed questions about each **thread**,
and applies the answers as Gmail labels and colours.

```
$ mail-classify --limit 4
4 thread(s) / 4 message(s) via 'in:inbox -label:a' -> jev-1.13 [DRY RUN]

  [1/4] Updated invitation: Check-in @ Weekly from 10am  -> Calendar   P1   f=1.00 p=0.99
  [2/4] [GitHub] Your fine-grained personal access token -> Dev        P0   f=1.00 p=0.89+Security+NeedsAction
  [3/4] Your GitHub receipt for October 2026             -> Finance    P2   f=0.99 p=0.94
  [4/4] Amazon Web Services - Email Address Verification -> Cloud      P0   f=0.97 p=0.91

4 classified, 4 distinct label set(s), 0 failed, $0.0003

[dry-run] nothing written. Re-run with --apply.
```

Note the same `github.com` sender landing in `Dev` and `Finance`: filing follows
what a message *is about*, not who sent it.

## Requirements

- **[himalaya](https://github.com/pimalaya/himalaya)** ≥ 2.0, configured with a
  **Gmail backend** (`gmail.auth.token.command`). Every id in this pipeline is a
  Gmail API id, so an IMAP-only setup will not work.
- A way to get an OAuth token for that account. [ortie](https://github.com/pimalaya/ortie)
  is what the examples use; any command that prints a token on stdout will do.
- An **[OpenRouter](https://openrouter.ai/settings/keys) API key**, which is what
  pays for the model.
- **[uv](https://docs.astral.sh/uv/)** or **[pipx](https://pipx.pypa.io/)** to
  install it. `pip install .` works too.

## Install

```sh
uv tool install .        # or: pipx install .
```

That puts `mail-classify` on your PATH and resolves its one dependency
(`typesafe-sdk`) into an isolated environment you never have to manage.

To run it from a checkout without installing:

```sh
uv run mail_classify.py
```

## Configure

```sh
mkdir -p ~/.config/mail-classify
cp config.example.toml ~/.config/mail-classify/config.toml
$EDITOR ~/.config/mail-classify/config.toml
```

Then point `[secret]` at wherever your OpenRouter key lives. Three backends:

```toml
[secret]
backend = "dotenv"                                   # read from a file
path = "~/.config/mail-classify/.env"                # chmod 600 it

# backend = "env"                                    # read $OPENROUTER_API_KEY
# backend = "command"                                # run anything, take stdout
# command = ["secret-tool", "lookup", "service", "openrouter", "account", "mail-classify"]
# command = ["pass", "show", "openrouter"]
# command = ["security", "find-generic-password", "-s", "openrouter", "-w"]   # macOS
```

`command` is the one that keeps the key off disk, and the one to use for machine
accounts and scheduled runs. Store it once with, for example:

```sh
printf '%s' 'sk-or-...' \
  | secret-tool store --label='openrouter' service openrouter account mail-classify
```

Verify before you trust it — this touches no mail and calls no model:

```sh
mail-classify --check
```

```
  [ok  ] secret backend - dotenv ~/.config/mail-classify/.env
  [ok  ] secret resolves
  [ok  ] himalaya gmail backend - 35 labels visible
  [ok  ] vocabulary labels exist
  [ok  ] gmail api token (for label colours)
  [ok  ] worklist query - 0 message(s) match 'in:inbox -label:a'
```

## Use

```sh
mail-classify                     # dry run the inbox (the safe default)
mail-classify --limit 25          # dry run 25 threads
mail-classify --apply             # label them for real
mail-classify --query 'label:foo' # work on some other slice of mail
mail-classify --log-level debug   # why is it doing that
```

A dry run prints every decision, the resulting label sets and the exact cost.
Nothing is written without `--apply`.

## How it works

```
worklist  ->  fetch  ->  classify  ->  group  ->  apply

worklist   unclassified messages matching a query, grouped into threads
fetch      himalaya gmail messages get --format raw -> a minimal state
classify   one Jev request per thread: a filing Choice, a priority Choice, gates
group      bucket threads by the exact label set they need
apply      himalaya gmail threads modify, one call per thread
```

**There is no state file.** Gmail is both the queue and the database: a sentinel
label is applied to everything processed, so the worklist query
(`in:inbox -label:<sentinel>`) is idempotent, a crash mid-run only leaves
unprocessed threads unmarked, and re-running is always safe. Run it hourly, run
it twice, run it after a two-week outage — the result is the same.

**The thread is the unit of classification.** One request per conversation, with
the answer applied to every message in it, so a thread can never show two filing
labels. This is also cheaper than per-message: a 70-message inbox is 51 threads.

## The vocabulary *is* the config

`[filing]` and `[priority]` are not just label lists — they are Jev's `criteria`
map. Each key is a Gmail label, each value the rubric that defines it. The model
can only return a key listed there, and anything it invents is rejected rather
than created, so editing these tables is how you improve accuracy.

Three rules, each learned from a real misclassification:

**Boundary cases belong in the criteria.** Jev reads instructions literally and
answers the question you wrote, not the one you meant. If you find yourself
explaining what you really meant after seeing a wrong answer, that explanation is
the missing half of the rubric.

**Orthogonal properties go in `[gates]`, not `[filing]`.** A thread can be from
GitHub *and* be a security event. As a filing option, `Security` competed with
the categories and won on anything that merely mentioned a credential — dragging
filing confidence from 0.91 down to 0.70 and misrouting a quarter of the inbox.
Gates apply *in addition* to the filing label.

**Never add a second label that answers a question a gate already answers.** The
same mistake, differently dressed. `Alerts` once included "security scanning
output" and became a magnet for anything with "alert" in the subject, so two
near-identical security emails filed differently. If you reach for a filing label
for a property, add a gate instead.

## Label colours

Applied on `--apply`, alongside label creation. Each value is a background hex;
text colour is chosen by luminance so light backgrounds stay readable:

```toml
[colors]
Dev      = "#16a765"
Personal = { bg = "#fad165", fg = "#000000" }
```

Gmail only accepts hex values from its own label palette; anything else is
rejected by the API and reported per label. This is the one part of the pipeline
that does not go through himalaya, because `gmail labels update` sets the name
and nothing else — so it needs `[gmail.token]`:

```toml
[gmail.token]
command = ["ortie", "token", "show", "-a", "google"]
```

Omit that block and the run simply skips colouring.

## Cost

Jev bills per **input** token; output tokens are free, and the `state` is
ingested once per request with every question evaluated against it in parallel.
So extra questions cost nothing and batching threads saves nothing — one request
per thread is both cheapest and most accurate, since accuracy falls as a state
fills with unrelated detail.

Cost is therefore driven entirely by state size, which is why `body_chars`
truncates and reply chains are stripped. Expect roughly **$0.00007 per thread**,
or about **$0.50 per 7,000 threads**.

## Scheduling

Every run is idempotent, so a missed or duplicated run is harmless. User-level
systemd units, a launchd agent and a cron example are in
**[contrib/](contrib/README.md)**.

## Tests

```sh
python3 -m unittest discover -s tests      # offline: no network, no API key
```

The live vocabulary check runs the same edge cases through the real model and
asserts the boundaries above still hold. It skips cleanly without credentials and
costs a fraction of a cent:

```sh
uv run python3 -m unittest tests.test_vocabulary -v
```

## Troubleshooting

| symptom | cause |
| --- | --- |
| `no value for OPENROUTER_API_KEY` under systemd/cron | the key came from a shell session the service cannot see; use the `command` or `dotenv` backend |
| `himalaya not found on PATH` | launchd and systemd do not inherit your shell; set `PATH`, or use an absolute path |
| `Gmail does not support the shared envelope search` | the account has an IMAP backend; this tool needs the Gmail backend |
| `colour X FAILED: HTTP 400` | the hex is not in Gmail's label palette |
| `rejected unknown label(s)` | the model returned something outside your vocabulary; the thread went to review |

`--log-level debug` prints every himalaya invocation and the secret resolution
path. `--log-file PATH` appends logs instead of writing them to stderr.

## Notes

- `--apply` creates labels directly in Gmail. Run a dry run first.
- A label the model invents is rejected and the thread goes to review; the tool
  never creates a label from model output.
- One failing thread does not abort the run. It is reported, left unmarked, and
  retried next time.
- Re-running after a rubric change is safe: the apply is an idempotent *set*, so
  labels are replaced rather than accumulated.

## Licence

MIT — see [LICENSE](LICENSE).
