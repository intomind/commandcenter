# Command Center

The IntoMind Command Center. A local server that owns the connection to a
biopotential device, plus a browser page that runs the instrument, records
sessions, and analyzes them.

Everything runs on the machine in front of you. Recordings are written to disk
locally and are not sent anywhere.

## Running it

    python3 hub.py

Then open http://localhost:8080. Nothing connects by itself: the page lists
every device it hears, and connects the one you pick. The page serves whether
or not a device is connected, so recorded sessions can be reviewed with no
hardware present.

Needs Python 3.11 or newer, the instrument API, and:

| package | what it is for |
|---|---|
| `intomind` | the instrument API: the link, the protocol, captures, exports, analyses |
| `aiohttp` | the local server and its websocket |
| `numpy` | the analyses |
| `scipy` | the analyses |
| `h5py` | optional, and only for the MATLAB export |

The API is its own package, installed rather than found on a path, with its
analyses, and the server needs aiohttp:

    pip install 'intomind[analysis]' aiohttp

## The device decides what appears

There is no list of gains, sample rates or modes in this application or in the
page it serves. A device declares what it is over the protocol, the library
composes that into `device.profile()`, and the page renders exactly that. A
device with fewer capabilities shows fewer controls. One that declares a
control this application has never heard of still gets a control.

The same rule decides whole panels. Electrode contact appears only on a device
that reports lead-off detection, and the model panel appears only on a device
that reports a model. A panel for something the device cannot do is worse than
no panel, because it reads as a fault in the device.

## The model

A device can carry an encoder and a set of heads, which are small sets of
weights, each trained for one question. When the device has one, the settings
drawer lists its head slots, lets one be selected, and turns predictions on and
off. Each output appears with the head that produced it and with the flags the
device sent: a window with a gap in it, or with an electrode off, produced that
number and the page says so.

From firmware 1.3 the device also reports how long one pass of its encoder
takes, and how often it describes a window. The interval is set in the same
panel. A longer one leaves the processor idle between passes, and the device
keeps it across power cycles. A head trained beside a different encoder is still offered, marked,
because its outputs may not mean what its name says.

Heads are trained on the embeddings the device sends, which the page can
collect to a file, with `intomind.model`, and uploaded from there. There is no way to train one from the browser, and there should not be:
what a head does on the device has to match what it does on the host exactly,
and one implementation is how that stays true.

## The device's name

From firmware 1.3 a device keeps a name and an adjective and advertises them
composed with the product's name: Ada's Blue IntoMind One. The settings drawer
sets both and shows the composed name as it is typed, counted in bytes, because
the air carries at most 29. When two devices in a host's list share a name, the
host numbers the later ones. The number belongs to the host, and the device
never holds it.

## A synthetic signal

A device that holds a generator can stream a synthetic signal in place of its
electrodes, for demonstrations and development. The page says so above the
trace for as long as it lasts, and a recording made meanwhile says so in its
manifest.

## Exports

Six formats, all of them the library's: `npz`, `edf`, `bdf`, `csv`, `tsv` and
MATLAB v7.3. This application writes no file format of its own, because a
second writer is a second set of rules about what a capture means and the two
would drift.

Pick a capture, pick a format, and the file comes back through the browser. The
page then reports what that export cost, measured on the file that was just
written rather than claimed about the format: how many samples it holds,
whether every sample came back at the value it was recorded at, and the largest
error if not. A capture that fails its own checksum is not exported at all.

## It answers only this machine

A page anywhere on the internet can make a browser send a request to
`localhost`, and a WebSocket handshake is not subject to the same-origin policy
at all. Every request is checked for a local `Host` and, when it carries one, a
local `Origin`. Without that check a foreign page could start runs, delete
recordings, or read the live stream.

## From a desktop icon

On Linux, `./install-launcher.sh` puts the Command Center in the application
list so it can be launched from an icon and pinned to the dock. It generates a
`.desktop` entry pointing at `hub.sh` and installs the icon. Nothing it writes
is inside this directory, and `python3 hub.py` works the same either way.
`./install-launcher.sh --uninstall` takes it back out.

The icon starts a server if none is running, then opens a window onto it. **The
server is the instrument and the window is only a view of it**, so:

- Clicking the icon again opens another window onto the same server. It never
  starts a second one, which would fail on the port anyway.
- Closing the window does not stop the server, drop the link, or end a
  recording. A run holds its samples in memory until it ends, so a stray click
  on a window's X must not be able to discard one.
- To actually stop it, right-click the icon and choose **Release device**. It
  refuses while a run is going.

`hub.sh` chooses the Python interpreter itself instead of trusting `PATH`: a
desktop session's `PATH` is not a terminal's, and the two resolving to
different interpreters is the usual reason an icon bounces once and gives up.
Set `COMMAND_CENTER_PYTHON` to override the choice. Everything a launch prints
goes to `~/.cache/command-center.log`, and a failure puts the tail of it on
screen.

## What is in here

| | |
|---|---|
| `hub.py` | the local server: routes, capture lifecycle, device ownership |
| `hub.html` | the page. One file, no build step, no external requests |
| `hub.sh` | the desktop launcher: interpreter choice, one server, the window |
| `install-launcher.sh` | generates and installs the `.desktop` entry and icon |
| `context.py` | where this installation's captures are, stated once |
| `tools/` | the capture CLIs: analyze, export, metrics, montage backfill |
| `captures/` | the recordings this application made and reads |
| `exports/` | files written out of those recordings, regenerable |
| `docs/` | the user and developer documentation, served at `/docs/` |
| `assets/` | page assets |
| `tests/run_all.py` | the suite. No device, no BLE, no network |

## Three modes

The page has Basic, Intermediate and Advanced. The mode changes how much of the
instrument you may change. It never changes what the device does, and it never
hides your data or your view.

## Tests

    python3 tests/run_all.py

That is the command, and there is no other. No device, no BLE, no network, no
pytest.

## License

Free software under the GNU Affero General Public License, version 3. We
also license it commercially, on fair terms shaped by your use case: write
to contact@intomind.com. See `LICENSING.md`, and `CONTRIBUTING.md` before a
first pull request.

IntoMind is a trademark of IntoMind, Inc. The license covers the code, not the
name.
