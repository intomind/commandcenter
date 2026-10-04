# Contributing

Issues, questions, and pull requests are welcome.

## Before your first pull request

Sign the contributor license agreement. It is short, it is in `CLA.md`,
and it is signed once rather than per contribution. Comment on your pull
request with:

```
I have read CLA.md and I agree to it.
```

from the account that owns the contribution. A maintainer records the
agreement against your account and later pull requests need nothing
further.

You keep the copyright in your work. The agreement gives IntoMind the
right to use your contribution under both the Affero license and the
commercial license, which is what makes it possible to offer either one.
Without it a contribution could only ever go out under one of them, and
the project would have to refuse the contribution or drop the commercial
license.

If you are contributing for an employer, get whoever owns your work to
agree as well, and say so on the pull request.

## What makes a change easy to accept

- One change per pull request, with the reason in the message rather than
  in a comment on the diff.
- A test that fails before your change and passes after it. A change to
  behavior without a test that shows the behavior is hard to review and
  hard to keep.
- The house style: American English, plain sentences, no abbreviation a
  reader has to look up.
- Nothing that names hardware this library does not support. The device
  is the authority on what the device is, and a table of specific
  hardware here is a defect.

## What gets refused

- Code you did not write, or code whose license is not compatible.
- A dependency added for something small. Every dependency is a thing
  that has to keep working for as long as this library does.
- Anything that makes a recording less honest: a silent gap, an invented
  sample, a statistic that was not measured.

## Security

Report anything security sensitive to contact@intomind.com rather than in
a public issue.

## How this code is written

Five things are true of this application, and a change that makes any of
them false is a change to refuse.

1. **A person is wearing the electrodes.** Nothing starts a capture or
   changes a device setting on its own; a person asks for it.
2. **The data stays on this machine.** No remotes, no uploads. The server
   answers only the machine it runs on, and `same_machine_only` is the whole
   of that defense: do not weaken it, and keep its test passing.
3. **The device answers for itself.** The channel count, the rates, the
   controls, whether there is contact detection, whether there is a model,
   what processing it runs: all of it comes from `device.profile()` and
   `device.can()`, over the protocol. If this code ever branches on a device
   name, or holds a list of gains, rates, or modes, that is the bug.
4. **A capture states how it was produced, and says unknown when it does not
   know.** `context.py` declares where the recordings live and which source
   tree recorded them, and declares a site only if this installation has one.
5. **The library owns the formats.** Exports go through `intomind.export` and
   nothing here writes a file format of its own. A second writer is a second
   set of rules about what a capture means.

What is deliberately not here: any register level. There is no register map,
no register cache, and no second control path behind the link. The device's
processing chain is set through the library's own operations, in the
device's words. Head training in the browser is also not here: the library
trains a head from a recording and uploads it, and one implementation is how
what a head produces on the device stays identical to what it produces on
the host.

Some working rules: real numbers only, from the code that measured them; an
export says what it cost by measuring the file it wrote; nothing works until
it is checked against a build, a test, or hardware; the page is one file
with no build step and no external requests, keep it so; a control that
cannot do anything is worse than an absent one, so a route and its panel are
deleted together.

| | |
|---|---|
| `hub.py` | the local server: routes, capture lifecycle, device ownership |
| `hub.html` | the page, one file, no build step |
| `context.py` | where this installation's captures are, stated once |
| `hub.sh`, `install-launcher.sh` | the desktop launcher |
| `tools/` | the capture command lines |
| `exports/` | regenerable output, never the record |
| `tests/run_all.py` | the suite: no device, no radio, no network |

This application depends on `intomind`, the instrument library, installed
from its own repository. The dependency runs one way: the library knows
nothing about this application.
