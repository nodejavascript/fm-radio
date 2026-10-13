# FM radio in a container

**A receiver should be honest about what it is hearing: a station is told from hiss by evidence and
not by loudness.** Everything here follows from that. The scan calls a channel a station only when
its 19 kHz stereo pilot is actually present — static has no pilot at all — and it refuses to call a
channel a station when the receiver merely happens to be loud there. That is why the list it
produces is short and trustworthy rather than long and partly wrong.

What it is: a web page for a USB FM radio. Tune it, scan the band, keep the stations you like.
One file of Python, standard library only, plus the `v4l2-ctl` and `parec`/`pactl` command-line tools.

```bash
cp ~/.fm_radio.py ./fm_radio.py   # if you are building from the running copy
docker compose up -d
# then open http://127.0.0.1:8150/
```

## What the host must provide

The container is deliberately thin. **All three of these are the host's business, and a container
cannot supply any of them:**

| Needed | Why |
|---|---|
| `/dev/radio0` | the kernel must already drive the stick (for one common stick: `radio_shark` + `tea575x`). A container cannot load a kernel module into a host it does not own. |
| an audio server socket | the app asks *your* PipeWire/PulseAudio to route the radio to the speakers, so the sound comes out of your machine. |
| the audio cookie | if your server authenticates clients with one. Both are mounted in `docker-compose.yml`. |

## Settings

| Variable | Default | What it does |
|---|---|---|
| `RADIO_DEVICE` | `/dev/radio0` | the V4L2 radio device |
| `RADIO_SOURCE` | *(empty)* | the audio input. Empty means discover it at start-up — the first `alsa_input.usb-` entry in `pactl list short sources`. A built-in microphone is never chosen, because a radio that has gone missing must not be replaced by the room. |
| `RADIO_PORT` | `8150` | the page's port |
| `RADIO_DATA` | `/data` | where saved stations and the last scan are written |
| `RADIO_V4L2` / `RADIO_PAREC` / `RADIO_PACTL` | `v4l2-ctl` / `parec` / `pactl` | the tool names, if they are not on `PATH` |

## Two things worth knowing before you run it

**A missing radio must not be answered with the microphone.** Unplug a USB input and PipeWire does
not report a failure — it moves the streams that pointed at it to the *default* input, which is
usually the built-in microphone. Measured on the machine this was written on: the app's own capture
then read the room, its loopback played the room back into the speakers (which is feedback, and it
gets louder with the volume), and a scan would have swept the room and called it the band. The
program therefore refuses to capture or play unless its named input is in the audio server's own
list, and it removes any loopback still pointing at a source that has gone.

**The station test is the 19 kHz stereo pilot.** A channel is called a station when that pilot
stands at least 15 dB above the noise beside it, which is how static and pure tones are kept off the
list. The thresholds were measured against a stick whose driver reports no signal strength at all. A
stick whose driver *does* report `Signal strength` could be judged on that instead — this one
cannot, so it measures the pilot.

## Source

`fm_radio.py` is the whole program. One file: copy it anywhere, run it with `python3`.

## Credits, and the one obligation in this tree

**The station table inside the program — frequency, call sign, city and format — was compiled from
Wikipedia's lists of radio stations** (the Ontario and New York lists). The individual facts are
facts and are nobody's property, but a list extracted from Wikipedia is under **CC BY-SA 4.0**, so
it is credited here and the data is marked as such. If you reuse that table, keep this credit; the
program itself is not affected by it.

The program drives `v4l2-ctl` (v4l-utils) and `parec`/`pactl` (PulseAudio) as separate processes.
It neither includes nor links them, so their licences place no condition on this code. No kernel
driver is distributed here: the radio's driver is part of the Linux kernel and stays on the host.

No product name is claimed here. The program talks to whatever the kernel exposes as a V4L2 radio
device, and a stick's own make and model are only mentioned in `README`-level notes where they save
you measuring something twice. Nothing here is affiliated with, endorsed by, or derived from any
radio manufacturer's software.

## Licence

**MIT — see `LICENSE`.** Copy it, change it, ship it, sell it; keep the copyright notice with it.

```
MIT License

Copyright (c) 2026 nodejavascript

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

One obligation rides alongside it: the station table inside the program came from Wikipedia and
stays under CC BY-SA 4.0 — see Credits above. That covers the data, not the code.
