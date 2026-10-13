#!/usr/bin/env python3
"""FM radio control page — a click-only web UI for a V4L2 FM tuner (/dev/radio0).

The scan is three-pass: a QUICK sweep of the whole band, then the LICENSED CARRIERS it flagged,
then a re-check of each. Frequencies are anchored to the published carrier list for this market,
because this receiver hears a station cleanly across about ±0.3 MHz and the middle of that
plateau is a measurement of the RECEIVER rather than of the station — measured, it moved 0.2-0.4
MHz between runs and put entries on frequencies with nothing licensed on them at all.

🔴 A STATION IS TOLD FROM HISS AND FROM A SINE WAVE BY THE 19 kHz STEREO PILOT — not by
loudness, and no longer by the audio's mean frequency. A broadcast FM station carries a 19 kHz
pilot tone, and a receiver LOCKED on one sees it; static has no line there, and neither does a
tone. Measured on this unit, per 0.6 s look: locked station looks stand +26 to +44 dB above the
band beside the pilot, and static looks -14.9 to +12.6 dB. The level decides NOTHING — it
follows the programme (19 dB of movement at one fixed frequency) and it is compressed — it only
ranks stations that have already passed the pilot, and it is what the chart draws.

A PURE TONE is not a station however loud it is, and the mean frequency cannot tell: a 1 kHz
tone reads 999 Hz, exactly where a station reads. Its SHAPE does — measured, a tone puts 99.3%
of its energy into three bins with a crest factor of 1.41 and a 40 ms autocorrelation of 1.00,
against 0.7-12%, 2.65-5.93 and -0.33-0.19 for every real station look.

AND THIS RECEIVER ALTERNATES. Five looks at 98.1 MHz, with the tuner read back at exactly
98.100000 MHz each time, gave: hiss, hiss, programme, programme, programme — and 98.5 did the
same in reverse, while 97.7 and 98.3 read programme on every look. The hiss state is always
about -22.9 dB with its energy wide and high, so it is a LOST CARRIER and not a mute. Every
frequency is therefore looked at several times, a MAJORITY decides, and nothing stops early.

PRIOR WORDING, preserved and now dead: "A station is told from hiss by the AUDIO'S MEAN
FREQUENCY ... while the mean frequency held ... every measurement in the scan therefore takes
several looks and stops early when it has what it needs." That rule is what let static and sine
waves onto the high-reception list: it reads a tone as programme, and the early exit meant one
locked look in five earned a channel a pass.
"""
import array, json, math, os, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 🔴 EVERYTHING THAT DIFFERS BETWEEN MACHINES IS SETTABLE, so this file can run in a container
# on somebody else's computer. The defaults are this machine and stay exactly as they were; the
# environment variables are what a docker-compose file sets.
def _env(name, default):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v


PORT     = int(_env("RADIO_PORT", "8150"))
# 🔴 WHERE IT LISTENS, AND WHY IT IS SETTABLE. On this machine the page is loopback-only on
# purpose — nobody else on the network can tune the radio. In a container that is a bug: the
# published port arrives from outside the container's loopback, so a server bound to 127.0.0.1
# inside can never be reached and the page answers nothing (measured: HTTP 000 with
# `0.0.0.0:8152->8150/tcp` listening on the host). The container sets RADIO_BIND=0.0.0.0, and
# there it is safe, because what is exposed is still only what the compose file publishes.
BIND     = _env("RADIO_BIND", "127.0.0.1")
DEVICE   = _env("RADIO_DEVICE", "/dev/radio0")
# THE AUDIO INPUT IS THE ONE SETTING THAT TRULY DIFFERS BETWEEN MACHINES: the source name
# carries the device's USB id on some systems and not on others, and the card number moves.
# Name it with RADIO_SOURCE, or leave that empty and a USB capture input is discovered at
# start-up — only a `usb-` input is considered, because the built-in microphone is not a radio
# and a `.monitor` is an output being looped back rather than an input at all.
SOURCE   = _env("RADIO_SOURCE", "")
# Nothing is written outside this directory: the saved stations, the mute flag, the last scan.
DATA_DIR = os.path.expanduser(_env("RADIO_DATA", "~"))
STATIONS = os.path.join(DATA_DIR, ".fm_radio_stations.json")
MUTEFILE = os.path.join(DATA_DIR, ".fm_radio_muted")
OLDPRE   = os.path.join(DATA_DIR, ".fm_radio_presets.json")
# Bare names, so PATH resolves them: inside a container they are /usr/bin too, but a distribution
# that puts them elsewhere should not need this file edited.
V4L2  = _env("RADIO_V4L2", "v4l2-ctl")
PAREC = _env("RADIO_PAREC", "parec")
PACTL = _env("RADIO_PACTL", "pactl")

# PASS 1 sweeps EVERY real channel once — 100 points at 0.2 MHz, about a minute.
# EACH POINT IS LOOKED AT TWICE AND THE BETTER LOOK IS KEPT. One look is not a measurement on
# this receiver: passing 94.7 MHz, the sweep read -37.3 dB while the same frequency read -13.6
# dB ten minutes earlier — a station's quiet moment and a channel with nothing on it are the
# same reading. The loudest of a few looks cannot hide a station, and hiss never produces one.
P1_STEP, P1_WIN, P1_SETTLE, P1_REPS = 0.2, 0.25, 0.15, 2
# PASS 2 measures the LICENSED CARRIERS the sweep flagged, and it is the pass that DECIDES.
# PASS 3 re-checks each survivor on fresh looks. Both are set below, beside PILOT_WIN, because
# both are the pilot's own look length and count — and they are set there and not here so that
# the file cannot be read out of order.
# THE FLOOR: a low percentile of the sweep's own 100 readings. It is not re-measured, and the
# reason is measured — see the note at the point of use.
FLOOR_PCT = 0.20
# 🔴 THE STATION TEST IS THE 19 kHz STEREO PILOT — NOT "IS IT LOUD", AND NOT THE MEAN FREQUENCY.
#
# A broadcast FM station carries a 19 kHz pilot tone. A receiver LOCKED on a station sees it; a
# channel carrying nothing but noise does not. That is how a real radio decides it has a station,
# and it is what the two things filling the high-reception list cannot fake:
#
#   * STATIC has no pilot. Measured on this unit over 25 looks on hiss: the best a noise-only look
#     ever managed with the strictest reference below was +1.8 dB. A locked station look never
#     read below +20.6 dB. PILOT_MIN_DB sits in that gap.
#   * A SINE WAVE has no pilot either (a pure tone leaves the reference band nearly empty, so its
#     ratio divides by almost nothing and still only reached +9.0 dB). Tones are rejected
#     separately, on their shape; see TONE_* below.
#
# 🔴 AND THE REFERENCE IS THE LOUDEST OF THE THREE PROBES, NOT THEIR MIDDLE. That is a measured
# choice: with the middle of the three, hiss reached +19.8 dB and a station's weakest locked look
# was +25.1 dB — a 5 dB gap that is too thin to trust. Taking the loudest neighbour as the floor
# measures the line against the WORST case beside it, which opened the same gap to 19 dB.
PILOT_HZ = 19000.0
PILOT_REF = (14200.0, 15500.0, 16800.0)   # the neighbourhood the pilot must stand above; FM audio
                                          # is gone by 15 kHz, so this band should be noise
PILOT_MIN_DB = 15.0                       # measured: locked looks >= +20.6, static <= +1.8
# HOW MANY LOOKS, AND HOW MANY MUST LOCK. THE LOCK COMES IN STRETCHES, so the looks must cover
# several of them: measured at 95.3 MHz, five looks in a row read hiss and then five read
# programme, at an unchanged tuned frequency. Four looks taken back to back can land entirely
# inside one stretch and call a station static — which is what happened here, and why 95.3 was
# dropped on a first attempt. Six looks, and at least two locked.
PILOT_LOOKS, PILOT_MIN_LOCKED = 6, 2
PILOT_WIN = 0.4                           # Goertzel is 2.9 Hz wide here: the pilot is a steady
                                          # tone, so a short look measures it perfectly well
P2_WIN, P2_REPS = PILOT_WIN, PILOT_LOOKS  # pass 2: six looks per carrier — the SCREEN
# 🔴 PASS 3 IS THE PASS THAT DECIDES, AND IT TAKES FOURTEEN LOOKS. Six was not enough and the
# failure is measured: a six-look window lands inside ONE of this receiver's lock stretches,
# so across three whole scans 98.1, 97.7 and 94.7 — all real, all local — were dropped in the
# re-check on one run and confirmed on the next, while 103.7 and 104.5 came in. Fourteen looks
# span about seven seconds, which covers two or three stretches, and that is long enough for the
# lock rate to mean something. It costs about three minutes, and it is the only thing that does.
P3_WIN, P3_REPS = PILOT_WIN, 14
# 🔴 BOTH PASSES RETRY A ZERO BEFORE THEY BELIEVE IT, AND THIS IS THE FIX FOR A REAL STATION BEING
# DELETED BY THE RECEIVER'S OWN DROPOUT. Measured on 12 October 2026: 91.1 held its 19 kHz pilot on
# 14 of 14 looks at 14:53 and then read 0 of 6 at 14:57 — and so did 91.3, 91.5, 91.7 and 91.9, a
# few seconds apart, because this receiver drops out in multi-second stretches. A 2.4-second screen
# can therefore land wholly inside one dropout and throw a station away; pass 3 has the same fault
# (88.3 hit 2 of 14 in one run and 7 of 20 in the run before; 103.7 hit 0 of 14 and 8 of 20). A
# station read fourteen times inside one dropout was not measured — so it is re-measured once, and
# only a carrier that reads zero pays for it.
PILOT_RETRY = 4                           # light second look, for a screen that read nothing
# 🔴 THE SECOND WINDOW IS THE SAME LENGTH AS THE FIRST, AND THAT IS NOT A DETAIL. It was 8 looks,
# and an 8-look window — about three seconds of audio — then won the label on rate alone and marked
# carriers "high" off 8 of 8 looks, a stretch shorter than this receiver's switching. Windows are
# only comparable with one another at the same length, and a window has to be long enough to span
# at least one switch before its lock rate means anything.
P3_RETRY = 14
# THE LABEL'S TWO NUMBERS, IN ONE PLACE, because pass 3's retry asks the same question the label
# does — has this carrier already read high? — and two copies of 0.75 would drift apart.
CONF_HIGH, CONF_MEDIUM = 0.75, 0.30
# The lock rate a carrier must reach to be called a station. Measured over 8 looks: real
# stations 3, 5, 5 and 6 of 8 (38-75%), every static channel 0 or 1 of 8 (0-12%). Over 14 looks
# that is 5 or more against 2 or fewer, and the bar of 4 sits inside the gap.
PILOT_MIN_FINAL = 4
# And the bar pass 2 uses — a SCREEN, so it is deliberately loose: its job is to throw away the
# obvious noise cheaply and let anything plausible through to the pass that decides.
PILOT_MIN_SCREEN = 2
# ONE STATION PER STRETCH OF BAND — how close two carriers have to be to be compared as one
# possibly being the other's skirt. This receiver hears a station across about ±0.3 MHz and this
# market's channels are 0.2 MHz apart, so carriers this close can be the same station heard twice.
# NOTE: carriers this close are COMPARED, never dropped for being close — see _resolve_groups.
PLATEAU_NEAR = 0.4
# THE TONE TEST. A sine reads 99.3% of its energy in three bins against 0.7-12% for real
# programme, and it is also flat-topped and perfectly periodic. A look is TONAL when any one of
# the three says so. The margin is wide: a synthetic tone measured crest 1.41 and r40 1.00, and
# every real station look measured crest 2.65-5.93 and r40 -0.33 to +0.19.
TONE_TOP3 = 0.70                          # share of the band's energy in its best three bins
TONE_CREST = 2.20                         # peak / RMS
TONE_R40 = 0.85                           # autocorrelation at 40 ms
TONE_LOOKS = 3                            # tonal looks out of four, and it is not a station
# THE MEAN FREQUENCY, now a SECONDARY check rather than the station test. Measured separation:
# station medians 1,800-2,900 Hz, hiss 4,400-5,700 Hz — but a 1 kHz sine reads 999 Hz, which is
# how sine waves passed as programme in the first place.
FEGOOD = 3600.0
# HOW CLOSE A SWEEP LEAD HAS TO BE to a licensed carrier for that carrier to be measured.
CARRIER_REACH = 0.5
SETTLE                   = 0.20           # settle for the fine passes
MARGIN                   = 5.0            # a CONFIRMED station must beat the band middle by this
MARGIN_HOT               = 3.0            # pass 1 only has to NOTICE a station: this level reads
                                          # programme loudness, so one quiet passage must not be
                                          # allowed to hide a real station from the sweep
REC_HIGH                 = 9.0            # a station this far over the measured floor is HIGH
                                          # reception — the same 9 dB the page splits its two
                                          # lists on, so the two can never disagree

# 🔴 THE CARRIERS THAT EXIST IN THIS MARKET — frequency: (call sign, city, format).
# Built from the published station lists for Ontario and New York State, filtered to the cities
# within about an hour of Hamilton. THIS IS THE SCAN'S ANCHOR, and it is what makes the
# frequencies right: this receiver hears a station cleanly across about ±0.3 MHz, so the middle
# of that plateau is a measurement of the RECEIVER and not of the station — it moved 0.2-0.4 MHz
# between runs and put entries on frequencies with nothing licensed on them at all (95.5, 89.7,
# 104.9, 100.3, 100.9, 92.7, 89.7). The scan now measures the carriers themselves, reports them
# at their real frequency, and a stretch of band with no carrier on it is not a station.
MARKET = {
    88.1: ("CIND-FM", "Toronto", "indie rock"),
    88.3: ("CJIQ-FM", "Kitchener", "campus radio"),
    88.5: ("CKLK-FM/CKDX-FM", "Grimsby/Beamsville/Newmarket", "classic hits"),
    88.7: ("WBFO", "Buffalo", "public radio"),
    88.9: ("CIRV-FM", "Toronto", "multilingual"),
    89.1: ("CBLA-FM-2/CKYY-FM", "Paris/Welland", "public news/talk"),
    89.5: ("CIUT-FM", "Toronto", "campus radio"),
    89.9: ("CJBC-FM-2/WBWA", "Paris/Buffalo", "public music (French)"),
    90.1: ("WRCU-FM", "Hamilton", "freeform"),
    90.3: ("CJBC-FM", "Toronto", "public music (French)"),
    90.7: ("CHQI-FM/CBL-FM-2", "Niagara-on-the-Lake/Paris", "tourist information"),
    91.1: ("CJRT-FM", "Toronto", "jazz"),
    91.3: ("WBNY", "Buffalo", "campus radio"),
    91.5: ("CKBT-FM", "Kitchener", "CHR"),
    91.7: ("CIXL-FM", "Welland", "classic rock"),
    91.9: ("CFRE-FM/CHIN-1-FM", "Mississauga/Toronto", "campus radio"),
    92.1: ("CKPC-FM/WZDV", "Brantford/Amherst", "adult contemporary"),
    92.5: ("CKIS-FM", "Toronto", "CHR"),
    92.9: ("CHTG-FM/WBUF", "Caledonia/Buffalo", "classic hits"),
    93.3: ("CFRU-FM/CFMU-FM/CFBN-FM", "Guelph/Hamilton/St. Catharines", "campus radio"),
    93.5: ("CFXJ-FM", "Toronto", "country"),
    93.7: ("CJTW-FM/WBLK", "Kitchener/Depew", "Christian radio"),
    93.9: ("CFWC-FM", "Brantford", "country"),
    94.1: ("CBL-FM", "Toronto", "public music"),
    94.5: ("WNED-FM", "Buffalo", "classical"),
    94.7: ("CHKX-FM", "Hamilton", "country"),
    95.3: ("CING-FM", "Hamilton", "adult contemporary"),
    95.9: ("CJKX-FM", "Ajax/Toronto", "country"),
    96.1: ("WTSS", "Buffalo", "adult contemporary"),
    96.3: ("CFMZ-FM", "Toronto", "classical"),
    96.7: ("CHYM-FM", "Kitchener", "adult contemporary"),
    96.9: ("CKHC-FM/WGRF", "Toronto/Buffalo", "campus radio/classic rock"),
    97.3: ("CHBM-FM", "Toronto", "classic hits"),
    97.7: ("CHTZ-FM", "St. Catharines", "active rock"),
    98.1: ("CHFI-FM", "Toronto", "adult contemporary"),
    98.5: ("CKWR-FM/WKSE", "Waterloo/Niagara Falls", "community radio/CHR"),
    98.7: ("CKFG-FM", "Toronto", "multilingual"),
    98.9: ("CHCD-FM", "Simcoe", "adult contemporary"),
    99.1: ("CBLA-FM", "Toronto", "public news/talk"),
    99.5: ("CFCA-FM/WDCX-FM", "Waterloo/Buffalo", "CHR/religious"),
    99.7: ("CKNC-FM", "Simcoe", "oldies"),
    99.9: ("CKFM-FM", "Toronto", "CHR"),
    100.7: ("CHIN-FM", "Toronto", "multilingual"),
    101.1: ("CFLZ-FM", "Fort Erie", "adult hits"),
    101.3: ("CJML-FM/CJSA-FM", "Milton/Toronto", "adult contemporary"),
    101.5: ("CIOI-FM/CKMO-FM", "Hamilton/Orangeville", "campus radio"),
    102.1: ("CFNY-FM", "Brampton", "modern rock"),
    102.5: ("WBKV", "Buffalo", "contemporary Christian"),
    102.7: ("CHOP-FM/CJRK-FM/CKMS-FM", "Newmarket/Scarborough/Waterloo", "campus radio"),
    102.9: ("CKLH-FM", "Hamilton", "adult hits"),
    103.3: ("WEDG", "Buffalo", "alternative rock"),
    103.5: ("CIDC-FM", "Orangeville", "CHR"),
    103.7: ("CFBU-FM", "St. Catharines", "campus radio"),
    104.1: ("WHTT-FM", "Buffalo", "classic hits"),
    104.5: ("CHUM-FM", "Toronto", "hot adult contemporary"),
    105.1: ("CJED-FM/CHOQ-FM", "Niagara Falls/Toronto", "hot adult contemporary"),
    105.3: ("CKSC-FM/CKKW-FM", "Scarborough/Waterloo", "Christian radio/adult hits"),
    105.5: ("CHRY-FM", "Toronto", "urban alternative"),
    105.7: ("CHRE-FM", "St. Catharines", "adult contemporary"),
    105.9: ("CFMS-FM", "Markham", "adult contemporary/multilingual"),
    106.1: ("CIMJ-FM", "Guelph", "hot adult contemporary"),
    106.5: ("WYRK", "Buffalo", "country"),
    106.7: ("CIKZ-FM", "Waterloo", "country"),
    107.1: ("CILQ-FM", "Toronto", "mainstream rock"),
    107.5: ("CJDV-FM", "Cambridge", "active rock"),
    107.9: ("CJXY-FM", "Burlington", "active rock"),
}

lock = threading.Lock()
state = {
    "freq": 98.1,
    "want_playing": True,
    "muted": False,
    "meter": {"db": None},
    "scan": {"running": False, "phase": 0, "stage": "", "i": 0, "n": 0, "points": [],
             "stations": [], "cands": [], "log": [], "lvl_base": None,
             "prog": {"1": 0.0, "2": 0.0, "3": 0.0, "total": 0.0}},
}
_last_play_try = 0.0
_last_state_check = 0.0
_cached_playing = False
_cached_input = True             # is the input we capture from actually there? see source_exists
_last_mute_press = 0.0          # when the MUTE BUTTON was last pressed by hand
_last_view = time.time()        # when a browser last asked for the state
NO_VIEW_MUTE = 120.0            # ...silence the radio after this long with no page open. It is the
                                # FALLBACK, not the mechanism: the page says goodbye on the way out
                                # and that is now instant. This has to stay generous, because a tab
                                # hidden behind another one is throttled by the browser and its poll
                                # can drop to once a minute — muting on that would take the radio
                                # away while it is still on screen in another window.
try:                            # and stay as he left it through a restart
    if os.path.exists(MUTEFILE):
        state["muted"] = open(MUTEFILE).read().strip() == "1"
except Exception:
    pass


# ---------------------------------------------------------------- audio + tuner
def run(args, timeout=None):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace")
    except Exception:
        return 1, ""


def tune(f):
    f = round(float(f), 2)
    run([V4L2, "-d", DEVICE, "--set-freq=%.1f" % f])
    with lock:
        state["freq"] = f
    return f


def resolve_source():
    """Pick the capture input once, at start-up, when the environment did not name one.

    Portability, and nothing else: `RADIO_SOURCE` is set on the machine this was built on and
    needs no discovery, but a fresh install has a different name (it can carry the USB id, or
    not) and there is nothing to guess from. `pactl list short sources` is the list the audio
    server itself keeps, and the first entry that is not a `.monitor` is the radio.
    """
    global SOURCE
    if SOURCE:
        return SOURCE
    _, out = run([PACTL, "list", "short", "sources"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("alsa_input.usb-"):
            SOURCE = parts[1]
            break
    return SOURCE


def _loopback_lines():
    """Our loopback, and only ours: the module line names the source it copies, so it is matched
    on that rather than on a device name — matching a device by its make and model would find
    nothing at all on a machine that uses a different stick."""
    _, out = run([PACTL, "list", "short", "modules"])
    return [l for l in out.splitlines()
            if "module-loopback" in l and SOURCE and SOURCE in l]


def source_exists():
    """Is the input we were told to capture from actually there?

    🔴 A VANISHED INPUT IS NOT A FAILURE, IT IS A RE-ROUTE — measured 13 October 2026. The radio
    was unplugged and a printer went into the same port; the PipeWire source
    disappeared, and the audio server moved BOTH streams that were pointed at it to the DEFAULT
    input, which is this machine's microphone. Measured consequences, both real: `parec` captured
    the room (`application.name = parec`, source = the onboard input), and the loopback played the
    room back into the speakers — which is the feedback he heard, and which rises with the volume.
    A scan in that state would have swept the room and reported it as the band.

    So an input that is not in the audio server's own list is refused, and a loopback still
    pointing at it is removed before it can be re-routed to anything else.
    """
    if not SOURCE:
        return False
    _, out = run([PACTL, "list", "short", "sources"])
    return any(SOURCE in l for l in out.splitlines())


def drop_stale_loopbacks():
    """Unload our own loopbacks while the source they name is gone, so they cannot re-route."""
    if source_exists():
        return 0
    gone = 0
    for line in _loopback_lines():
        run([PACTL, "unload-module", line.split()[0]])
        gone += 1
    return gone


def playing():
    return bool(_loopback_lines())


def ensure_play():
    """Keep the radio audible: self-heals if the loopback ever disappears."""
    global _last_play_try
    with lock:
        want = state["want_playing"] and not state["muted"]
    if not want:
        ensure_mute()                                  # muted, stopped, or scanning
        return False
    if not source_exists():
        drop_stale_loopbacks()                         # before the server re-routes it to the mic
        ensure_mute()
        return False
    if playing():
        return True
    if time.time() - _last_play_try < 3:
        return False
    _last_play_try = time.time()
    _, sink = run([PACTL, "get-default-sink"])
    run([PACTL, "load-module", "module-loopback", "source=" + SOURCE,
         "sink=" + (sink.strip() or "default"), "latency_msec=50"])
    with lock:
        still_wanted = state["want_playing"] and not state["muted"]   # may have muted mid-load
    if not still_wanted:
        ensure_mute()
    return playing()


def ensure_mute():
    """Nothing may be audible while a scan runs, whatever else recreates it."""
    lines = _loopback_lines()
    for line in lines:
        run([PACTL, "unload-module", line.split()[0]])
    return bool(lines)


def _pactl_volume():
    """The radio capture level, read back from PipeWire so the slider shows the truth and not a
    remembered number."""
    _, out = run([PACTL, "get-source-volume", SOURCE])
    for tok in out.replace("/", " ").split():
        if tok.endswith("%"):
            try:
                return int(tok[:-1])
            except ValueError:
                pass
    return None


def set_volume(pct):
    """THE RADIO'S OWN VOLUME KNOB — the capture level of the radio's input source.

    🔴 IT IS THE SOURCE AND NOT THE SPEAKERS, deliberately. Turning down the default sink would
    change the volume of everything else on this machine, and the loopback would fight it; the
    source level belongs to the radio alone, it is what the loopback carries to the speakers, and
    it is what the sound meter reads. The station test is untouched by it because the 19 kHz pilot
    is measured as a ratio inside one capture — measured, a locked look stands the same number of
    dB above the noise whatever the capture level is.
    """
    try:
        pct = max(0, min(100, int(round(float(pct)))))
    except (TypeError, ValueError):
        return None
    if _pactl_volume() is None:                # the source is not there to set
        return None
    run([PACTL, "set-source-volume", SOURCE, "%d%%" % pct])
    got = _pactl_volume()
    with lock:
        state["volume"] = got if got is not None else pct
        return state["volume"]


def set_mute(on):
    """Silence only — the station and the scan are untouched."""
    global _last_play_try
    with lock:
        state["muted"] = bool(on)
        if not on:
            state["want_playing"] = True           # unmuting means you want to hear it
    try:                                           # so a restart cannot startle him
        with open(MUTEFILE, "w") as fh:
            fh.write("1" if on else "0")
    except Exception:
        pass
    if on:
        ensure_mute()
    else:
        _last_play_try = 0.0                       # a button press must not wait for the throttle
        ensure_play()
    with lock:
        return state["muted"]


def play(on):
    with lock:
        state["want_playing"] = bool(on)
        if on:
            state["muted"] = False                 # pressing play means you want sound
    if on:
        _last_play_try = 0.0
        ensure_play()
    else:
        for line in _loopback_lines():
            run([PACTL, "unload-module", line.split()[0]])
    return playing()


# ------------------------------------------------------- one continuous capture
class Stream:
    """A single long-lived capture. Windows are measured without restarting anything."""

    def __init__(self, src, keep_seconds=6.0):
        self.src, self.keep = src, keep_seconds
        self.buf, self.base, self.total = bytearray(), 0, 0
        self.lock, self.proc, self.alive = threading.Lock(), None, False

    def start(self):
        if self.alive:
            return True
        if not source_exists():        # the input is gone: refusing is the only honest answer,
            return False               # because parec would silently capture the microphone
        try:
            self.proc = subprocess.Popen(
                [PAREC, "--device=" + self.src, "--format=s16le", "--rate=48000",
                 "--channels=2", "--raw"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            return False
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        return True

    def _reader(self):
        keep_bytes = int(self.keep * 48000 * 2) * 2
        while self.alive and self.proc and self.proc.stdout:
            d = self.proc.stdout.read(1 << 15)
            if not d:
                break
            with self.lock:
                self.buf.extend(d)
                self.total += len(d) // 2
                if len(self.buf) > keep_bytes:
                    drop = (len(self.buf) - keep_bytes) // 2 * 2
                    del self.buf[:drop]
                    self.base += drop // 2

    def now(self):
        return self.total

    def _frames(self, a, b):
        """The mono frames in a..b, or [] if there are not enough of them.

        The capture is interleaved stereo, so the frames are averaged to mono first —
        decimating the interleaved bytes directly aliases the two channels together.
        """
        with self.lock:
            lo, hi = max(0, a - self.base), min(len(self.buf) // 2, b - self.base)
            if hi - lo < 4800:
                return []
            seg = array.array("h")
            seg.frombytes(bytes(self.buf[lo * 2: hi * 2]))
        fr = [0.5 * (seg[i] + seg[i + 1]) for i in range(0, len(seg) - 1, 2)]
        return fr if len(fr) >= 2400 else []

    def _blocks(self, a, b):
        """The window cut into ~24 blocks, each measured on its own, in dB."""
        frames = self._frames(a, b)
        if not frames:
            return []
        blk = max(480, len(frames) // 24)
        out = []
        for i in range(0, len(frames) - blk + 1, blk):
            chunk = frames[i:i + blk]
            rr = (sum(float(v) * v for v in chunk) / len(chunk)) ** 0.5 / 32768.0
            if rr > 0:
                out.append(20 * math.log10(rr))
        return out

    def qstats(self, a, b, peak=False):
        """(level dB, mean frequency Hz) for one window — the scan's station test.

        For a tone, RMS(first difference) / RMS(signal) = 2*sin(pi*f/fs), so the ratio gives
        the frequency the energy sits at. Nothing to install and nothing to tune: measured on
        this unit it puts programme at 1,800-2,900 Hz and hiss at 4,400-5,700 Hz.
        """
        frames = self._frames(a, b)
        blk = self._blocks(a, b)
        if not frames or not blk:
            return -99.9, 0.0
        ordered = sorted(blk)
        level = ordered[-1] if peak else ordered[len(ordered) // 2]
        n = len(frames)
        rms = (sum(float(v) * v for v in frames) / n) ** 0.5
        if rms <= 0:
            return round(level, 1), 0.0
        drms = (sum((frames[i] - frames[i - 1]) ** 2 for i in range(1, n)) / (n - 1)) ** 0.5
        return round(level, 1), round((drms / rms) * 48000 / (2 * math.pi))

    def stats(self, a, b, peak=False):
        """(level dB, spread dB) over samples a..b.

        MEDIAN by default: steady from one sweep to the next, which is what a reading has to
        be. A single window's RMS swings with the programme — that is why the same channel
        used to read anywhere from -23 to -14 dB.
        """
        blk = self._blocks(a, b)
        if not blk:
            return -99.9, 0.0
        ordered = sorted(blk)
        if len(ordered) >= 8:
            spread = ordered[int(len(ordered) * 0.9)] - ordered[int(len(ordered) * 0.1)]
        else:
            spread = 0.0
        return round(ordered[-1] if peak else ordered[len(ordered) // 2], 1), round(spread, 1)

    def stop(self):
        self.alive = False
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        return True


resolve_source()                    # before the capture stream is built, never after
STREAM = Stream(SOURCE)
LASTSCAN = os.path.join(DATA_DIR, ".fm_radio_last_scan.json")


def _load_last_scan():
    try:
        with open(LASTSCAN) as fh:
            got = json.load(fh)
        if isinstance(got, dict) and got.get("points") is not None:
            state["scan"].update(got)
            state["scan"]["running"] = False
    except Exception:
        pass


def _read_tuner():
    """What the hardware is actually tuned to, so the page never shows a stale number."""
    _, out = run([V4L2, "-d", DEVICE, "--all"])
    for line in out.splitlines():
        if "Frequency for tuner" in line and "(" in line:
            try:
                return round(float(line.split("(")[1].split("MHz")[0].strip()), 1)
            except Exception:
                break
    return None


def _save_last_scan():
    try:
        with open(LASTSCAN, "w") as fh:
            json.dump(state["scan"], fh)
    except Exception:
        pass


_load_last_scan()
_tuned = _read_tuner()
if _tuned:
    state["freq"] = _tuned
with lock:                    # 🔴 THE LAST SCAN'S FINDINGS STAY ON SCREEN ACROSS A RESTART.
    # They used to be moved to `prev` and the live list emptied — and the page reads the live
    # list, while NOTHING reads `prev`, so a restart wiped the band list and both columns said
    # "none" while the JSON still held 28 stations. Measured 12:28 on 13 October 2026, from a
    # screenshot of the page. `prev` is still filled, because something may want the older
    # findings, but the findings themselves are no longer thrown away.
    state["prev"] = list(state["scan"].get("stations") or [])
_vol0 = _pactl_volume()        # the slider shows the radio's REAL level, not a remembered one
if _vol0 is not None:
    state["volume"] = _vol0


def sample(stream, freq, settle=SETTLE, win=P1_WIN, peak=False, reps=1):
    """Retune, let it settle, then measure. Returns (best level dB, best mean frequency Hz).

    With more than one look the BEST of them is kept for both readings: the loudest for the
    level and the LOWEST mean frequency for the quality, because a station's programme is what
    has to be caught and one quiet or muted look must not be able to brand a real station as
    hiss. The wait is the window that was asked for, not a fixed third of a second — the old
    fixed wait meant a 0.15 s window really measured 0.33 s and every pass-1 point cost double.
    """
    if freq is not None:
        tune(freq)
    time.sleep(settle)
    level, feq = -99.9, 9999.0
    need = max(6000, int(win * 48000 * 0.95))
    for _ in range(max(1, reps)):
        a = stream.now()
        time.sleep(win)
        b = stream.now()
        waited = 0.0
        while b - a < need and waited < 0.5:      # a stalled capture is not audio
            time.sleep(0.05)
            waited += 0.05
            b = stream.now()
        lvl, q = stream.qstats(a, b, peak=peak)
        if lvl > level:
            level = lvl
        if q > 0:
            feq = min(feq, q)
    return level, feq


# --------------------------------------------------------------- the pilot detector
# Two ways of measuring one line in the audio: a Goertzel filter, for the pilot at its exact
# frequency, and a radix-2 FFT, for the shape of the audio. Both are written out because
# neither library is available, and both are cheap enough to run on every look.
_TWIDDLE = {}


def _goertzel(frames, freq):
    """Power at ONE exact frequency — a single DFT bin, by the Goertzel recurrence.

    Chosen over an FFT bin for a measured reason: the pilot has to be measured AT 19,000 Hz,
    and the nearest bins of a 1024-point FFT are 23 Hz away, which throws away most of the
    margin this test lives on. Goertzel measures the one frequency asked for, two
    multiplications a sample, with a selectivity of about 1/(window) — 1.7 Hz over 0.6 s.
    """
    n = len(frames)
    if n < 2400:
        return 0.0
    k = 2.0 * math.cos(2.0 * math.pi * freq / 48000.0)
    s1 = s2 = 0.0
    for v in frames:
        s0 = v + k * s1 - s2
        s2 = s1
        s1 = s0
    return max(0.0, (s1 * s1 + s2 * s2 - k * s1 * s2)) / float(n * n)


def pilot_db(frames):
    """How far the 19 kHz stereo pilot stands above the band beside it, in dB.

    The reference is the LOUDEST of three probes in the 14.2-16.8 kHz band, which is a measured
    choice and not a cautious one: measuring against the middle probe let hiss reach +19.8 dB
    while a station's weakest locked look was +25.1 — too thin. Against the loudest neighbour the
    same 25 static looks topped out at +1.8 dB and the locked looks still read +20.6 and up.
    """
    if not frames:
        return -99.0
    ref = max(_goertzel(frames, f) for f in PILOT_REF)
    p = _goertzel(frames, PILOT_HZ)
    if ref <= 0.0 or p <= 0.0:
        return -99.0
    return 10.0 * math.log10(p / ref)


def _power_spectrum(frames):
    """Power in each of 512 bins of a Hann-windowed 1024-sample slice, mean removed.

    THE MEAN IS REMOVED AND NOTHING BELOW 300 Hz IS EVER MEASURED, both for a measured reason:
    window and DC leakage from bins 0-6 reported a "tone" at 117 Hz carrying 43% of the energy
    on ordinary hiss. Above 300 Hz that same hiss reads 0.7-3%.
    """
    n = 1024
    seg = frames[-n:]
    mu = sum(seg) / float(n)
    re = [(seg[i] - mu) * (0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1))) for i in range(n)]
    im = [0.0] * n
    if n not in _TWIDDLE:
        _TWIDDLE[n] = [(math.cos(-2.0 * math.pi * k / n), math.sin(-2.0 * math.pi * k / n))
                       for k in range(n // 2)]
    tw = _TWIDDLE[n]
    j = 0
    for i in range(1, n):                                    # bit-reversal permutation
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
    size = 2
    while size <= n:
        half, step = size // 2, n // size
        for i in range(0, n, size):
            for k in range(half):
                wr, wi = tw[k * step]
                xr, xi = re[i + k + half], im[i + k + half]
                tr = xr * wr - xi * wi
                ti = xr * wi + xi * wr
                re[i + k + half] = re[i + k] - tr
                im[i + k + half] = im[i + k] - ti
                re[i + k] += tr
                im[i + k] += ti
        size <<= 1
    return [(re[i] * re[i] + im[i] * im[i]) for i in range(n // 2)]


def tone_stats(frames):
    """(best-three-bin share, crest factor, autocorrelation at 40 ms) — the tone test.

    A pure sine is not a station, however loud it is, and the mean frequency cannot tell: a 1
    kHz tone reads 999 Hz, exactly where a station reads. Its SHAPE gives it away, in three
    independent ways, and each has a margin wide enough to trust on one look.
    """
    if len(frames) < 4800:
        return 0.0, 0.0, 0.0
    spec = _power_spectrum(frames)
    bins = spec[8:int(10000 / (48000.0 / 1024))]             # 375 Hz-10 kHz, clear of DC leakage
    total = sum(bins)
    top3 = (sum(sorted(bins)[-3:]) / total) if total > 0 else 1.0
    n = len(frames)
    rms = (sum(float(v) * v for v in frames) / n) ** 0.5
    crest = (max(abs(v) for v in frames) / rms) if rms > 0 else 0.0
    lag = 1920                                               # 40 ms at 48 kHz
    a, b = frames[:-lag], frames[lag:]
    ma, mb = sum(a) / float(len(a)), sum(b) / float(len(b))
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(len(a)))
    da = sum((v - ma) ** 2 for v in a) ** 0.5
    db = sum((v - mb) ** 2 for v in b) ** 0.5
    r40 = (num / (da * db)) if da > 0 and db > 0 else 0.0
    return top3, crest, r40


def _blank(freq):
    """A carrier's running totals, before any look has been taken."""
    return {"f": freq, "looks": 0, "lock": 0, "snd": 0, "tone": 0, "pilot_max": -99.0,
            "lvl": -99.9, "feq": 0.0, "top3_max": 0.0, "crest_min": 9.9, "levels": [], "fqs": []}


def _look(freq, win=PILOT_WIN, settle=SETTLE, retune=True):
    """ONE look at one frequency: capture, and measure everything the decision needs.

    🔴 `retune` IS NOT A DETAIL. This receiver MUTES ITS AUDIO for a moment after every frequency
    change, and re-tuning before every look threw away the first moments of all of them — measured,
    it dropped 88.9, 89.5 and 90.3 from 12 locked looks out of 20 to 1 out of 14, while the whole
    rest of the band was unaffected. So a look is taken with `retune=False` when the receiver is
    ALREADY on that frequency (consecutive looks at one carrier), and with `retune=True` only when
    it may have moved since (the group case, where carriers take turns).
    """
    if retune:
        tune(freq)
        time.sleep(settle)
    a = STREAM.now()
    time.sleep(win)
    b = STREAM.now()
    waited = 0.0
    while b - a < int(win * 48000) and waited < 0.8:          # a stalled capture is not audio
        time.sleep(0.1)
        waited += 0.1
        b = STREAM.now()
    frames = STREAM._frames(a, b)
    if not frames:
        return None
    blk = STREAM._blocks(a, b)
    ordered = sorted(blk) if blk else []
    n = len(frames)
    rms = (sum(float(v) * v for v in frames) / n) ** 0.5
    drms = (sum((frames[i] - frames[i - 1]) ** 2 for i in range(1, n)) / (n - 1)) ** 0.5
    return {"lvl": ordered[len(ordered) // 2] if ordered else -99.9,
            "feq": (drms / rms) * 48000.0 / (2 * math.pi) if rms > 0 else 0.0,
            "pilot": pilot_db(frames),
            "tone": tone_stats(frames)}


def _absorb(out, look):
    """Fold one look into a carrier's totals. A look that produced no audio is dropped."""
    if look is None:
        return
    out["looks"] += 1
    out["pilot_max"] = max(out["pilot_max"], look["pilot"])
    if look["pilot"] >= PILOT_MIN_DB:
        out["lock"] += 1
        out["levels"].append(look["lvl"])      # only a LOCKED look measures a station
    if look["feq"] > 0:
        out["fqs"].append(look["feq"])
        if look["feq"] < FEGOOD:
            out["snd"] += 1
    top3, crest, r40 = look["tone"]
    out["top3_max"] = max(out["top3_max"], top3)
    out["crest_min"] = min(out["crest_min"], crest)
    if top3 >= TONE_TOP3 or crest < TONE_CREST or r40 >= TONE_R40:
        out["tone"] += 1


def _finish(out):
    if out["levels"]:
        out["lvl"] = round(_median(out["levels"]), 1)
    if out["fqs"]:
        out["feq"] = round(_median(out["fqs"]))
    out["pilot_max"] = round(out["pilot_max"], 1)
    out["top3_max"] = round(out["top3_max"], 3)
    out["crest_min"] = round(out["crest_min"], 2)
    out["levels"] = []
    out["fqs"] = []
    return out


def qualify(freq, looks=PILOT_LOOKS, win=PILOT_WIN, settle=SETTLE):
    """Look at ONE frequency `looks` times and decide, ON THE PILOT, whether a station is there.

    Returns counts and measurements — no early exit, and no best-of. The rule this replaces kept
    the LOWEST mean frequency and the LOUDEST level across looks, so ONE locked look in five
    earned the channel a pass and the loudest look set the level it was ranked by. That is how
    static and sine waves reached the high-reception list. Here every look votes, and the
    majority decides.
    """
    out = _blank(freq)
    tune(freq)                                  # ONCE, then look without moving the receiver
    time.sleep(settle)
    for _ in range(max(1, looks)):
        _absorb(out, _look(freq, win, retune=False))
        if state["muted"]:
            ensure_mute()
    return _finish(out)


def qualify_group(freqs, looks, win=PILOT_WIN, settle=SETTLE):
    """Measure a GROUP of nearby carriers IN TURN — one look each, round and round.

    🔴 WHY THE CARRIERS OF A GROUP MUST SHARE THEIR SECONDS. A listening test on 91.3, which this
    scan had labelled HIGH because it held 20 of 20 looks, and it sounded poor — while 91.1,
    which it had labelled "next to it", was much better. Measuring both again, one after the
    other, gave 91.1 held 14 of 14 against 91.3's 9 of 14: the opposite of the scan. Both are
    true, and that is the defect — THIS RECEIVER ALTERNATES IN STRETCHES OF SECONDS, so the
    carriers are not compared at all when they are measured one at a time; each one is compared
    against whatever the receiver happened to be doing during its own turn. Interleaving gives
    every carrier in a group the SAME receiver states, which is the only way to say which of two
    carriers 0.2 MHz apart is the one with a transmitter on it. A second gain falls out of it:
    each carrier's looks now spread across the whole group's turn, so they cover more of the
    alternation than the same number of consecutive looks would.
    """
    outs = {f: _blank(f) for f in freqs}
    for _ in range(max(1, looks)):
        for f in freqs:
            # ONE turn each, so every carrier in the group meets the same receiver states — and
            # the audio mute that follows a frequency change is paid once per turn, not per look.
            _absorb(outs[f], _look(f, win, settle, retune=True))
            if state["muted"]:
                ensure_mute()
    return {f: _finish(o) for f, o in outs.items()}


# 🔴 THE dB GATE THAT USED TO SKIP A GROUP IS GONE, AND IT WAS MY ERROR. It ran the turn-taking
# measurement only when the members' SEQUENTIAL pilots were within AMBIG_DB (10 dB) of each other —
# and the sequential pilots are exactly the numbers this receiver cannot compare, because they are
# taken one carrier at a time, minutes apart, with the audio coming and going. Measured: 91.3 and
# 91.1 read within 1 dB of each other sequentially (+55.4 against +54.8) but +46.9 against +27.3 in
# turn. So the gate could only ever skip the group that needed measuring: with 91.1 at +45 and 91.3
# at +29 sequentially — 16 dB apart — it would have skipped the very pair that was heard. Every group
# of carriers 0.2 MHz apart is now measured taking turns, and that is cheap because the groups are
# few and never more than three carriers.
# Taking turns is expensive on a weak carrier (see pass 3), so it is kept short and given a longer
# settle than the sequential pass: the receiver has just changed frequency and needs the moment.
AMBIG_LOOKS, AMBIG_SETTLE = 6, 0.5
# A turning gap this big, WITH THE TURNING RUN PROVEN GOOD, says the weaker carrier is the skirt of
# the stronger one. Below it both are left as stations and merely pointed at each other, because
# two real stations can sit 0.2 MHz apart — 96.3 CFMZ and 96.7 CHYM do.
ADJ_DB = 15.0


def _resolve_groups(stations, log):
    """Point out which carrier of a close pair the receiver is really hearing — and never guesser.

    SETTLED BY LISTENING, not by the scan: 91.3 held 20 of 20 looks and was
    labelled HIGH in a scan, and it sounded poor, while 91.1 sounded much better. Taking turns,
    91.1 reads +46.9 dB against 91.3's +27.3 — repeatable across three runs — so 91.1 is the
    station and 91.3 is what its skirt leaves behind. Off a station's own frequency the audio
    arrives through the IF skirt, which is why the pilot stands less far above the noise.

    Two things this must NOT do, both learned by measurement in the hour after it was first built:
    ✗ IT MUST NOT DROP A CARRIER FOR BEING CLOSE TO ANOTHER. It did, and it threw away 96.7 CHYM
      — a real Kitchener station — as "38 dB below 96.3", and marked 97.7 CHTZ, which had just
      held 20 of 20 looks, as a skirt. Drops belong to the sequential pass, where one frequency is
      looked at fourteen times; nothing is dropped for being near a neighbour.
    ✗ IT MUST NOT TRUST A TURNING RUN THAT WENT WRONG. Taking turns costs a weak carrier its lock
      (88.9: 14 of 14 alone, 2 of 14 in turn), so only the members that kept their lock for at
      least half the looks are used, and only those settle the group — they are the ones measured
      under the same conditions as each other. ⚠ THE FIRST VERSION OF THIS DISCARDED THE WHOLE RUN
      WHEN ANY MEMBER MISSED, AND THAT WAS MY ERROR: on this receiver a member often misses on its
      own, so most groups were discarded and left unresolved — which is why 91.3 kept its HIGH
      label and the 91.3 reading sounded poor where 91.1 was much better. A member that missed is now
      neither dropped nor claimed about; it keeps its own sequential numbers and is pointed at the
      group.
    """
    if len(stations) < 2:
        return stations
    freqs = [s["f"] for s in stations]
    groups, placed = [], set()
    for f in freqs:                       # anchored, not chained, and never more than three
        if f in placed:
            continue
        g = [f]
        for o in freqs:
            if len(g) >= 3:
                break
            if o not in placed and o != f and abs(o - f) <= PLATEAU_NEAR + 1e-9:
                g.append(o)
        placed.update(g)
        groups.append(g)
    by_f = {s["f"]: s for s in stations}
    pilot = {s["f"]: (s["pilot"] if s["pilot"] is not None else -99.0) for s in stations}
    for g in groups:
        if len(g) < 2:
            continue
        log("group %s - these are 0.2 MHz apart, so they are measured taking turns, which is the "
            "only way to say which of them has a transmitter" % "/".join("%.1f" % f for f in g))
        try:
            qs = qualify_group(g, AMBIG_LOOKS, PILOT_WIN, AMBIG_SETTLE)
        except Exception:
            qs = {}
        held = [f for f in g if f in qs and qs[f] and qs[f]["looks"]
                and qs[f]["lock"] >= max(1, qs[f]["looks"] // 2)]
        missed = [f for f in g if f not in held]
        if not held:
            log(" - that run is DISCARDED: not one of %s kept its lock while the receiver moved, so "
                "what it measured is the receiver's own state and not a carrier. The sequential "
                "numbers stand" % "/".join("%.1f" % f for f in g))
            continue
        # 🔴 ONE MEMBER MISSING NO LONGER THROWS THE WHOLE RUN AWAY, AND THAT IS THE SECOND BUG THAT
        # KEPT 91.3 LOOKING HIGH. Taking turns costs a weak carrier its lock (88.9: 14 of 14 alone,
        # 2 of 14 in turn), and this receiver also alternates on its own, so in a group of three it
        # was common for one member to miss — and then the run was discarded and the pair was left
        # unresolved. Now the members that DID hold settle the group among themselves, because those
        # are the ones measured under the same conditions as each other; the ones that missed are
        # neither dropped nor claimed about — they are pointed at the group and keep their own
        # sequential readings. Nothing here is dropped for being close to a neighbour: 96.7 CHYM was,
        # and it is a real Kitchener station.
        for f in held:
            pilot[f] = qs[f]["pilot_max"]
            by_f[f]["pilot"] = qs[f]["pilot_max"]
        if missed:
            log(" - the receiver could not hold %s while it was moving between carriers, so nothing "
                "is claimed about %s; %s did hold and settle the group"
                % ("/".join("%.1f" % f for f in missed), "/".join("%.1f" % f for f in missed),
                   "/".join("%.1f" % f for f in held)))
        order = sorted(held, key=lambda f: -pilot[f])
        prim = order[0]
        for f in order[1:]:
            s = by_f[f]
            if pilot[prim] - pilot[f] >= ADJ_DB:
                s["primary"] = False
                s["adj"] = prim
                s.pop("near", None)
                s["conf"] = "low"
                log("%.1f MHz is NEXT TO %.1f MHz (pilot %.1f against %.1f dB, measured in turn) - "
                    "%.1f is the real carrier here, so what is heard on %.1f is that station"
                    % (f, prim, pilot[f], pilot[prim], prim, f))
            else:
                s["near"] = prim
                log("%.1f MHz and %.1f MHz are only %.1f dB apart in the pilot - both left as "
                    "stations, because two real stations can sit this close"
                    % (f, prim, pilot[prim] - pilot[f]))
        for f in missed:
            by_f[f]["near"] = prim
    return stations


def _best_window(windows, min_looks=0):
    """The window in which this receiver held the carrier BEST — and that is what gets reported.

    🔴 WHY THE BEST WINDOW, AND NEITHER THE LAST ONE NOR THE SUM. Traced look by look on 12 October
    2026, and this receiver does not FADE, it SWITCHES. At 100.7 MHz the 19 kHz pilot stood at power
    1.4e+04 for four looks and then collapsed to 50-300 — a 20 dB drop — and stayed there, with the
    audio level falling with it. At 95.3 MHz it was absent for four looks, came back, and then held
    for sixteen. So one station read 0 of 14 looks and 14 of 14 looks IN THE SAME MINUTE, and:
      * a SINGLE window is a coin toss — which is why 100.7 CHIN read 20 of 20 in one scan and was
        dropped outright in the next, and why 95.3 went from high to medium between two scans;
      * the SUM of the windows answers a question nobody asked, and it dragged clear stations down,
        because the 6-look screen is the part most likely to land inside a switch.
    The useful question is whether this receiver can hold the carrier at all, and the answer is the
    stretch in which it did. Static cannot win this: across 40 traced looks with no station the
    pilot never passed +13.6 dB against the 15 dB bar, and a sine wave has no pilot line at all.
    And it must never be one LOOK or one SHORT window that decides — `min_looks` insists a window be
    long enough to span a switch before its rate is allowed to set a label.
    """
    good = [w for w in windows if w and w.get("looks") and w["looks"] >= min_looks]
    if not good:
        return None
    return max(good, key=lambda w: (w["lock"] / float(w["looks"]), w["looks"]))


# --------------------------------------------------------------------- stations
def saved():
    for path, key in ((STATIONS, "saved"), (OLDPRE, "presets")):
        try:
            with open(path) as fh:
                return sorted(round(float(x), 1) for x in json.load(fh).get(key, []))
        except Exception:
            continue
    return []


def save_saved(lst):
    with open(STATIONS, "w") as fh:
        json.dump({"saved": sorted(set(round(float(x), 1) for x in lst))}, fh, indent=1)
    return saved()


def keep_station(f, near=0.35):
    lst = saved()
    f = round(float(f), 1)
    if any(abs(f - x) <= near for x in lst):     # same station, already on the list
        return lst
    return save_saved(lst + [f])


# ------------------------------------------------------------------- band scan
def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _finding(f, q, base, call="", city="", fmt=""):
    """What is known about one confirmed station, and how much to trust it.

    🔴 CONFIDENCE COMES FROM THE LOCK RATE FIRST AND THE LEVEL SECOND — the reverse of the old
    rule, and the fix for the false high-reception entries. On this receiver the level cannot
    rank stations: it follows the programme (10-25 dB of movement at one fixed frequency) and
    it is compressed, so a 100 kW local and a 250 W campus station can differ by a few dB. The
    pilot lock is not a measurement of the audio at all — it is a fact about the receiver being
    on a station — so it ranks reliably. Level still separates two stations that both hold the
    lock. `call`, `city` and `fmt` come from the published carrier list for this market.

    A finding is deliberately NOT added to the saved list here: the page offers it and the
    person watching decides. Silence would otherwise fill the list without asking.
    """
    looks = q["looks"] or 1
    rate = q["lock"] / float(looks)
    above = round(q["lvl"] - base, 1) if (base is not None and q["lvl"] > -90) else None
    # 🔴 THE RECEPTION LABEL IS THE LOCK RATE, AND NOTHING ELSE. That is what reception means: how
    # much of the time this receiver actually holds the station. Measured over 20 looks it either
    # holds it almost always (20/20, 19/20, 17/20 on 95.3, 97.7, 98.1, 88.1) or a coherent part of
    # the time (12/20, 8/20), and NOTHING static ever reached 3/20. Two earlier versions also
    # demanded that the audio sound like programme on half the looks, or that the carrier stand
    # 9 dB over the band middle, and both were wrong here: the audio drops out on many looks even
    # on a station held perfectly, and the levels on this receiver are compressed so 9 dB over the
    # floor excluded every station. Level RANKS the list; it never labels it.
    if rate >= CONF_HIGH:
        conf = "high"
    elif rate >= CONF_MEDIUM:
        conf = "medium"
    else:
        conf = "low"
    # 🔴 THE TONE TEST NO LONGER TOUCHES THE LABEL, AND THAT CAP WAS A DEFECT OF MINE. It demoted a
    # carrier from high to medium whenever the tone test fired on 3 looks — and 100.7 and 91.3 were
    # BOTH capped that way WHILE HOLDING THE PILOT ON 20 OF 20 LOOKS, which is the app contradicting
    # its own measurement on the face of its report. The test over-fires on real stations in any
    # case (measured top3 0.85-0.97 on 95.3, 97.3, 97.7 and 101.1, every one of them real), because
    # a 0.4-second look can be filled by a single sustained note inside ordinary music. Nothing is
    # lost by dropping the cap: a sine wave is already rejected by the pilot itself, and a synthetic
    # tone measured -5.9 dB against the 15 dB bar. The count is still recorded, and still printed in
    # the log when a channel is dropped.
    tonal = q["tone"] >= 3
    return {"f": f, "db": q["lvl"], "above": above, "conf": conf, "feq": q["feq"],
            "lock": q["lock"], "looks": looks, "rate": round(rate, 2), "tonal": tonal,
            "snd": q["snd"], "pilot": q["pilot_max"], "primary": True, "call": call,
            "city": city, "fmt": fmt}


def _why_not(q):
    """One plain line saying which test rejected a channel — the log is read by a person.

    The pilot is reported FIRST, because it is the test that actually rejected the channel: the
    other way round, a real station that happened to hold a sustained note was described as "a
    tone, not a station" when what had happened is that its pilot was not locked often enough.
    """
    if q["lock"] < PILOT_MIN_FINAL:
        line = ("the 19 kHz pilot was locked on only %d of %d looks (best %+.1f dB, needs %+.1f) "
                "- static or a station this receiver cannot hold"
                % (q["lock"], q["looks"], q["pilot_max"], PILOT_MIN_DB))
        if q["tone"] >= TONE_LOOKS:
            line += ("; it also read as a tone for most of the looks (top3 %.2f, crest %.2f), "
                     "which on this receiver is what one sustained note inside music does"
                     % (q["top3_max"], q["crest_min"]))
        return line
    return "dropped in the re-check"


def _grid(lo, hi, step):
    out, f = [], lo
    while f <= hi + 1e-9:
        out.append(round(f, 2))
        f += step
    return out


def scan_worker():
    """Three passes. Each one sweeps, fine-tunes what it found, and reports to the page."""
    if not STREAM.start():
        with lock:
            state["scan"]["running"] = False
            if not source_exists():
                log("there is no radio to scan - nothing is connected at %s, so there is no audio "
                    "to measure. Plug the radio in and scan again." % DEVICE)
        return
    # THE SCAN OBEYS THE MUTE BUTTON. It does not run a silence of its own behind the
    # button's back: it PRESSES mute, so the button shows the truth, and the button stays in
    # charge from then on. Press Unmute during a scan and you hear what the scan is doing -
    # the scan will not fight you for it. Whatever he last pressed is what stands.
    with lock:
        was_muted, was_playing = state["muted"], state["want_playing"]
        started_at = time.time()
    set_mute(True)
    # This event gets its OWN name. It used to be called `quiet`, and `quiet` was ALSO used
    # for the list of quiet channels measured below — so rebinding it to a list killed this
    # watchdog about twenty seconds into every scan, and the `quiet.set()` at the end of the
    # scan raised on a list, which meant the scan never gave the button back either.
    done_ev = threading.Event()

    def mute_watch():
        while not done_ev.wait(2.0):
            if state["muted"]:                        # silent because the button says so
                ensure_mute()

    threading.Thread(target=mute_watch, daemon=True).start()
    save_saved([])        # a scan CLEARS the saved list: its findings are the new candidates
    with lock:                                   # the last scan's findings move over here
        state["prev"] = list(state["scan"].get("stations") or [])
        state["scan"] = {"running": True, "phase": 1, "stage": "pass 1 of 3 - sweeping the band",
                         "i": 0, "n": 0, "points": [], "stations": [], "cands": [],
                         "log": [], "lvl_base": None,
                         "prog": {"1": 0.0, "2": 0.0, "3": 0.0, "total": 0.0}}
    completed = False
    log = lambda msg: state["scan"]["log"].append("%s  %s" % (time.strftime("%H:%M:%S"), msg))

    def set_prog(key=None, value=None):
        """How far through each pass, and through all three, for the page's progress chart.

        Each pass counts the measurements it actually makes, so a bar is the real work and not
        a clock: pass 1 is its sweep points, pass 2 is the channels it must measure around the
        markers, pass 3 is three channels per frequency. The TOTAL gives each pass one third,
        which is why it cannot jump backwards the way a time estimate does when a pass turns
        out to hold more work than the last scan did.
        """
        with lock:
            pr = state["scan"]["prog"]
            if key is not None:
                pr[key] = max(pr.get(key, 0.0), min(1.0, float(value)))    # never goes backwards
            pr["total"] = round((pr["1"] + pr["2"] + pr["3"]) / 3.0, 4)
    try:
        t0 = time.time()                                  # let the capture fill first
        while STREAM.now() < 48000 and time.time() - t0 < 5:
            time.sleep(0.1)

        def do_pass(pass_no, freqs, win, settle=SETTLE, peak=False, reps=1):
            """tune and measure a list of frequencies, tagged with the pass number."""
            out = []
            for i, f in enumerate(freqs):
                if not state["scan"]["running"]:
                    return None
                lvl, feq = sample(STREAM, f, settle=settle, win=win, peak=peak, reps=reps)
                if lvl < -90:
                    lvl, feq = sample(STREAM, None, settle=0.5, win=win, peak=peak, reps=reps)
                with lock:
                    state["scan"]["points"].append([f, lvl, feq, pass_no])
                    state["scan"]["i"] = i + 1
                if pass_no == 1:
                    set_prog("1", (i + 1) / max(1, len(freqs)))
                out.append((f, lvl, feq))
                if i % 5 == 0 and state["muted"]:     # hold the silence only while it is asked for
                    ensure_mute()                     # some other caller may have re-loaded it
            ensure_play()
            return out

        # ---- PASS 1: the whole band, fast ------------------------------------
        grid1 = _grid(88.1, 107.9, P1_STEP)          # 100 points at 0.2 MHz - every channel, ~30 s
        with lock:
            state["scan"]["n"] = len(grid1)
        p1 = do_pass(1, grid1, P1_WIN, settle=P1_SETTLE, reps=P1_REPS)
        if p1 is None:
            return
        levels = [l for _, l, _ in p1 if l > -90]
        base = _median(levels) if levels else -40.0
        thr = round(base + MARGIN_HOT, 1)          # pass 1 only has to hear that it is loud
        with lock:
            state["scan"]["lvl_base"] = round(base, 1)
        # The bar a station has to clear is MEASURED, not assumed: quiet channels the sweep
        # found are re-measured with the SAME window the stations get, and the middle reading
        # is the floor. TEN of them, spread across the band — a floor taken from a handful of
        # single readings moved 3 dB between runs, and that alone changed how many stations
        # went on to survive pass 2.
        # THE FLOOR COMES FROM THE SWEEP ITSELF — nothing is re-measured for it. Re-measuring the
        # ten quietest points was WRONG, and this is the measurement that proves it: the ten
        # "quietest" points read -30 to -38 dB in the sweep and -22 dB when re-measured, because
        # the quietest READINGS are not the quietest CHANNELS — they are whichever channels were
        # caught during one of this receiver's dropouts. The bar they set sat ABOVE every real
        # station, so a whole scan reported nothing but weak reception. A low percentile of all
        # 100 readings cannot be dragged that way, is steadier, and costs no extra time.
        levels_all = sorted(l for _, l, _ in p1 if l > -90)
        floor_lvl = levels_all[max(0, int(len(levels_all) * FLOOR_PCT) - 1)] if levels_all else base
        confirm = round(floor_lvl + MARGIN, 1)
        with lock:
            state["scan"]["lvl_base"] = round(floor_lvl, 1)
            log("floor %.1f dB (reading %d of %d in the sweep) - a station must beat %.1f dB"
                % (floor_lvl, int(len(levels_all) * FLOOR_PCT), len(levels_all), confirm))
        # WHICH CARRIERS TO GO AND MEASURE. A point is a LEAD if it was loud, or if its audio
        # sounded like programme. Every licensed carrier within CARRIER_REACH of a lead is then
        # measured properly in pass 2 — the CARRIERS, not the middle of the loud run, because the
        # middle of that run is where the RECEIVER hears best and not where the station is:
        # measured, it moved 0.2-0.4 MHz between runs and put entries on frequencies that have
        # nothing licensed on them at all.
        leads = [f for f, l, q in p1 if l >= thr]
        # 🔴 A LEAD IS A LOUD POINT AND NOTHING ELSE. The mean-frequency test is deliberately NOT
        # used to pick carriers any more, and the reason is the same plateau: this receiver hears
        # a station across about ±0.3 MHz, so on this market's 0.2 MHz channel grid almost every
        # point sounds like programme — that test flagged all 65 carriers and made pass 2 the
        # longest part of a scan. Loudness still localises a station, because the plateau is
        # loudest over the transmitter.
        near = {c for c in MARKET if any(abs(c - f) <= CARRIER_REACH for f in leads)}
        # 🔴 A CARRIER IS NEVER MEASURED WITHOUT ITS 0.2 MHz NEIGHBOURS, AND THAT IS THE BUG THAT
        # HID 91.1. A listening test on 91.3, which this scan called HIGH, sounded poor while 91.1
        # sounded much better — and 91.1 was not in the list at all, so the pair could never be
        # compared. The cause: the set was built as "licensed carriers within CARRIER_REACH of a
        # LEAD", and a lead at 91.7 is 0.4 MHz from 91.3 and 91.5 but 0.6 from 91.1 — so the pair was
        # split and only one of the two was ever measured. Closing the set under adjacency is what
        # makes a group comparable, and comparing a group is the whole reason turn-taking exists.
        # Measured with the set closed, 91.1 holds its pilot 14 of 14 looks on its own.
        grew = True
        while grew:
            grew = False
            for c in sorted(MARKET):
                if c in near:
                    continue
                if any(abs(c - o) <= 0.2 + 1e-9 for o in near):
                    near.add(c)
                    grew = True
        near = sorted(near)
        with lock:
            state["scan"]["cands"] = [{"f": c} for c in near]
            log("pass 1 of 3 done - %d points in the sweep, %d leads, %d licensed carriers to check"
                % (len(grid1), len(leads), len(near)))

        # ---- PASS 2: measure the licensed carriers the sweep flagged -----------
        with lock:
            state["scan"]["phase"] = 2
            state["scan"]["stage"] = "pass 2 of 3 - measuring the licensed carriers"
            state["scan"]["n"] = len(near)
        kept = []
        for k, c in enumerate(near):
            if not state["scan"]["running"]:
                return
            with lock:
                state["scan"]["i"] = k
            q = qualify(c, PILOT_LOOKS, PILOT_WIN)
            if not q["looks"]:
                return
            # 🔴 A SCREEN THAT READ ZERO IS RE-MEASURED BEFORE IT IS BELIEVED. This cost the scan
            # the station that mattered: 91.1 held its pilot 14 of 14 looks at 14:53, read 0 of
            # 6 at 14:57, and was dropped — and with it went the comparison against 91.3, because a
            # pair cannot be settled when only one of them is in the list. The retry is only for a
            # carrier that read nothing, so an empty channel pays one extra look and no more.
            if not q["lock"]:
                q_retry = qualify(c, PILOT_RETRY, PILOT_WIN)
                if q_retry["looks"] and q_retry["lock"] > q["lock"]:
                    log("%.1f MHz read nothing on the first six looks and held the pilot on %d of %d "
                        "when it was measured again - the receiver had dropped out, not the station"
                        % (c, q_retry["lock"], q_retry["looks"]))
                    q = q_retry
            with lock:
                state["scan"]["points"].append([c, q["lvl"], q["feq"], 2])
            set_prog("2", (k + 1) / max(1, len(near)))
            # 🔴 THE STATION TEST, IN THREE PARTS, AND IT IS THE PILOT THAT DECIDES.
            #   * STATIC is rejected by the pilot — no line at 19 kHz means no station. Measured:
            #     static never beat +1.8 dB against the loudest neighbour, and a locked station
            #     look never read below +20.6 dB.
            #   * A SINE WAVE is rejected by its SHAPE — measured 99.3% of its energy in three
            #     bins, crest factor 1.41 and 40 ms autocorrelation 1.00, against 0.7-12%,
            #     2.65-5.93 and -0.33-0.19 for every real station look.
            #   * A STATION THE RECEIVER CANNOT HOLD is rejected by the LOCK RATE. Nothing else
            #     is required to pass: an earlier version also demanded that the mean frequency
            #     read as programme on most looks, and THAT WAS A FALSE-FAILURE MACHINE — this
            #     receiver drops a station for seconds at a time, so 94.7 (a 100 kW Hamilton
            #     local), 102.9 CKLH and 103.3 WEDG were all thrown away by it while their
            #     pilots were locked. Over the whole 58-carrier scan no static carrier ever
            #     reached two locked looks of six, so the LOCK COUNT is what separates them.
            if q["lock"] >= PILOT_MIN_SCREEN:
                kept.append((c, q))
            else:
                log("%.1f MHz dropped - %s" % (c, _why_not(q)))
            if k % 5 == 0 and state["muted"]:
                ensure_mute()
        with lock:
            state["scan"]["cands"] = [{"f": c} for c, _ in kept]
            log("pass 2 of 3 done - %d of %d licensed carriers hold a station"
                % (len(kept), len(near)))

        # ---- PASS 3: re-check each carrier for QUALITY — the pass that decides --
        with lock:
            state["scan"]["phase"] = 3
            state["scan"]["stage"] = "pass 3 of 3 - re-checking each carrier for static"
            state["scan"]["n"] = len(kept)
        stations = []
        rejected = 0
        # 🔴 THE BAND IS MEASURED ONE CARRIER AT A TIME, WHICH IS NOT THE FAIREST WAY AND IS THE
        # ONLY WAY THAT WORKS. Measuring the neighbours of a carrier in turn looks obviously
        # better — they would meet the same receiver states — and it was built, run and MEASURED,
        # and it breaks the weak carriers: 88.9 held 14 of 14 looks on its own and 2 of 14 when it
        # took turns with 89.1; 96.3 held 12 of 14 alone and 2 of 14 in turn with 96.7. The strong
        # carrier of each pair stayed at 14 of 14. So taking turns is reserved for the one thing it
        # is needed for — deciding WHICH of two ambiguous carriers has the transmitter on it — and
        # that is done after this pass, on the pair alone, in `_resolve_groups`.
        for k, (c, q2) in enumerate(kept):
            if not state["scan"]["running"]:
                return
            with lock:
                state["scan"]["i"] = k
            q3 = qualify(c, P3_REPS, P3_WIN)
            if not q3["looks"]:
                return
            with lock:
                state["scan"]["points"].append([c, q3["lvl"], q3["feq"], 3])
            set_prog("3", (k + 1) / max(1, len(kept)))
            # AND IT IS THE PILOT ALONE THAT DECIDES, NOT THE TONE TEST. Measured, and this is
            # why: the tone test read 0.85-0.97 on 95.3 CING, 97.3, 97.7 and 101.1 — every one a
            # real station — because the measurement takes a 21 ms slice and one sustained bass
            # note can fill three bins of it. A TONE CANNOT DECIDE, because the test cannot tell
            # a sustained note inside music from a tone standing alone; THE PILOT CAN, and a sine
            # wave has no pilot at all (a synthetic tone measured -5.9 dB against a 15 dB bar).
            # So tonality is recorded, it lowers the confidence, and it never overrules the pilot.
            # 🔴 THE LABEL RESTS ON THE BEST OF THE LONG WINDOWS, AND A SHORT ONE MAY NEVER SET IT.
            # Two measured faults are corrected here, both mine:
            #   * ONE WINDOW IS A COIN TOSS — the trace in `_best_window` has 100.7 holding for four
            #     looks and then losing the station for ten, and 95.3 doing the reverse. A carrier
            #     that lands in a bad stretch is not a weak station.
            #   * A SHORT WINDOW MUST NOT DECIDE. The 6-look screen and the old 8-look second look are
            #     both shorter than the switching, and letting either win on rate alone marked
            #     carriers high off about three seconds of audio. Only a window as long as the
            #     deciding pass can set a label.
            # So a carrier still short of HIGH after its first long window is measured again. A
            # carrier that is already high costs nothing extra at all.
            windows = [q2, q3]
            best = _best_window(windows, min_looks=P3_REPS)
            if best is None or best["lock"] < int(CONF_HIGH * best["looks"] + 0.999):
                q_retry = qualify(c, P3_RETRY, P3_WIN)
                if q_retry["looks"]:
                    windows.append(q_retry)
                    best = _best_window(windows, min_looks=P3_REPS)
                    log("%.1f MHz held the pilot on %d of %d looks in the better of its two windows "
                        "(the screen read %d of %d) - this receiver switches between the station and "
                        "nothing, so the best stretch is the honest answer"
                        % (c, best["lock"], best["looks"], q2["lock"], q2["looks"]))
            if best is None or best["lock"] < PILOT_MIN_FINAL:
                rejected += 1
                log("%.1f MHz dropped in the re-check - %s" % (c, _why_not(best or q3)))
                continue
            m = best
            call, city, fmt = MARKET[c]
            stations.append(_finding(c, m, floor_lvl, call, city, fmt))
            with lock:
                state["scan"]["stations"] = list(stations)
        # The last thing before the list is finished: decide which carrier of an ambiguous pair is
        # the station, and mark or drop the other. This is where a 91.3 is settled.
        stations = _resolve_groups(stations, log)
        if stations:
            set_prog("3", 1.0)
        completed = True
        with lock:
            state["scan"]["stations"] = list(stations)
            log("pass 3 of 3 done - %d stations confirmed (%d carriers dropped) - yours to keep or "
                "discard" % (len(stations), rejected))
    finally:
        with lock:
            state["scan"]["running"] = False
            state["scan"]["phase"] = 3
            state["scan"]["stage"] = "done"
            state["scan"]["i"] = state["scan"]["n"]
        if completed:                                 # a stopped scan must not overwrite a good chart
            set_prog("1", 1.0)
            set_prog("2", 1.0)
            set_prog("3", 1.0)
            # END THE SCAN ON THE BEST STATION. A finished scan is left tuned to the first
            # HIGH-reception channel it found, so what you hear when it stops is the best thing
            # it heard. "First" means the same thing here as on the page: the list is ordered
            # strongest first, so it is the strongest high-reception one. If nothing reached
            # high reception, the strongest finding is used instead - a scan that only heard
            # low-reception stations still ends on its best one rather than on dead air at the
            # top of the band. Nothing is added to the saved list: that is still yours to press.
            if stations:
                # A STATION RANKS ABOVE ITS OWN SKIRT AND ABOVE NOTHING ELSE. Primary carriers
                # first, then how often the pilot held, then how strong the pilot stood — which is
                # the order a listening test established when 91.1 sounded much better than
                # the 91.3 that this scan had ranked first.
                ranked = sorted(stations, key=lambda s: (0 if s.get("primary", True) else 1,
                                                         -s["rate"], -(s["pilot"] or 0)))
                high = [s for s in ranked if s["conf"] == "high"]
                pick = (high or ranked)[0]
                tune(pick["f"])
                with lock:
                    log("scan finished - left tuned to %.1f MHz (%s, %s reception) to play"
                        % (pick["f"], pick.get("call") or "unknown", "high" if high else "low"))
            _save_last_scan()
        # NOT stopping the capture: the sound meter needs it continuously, and it makes no sound.
        done_ev.set()
        # Give the button back exactly as he left it - unless he pressed it while the scan was
        # running, in which case HIS last press is the one that stands and we do not undo it.
        if _last_mute_press < started_at:
            set_mute(was_muted)
            if not was_playing:                       # including a playback he had stopped
                play(False)
        ensure_play()


# ------------------------------------------------------------------------ page
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>127.0.0.1:8150</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
/* 🔴 THE BACKGROUND IS THE SITE'S OWN COLOUR, AND ITS TREATMENT IS THE FAMILY'S — the same warm
   near-black and the same three gradients that fade to nothing (a gradient wash, never bands),
   so the app and its page read as one product rather than two. The accents come from the same
   palette for the same reason: a warm background with cool blue chrome looks like two designs. */
:root{--bg:#140f08;--fg:#f7f0e4;--dim:#a3907a;--acc:#f5a524;--card:#221a0d;--line:#3a2c14;--warn:#fbbf24}
*{box-sizing:border-box}body{margin:0;color:var(--fg);
background-color:var(--bg);
background-image:radial-gradient(1100px 520px at 78% -6%,rgba(245,165,36,.13),transparent 72%),
linear-gradient(158deg,rgba(180,105,12,.12),transparent 58%),
linear-gradient(18deg,rgba(120,70,8,.1),transparent 52%);
background-attachment:fixed;
font:16px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;padding:22px}
h1{font-size:15px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin:0 0 18px}
.wrap{max-width:860px;margin:0 auto}
.freq{font-size:84px;font-weight:700;letter-spacing:-.03em;text-align:center;margin:6px 0 2px}
.freq span{font-size:26px;color:var(--dim);margin-left:6px}
.meter{display:flex;align-items:center;gap:10px;max-width:560px;margin:0 auto 12px}
.meter canvas{width:100%;height:26px;display:block}
.meter span{min-width:54px;text-align:right;font-size:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin:14px 0}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;
padding:12px 18px;font-size:16px;cursor:pointer}
button:hover{border-color:#5a4520}button.big{font-size:22px;padding:12px 26px}
button.on{background:var(--acc);color:#1a1103;border-color:var(--acc);font-weight:700}
input{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;
padding:12px;font-size:18px;width:110px;text-align:center}
.pgh{color:var(--fg);font-size:13px;max-width:780px;margin:14px auto 0;text-align:left;line-height:1.5}
.prog{max-width:780px;margin:12px auto 2px}
.pgrow{display:grid;grid-template-columns:1fr 300px 54px;gap:10px;align-items:center;
color:var(--dim);font-size:12px;padding:3px 0}
.pgt{display:block;height:8px;background:var(--card);border-radius:99px;overflow:hidden}
.pgt i{display:block;height:100%;width:0;background:var(--acc);transition:width .35s}
.pgrow.now{color:var(--fg)}.pgrow.now .pgl{font-weight:700}.pgv{text-align:right}
@media(max-width:800px){.pgrow{grid-template-columns:1fr 120px 46px}}
.list{display:flex;gap:8px;flex-wrap:wrap}
.chip{background:var(--card);border:1px solid var(--line);border-radius:99px;padding:8px 14px;cursor:pointer}
.chip:hover{border-color:var(--acc)}.chip.cur{border-color:var(--acc);color:var(--acc);font-weight:700}
.chip.hi{border-color:#8a6320}.chip.lo{opacity:.55}
.chip x{color:var(--dim);margin-left:8px}.chip .cl{color:var(--dim);font-style:normal;font-size:12px;margin-left:6px}
/* 🔴 A LIST, NOT A BAG OF CHIPS: frequency, call sign and the genres, one
   station per line, in frequency order. The frequency column is a fixed width and the digits are
   tabular so the Decimal points line up down the page and the list can be read against the dial. */
.stlist{display:flex;flex-direction:column;gap:6px}
/* 🔴 THE TWO LISTS ARE STYLED BY ID, NOT BY A SECOND CLASS, AND THAT IS DELIBERATE. They were
   given a list class beside `list`, and the file was then written at 13:48:07 on 13 October 2026
   by something that put the class back to plain `list` — the exact moment the wrapping returned.
   An id cannot be lost that way, and an id outranks `list`'s `flex-wrap` on specificity, so the
   rows stay one to a line whatever the container's class says. */
#sthi,#stlo{display:flex;flex-direction:column;flex-wrap:nowrap;gap:6px;align-items:stretch}
#sthi .strow,#stlo .strow{width:100%}
.strow{display:grid;grid-template-columns:62px 118px minmax(0,1fr) auto;gap:10px;align-items:center;
background:var(--card);border:1px solid var(--line);border-left:3px solid var(--line);border-radius:10px;
padding:8px 12px;cursor:pointer;font-size:14px}
.strow:hover{border-color:var(--acc);border-left-color:var(--acc)}
.strow.cur{border-color:var(--acc);border-left-color:var(--acc)}
.strow.hi{border-left-color:#8a6320}
.strow.lo{opacity:.62}
.strow .fq{font-variant-numeric:tabular-nums;font-weight:700;letter-spacing:.2px}
.strow .cs{color:#f2c078;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.strow .gn{display:flex;gap:6px;flex-wrap:wrap;min-width:0}
.strow .gt{background:#2a2013;border:1px solid #3d2f18;border-radius:99px;padding:1px 9px;
font-size:12px;font-style:normal;color:var(--dim);white-space:nowrap}
.strow .ev{color:var(--dim);font-size:12px;text-align:right;white-space:nowrap}
/* THE VOLUME SLIDER. It sets the RADIO's own capture level, not the machine's speaker volume, so
   nothing else on this box changes when it moves. */
.vols{align-items:center;gap:12px}
.vols .vl{color:var(--dim);font-size:14px;white-space:nowrap}
.vols input[type=range]{flex:1;min-width:220px;height:26px;background:transparent;
accent-color:var(--acc);cursor:pointer}
.vols .vv{color:var(--dim);font-size:13px;font-variant-numeric:tabular-nums;min-width:46px;
text-align:right}
.note{color:var(--dim);font-size:13px;text-align:center;margin-top:6px}
h2{font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin:26px 0 10px}
#cv{width:100%;height:200px;display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;cursor:crosshair}
.legend{display:flex;gap:18px;justify-content:center;color:var(--dim);font-size:12px;margin-top:8px}
#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:26px;background:#221a0d;
border:1px solid var(--acc);color:var(--fg);padding:12px 20px;border-radius:12px;font-size:15px;
opacity:0;transition:opacity .3s;pointer-events:none;max-width:520px;text-align:center;z-index:9}
#toast.show{opacity:1}
#mo{position:fixed;inset:0;background:rgba(10,7,3,.72);display:flex;align-items:center;
justify-content:center;z-index:10}#mo[hidden]{display:none}
#mo .box{background:#221a0d;border:1px solid #3d2f18;border-radius:14px;padding:22px 24px;max-width:420px}
#mo h3{margin:0 0 8px;font-size:17px}#mo p{margin:0 0 16px;color:var(--dim);font-size:14px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
</style></head><body><div class="wrap">
<h1>FM radio &middot; __ADDR__</h1>
<div class="freq" id="f">98.1<span>MHz FM</span></div>
<div class="meter"><canvas id="mt"></canvas><span class="note" id="mdb"></span></div>
<div class="note" id="s">starting…</div>
<div class="row">
  <button class="big" onclick="step(-0.2)">&#9664;</button>
  <button class="big" id="p" onclick="togglePlay()">Play</button>
  <button class="big" id="m" onclick="toggleMute()">Mute</button>
  <button class="big" id="sv" onclick="toggleSave()">Save</button>
  <button class="big" onclick="step(0.2)">&#9654;</button>
</div>
<div class="row vols">
  <span class="vl">Volume</span>
  <input id="vol" type="range" min="0" max="100" step="1" value="100"
         oninput="onVol(this.value)" onchange="onVol(this.value,1)">
  <span class="vv" id="volv">100%</span>
</div>
<h2>Saved stations</h2><div class="list" id="pr"></div>
<h2>Scan &middot; three passes &middot; about ten minutes &middot; a scan clears the saved list</h2>
<div class="row"><button id="sc" onclick="scan()">Scan the band</button></div>
<div id="ph" class="pgh"></div>
<div id="pg" class="prog"></div>
<canvas id="cv" onclick="clickChart(event)"></canvas>
<div class="legend">
  <span><i class="sw" style="background:#f5a524"></i>pass 1 &mdash; every channel, once</span>
  <span><i class="sw" style="background:#e8c07d"></i>pass 2 &mdash; best frequency at each marker</span>
  <span><i class="sw" style="background:#ffd166"></i>pass 3 &mdash; quality, least static</span>
  <span><i class="sw" style="background:#ff4d4d"></i>red line = station found</span>
  <span>high reception = 9 dB or more above the band middle, low = the rest</span>
</div>
<h2 id="hhi">High reception</h2><div class="list" id="sthi"></div>
<h2 id="hlo">Low reception</h2><div class="list" id="stlo"></div>
<h2>Scan log</h2><div class="list" id="lg"></div>
</div>
<div id="toast"></div>
<div id="mo" hidden><div class="box"><h3 id="mh">Scan in progress</h3><p id="mm"></p>
<div class="row"><button onclick="mAns(0)">Cancel</button>
<button class="on" id="mok" onclick="mAns(1)">Stop the scan and continue</button></div></div></div>
<script>
let S={}, seenLog=0, mResolve=null;
// Measured on this receiver, stations land 6-12 dB above the band middle; 9 dB is the
// boundary between one that comes in clearly and one that only just makes it.
const REC_HIGH=9;
async function j(u){const r=await fetch(u);return r.json()}
function fmt(f){return (+f).toFixed(1)}
function modal(msg,always,okLabel){
  if(!always && !(S.scan&&S.scan.running)) return Promise.resolve(true);
  document.getElementById('mh').textContent=(S.scan&&S.scan.running)?'Scan in progress':'Please confirm';
  document.getElementById('mm').textContent=msg;
  const ok=document.getElementById('mok');
  if(ok) ok.textContent=okLabel||((window.__guardStop===false)?'Continue':'Stop the scan and continue');
  document.getElementById('mo').hidden=false;
  return new Promise(r=>mResolve=r);
}
function mAns(v){document.getElementById('mo').hidden=true;const r=mResolve;mResolve=null;if(r)r(!!v)}
async function guard(msg,fn,stopScan=true,always=false,okLabel=null){
  window.__guardStop=stopScan;
  if(!(await modal(msg,always,okLabel))) return;
  if(stopScan&&S.scan&&S.scan.running){ await j('/api/scan?stop=1'); }
  await fn();
}
function toast(msg){
  const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(toast._t);toast._t=setTimeout(()=>t.classList.remove('show'),7000);
}
function render(){
  document.getElementById('f').innerHTML=fmt(S.freq)+'<span>MHz FM</span>';
  // 🔴 THE FREQUENCY BOX AND THE TUNE BUTTON ARE GONE, and this line is why
  // they were useless: it wrote the CURRENT frequency into the box on every refresh, so a number
  // typed in it was overwritten within a second and Tune then tuned to the frequency he was
  // already on. Nothing happened, every time. Tuning is now the arrows, the chart, or a station
  // line — all of which move the dial to somewhere it is not already.
  const p=document.getElementById('p');p.textContent=S.want_playing?'Stop':'Play';
  p.className='big'+(S.want_playing?' on':'');
  const mb=document.getElementById('m');mb.textContent=S.muted?'Unmute':'Mute';
  mb.className='big'+(S.muted?' on':'');
  // 🔴 SAVE BECOMES UNSAVE, AND LIGHTS UP, FOR A STATION ALREADY IN THE LIST.
  // The button now answers "is this one saved?" by itself, the same way Play and Mute do — you do
  // not have to read the list to find out.
  const sv=document.getElementById('sv');
  const isSaved=(S.saved||[]).some(f=>Math.abs(f-S.freq)<0.05);
  sv.textContent=isSaved?'Unsave':'Save';
  sv.className='big'+(isSaved?' on':'');
  // The slider follows the server unless it is the thing being dragged, so a value set here is
  // never yanked back mid-drag.
  const vol=document.getElementById('vol');
  if(vol&&document.activeElement!==vol&&S.volume!=null&&+vol.value!==+S.volume){
    vol.value=S.volume;
    document.getElementById('volv').textContent=S.volume+'%';
  }
  document.getElementById('pr').innerHTML=(S.saved||[]).map(f=>
    '<div class="chip'+(Math.abs(f-S.freq)<0.05?' cur':'')+'" onclick="tune('+f+')">'+fmt(f)+
    '<x onclick="event.stopPropagation();save('+f+',1)">&#10005;</x></div>').join('')
    ||'<span class="note">none yet — scan or save one</span>';
  const q=S.scan||{}, lg=q.log||[];
  if(lg.length>seenLog){ toast(lg[lg.length-1]); seenLog=lg.length; }
  document.getElementById('lg').innerHTML=lg.slice(-4).map(t=>'<span class="note">'+t+'</span>').join(' ')||'<span class="note">no scan yet</span>';
  progress(q);
  // 🔴 SAY SO WHEN THE RADIO IS NOT THERE. The audio server does not fail when a USB input is
  // unplugged — it moves the stream to the DEFAULT input, which is the microphone — so the meter
  // would read the room, a scan would sweep the room, and the speakers would play the room back.
  // The server now refuses to capture in that state; this is where it gets said out loud, so the
  // list on screen is never mistaken for a live band.
  const noRadio = (S.input_ok === false);
  document.getElementById('s').textContent = noRadio
    ? 'no radio connected — nothing is at ' + (S.device || '/dev/radio0') +
      ', so nothing is captured, nothing is measured and nothing plays. The list below is the last scan.'
    : (q.running
    ? q.stage+'  ·  '+q.i+' / '+q.n+'  ·  '+(S.muted
        ? 'muted by the scan — press Unmute to listen while it runs'
        : 'sound on — the scan is obeying the button')
    : (S.muted ? 'muted — click Unmute to listen'
      : (q.points&&q.points.length
        ? 'last scan: '+(q.stations||[]).length+' stations  ·  band middle '+(q.lvl_base!=null?q.lvl_base:'?')+' dB'
        : (S.playing?'playing through your speakers':'stopped'))));
  // HIGH vs LOW reception. HIGH means the receiver HELD THE PILOT on most looks AND the carrier
  // stands over the band middle this scan measured; the lock decides, the level only separates
  // two stations that both hold it. (The old level-only form is kept as a fallback for a
  // finding saved before the pilot test existed.)
  const base=(q.lvl_base!=null)?q.lvl_base:null;
  const strong=s=>(s.conf==='high')||(s.conf==null&&base!=null&&s.db!=null&&(s.db-base)>=REC_HIGH);
  // 🔴 THE STATION COMES BEFORE ITS OWN SKIRT. A listening test settled it: 91.3 was
  // labelled high here and sounded poor, while 91.1 — shown as "next to" it — was much better.
  // So primary carriers lead, then how often the pilot held, then how strong it stood.
  // 🔴 A LIST IN FREQUENCY ORDER, with the frequency, the call sign and the
  // genres on each line. Frequency is the one field that never depends on a measurement — the same
  // band always reads the same way — so the list can be read against the dial and against the next
  // scan. Reception still decides WHICH list a station goes in; it no longer decides the order.
  const found=(q.stations||[]).slice().sort((a,b)=>a.f-b.f);
  const hi=found.filter(strong), lo=found.filter(s=>!strong(s));
  // A carrier's genre entry can name two genres ("campus radio/classic rock"), so the field is
  // split into a list rather than printed as one string.
  const genres=s=>(s.fmt||'').split('/').map(t=>t.trim()).filter(Boolean);
  const rows=a=>a.map(s=>{
    const ev=[];
    if(s.lock!=null) ev.push('held '+s.lock+'/'+s.looks);
    if(s.db!=null) ev.push(s.db+' dB');
    if(s.adj) ev.push('next to '+s.adj);
    else if(s.near) ev.push('near '+s.near);
    const g=genres(s);
    return '<div class="strow '+(strong(s)?'hi':'lo')+(Math.abs(s.f-S.freq)<0.05?' cur':'')+
      '" title="'+((s.call||'')+(s.city?' - '+s.city:'')+(s.fmt?' - '+s.fmt:'')+
      (s.lock!=null?'  ·  station held on '+s.lock+' of '+s.looks+' looks':'')+
      (s.pilot!=null?'  ·  pilot '+s.pilot+' dB':'')+
      (s.above!=null?'  ·  '+s.above+' dB over the band middle':'')+
      (s.adj?'  ·  within 0.4 MHz of a louder carrier ('+s.adj+' MHz), so the call sign may belong to that station':'')+
      (s.snd!=null&&s.looks?'  ·  the audio read as programme on '+s.snd+' of '+s.looks+' looks':'')+
      (s.tonal?'  ·  a sustained note dominates some looks, so this one is trusted less':''))+
      '" onclick="tune('+s.f+')">'+
      '<span class="fq">'+fmt(s.f)+'</span>'+
      '<span class="cs">'+(s.call?s.call.split('/')[0]:'\u2014')+'</span>'+
      '<span class="gn">'+(g.length?g.map(t=>'<i class="gt">'+t+'</i>').join('')
        :'<i class="gt">genre unknown</i>')+'</span>'+
      '<span class="ev">'+ev.join(' \u00b7 ')+'</span></div>';
  }).join('');
  document.getElementById('hhi').textContent='High reception \u00b7 '+hi.length;
  document.getElementById('hlo').textContent='Low reception \u00b7 '+lo.length;
  document.getElementById('sthi').innerHTML=rows(hi)||'<span class="note">none</span>';
  document.getElementById('stlo').innerHTML=rows(lo)||'<span class="note">none</span>';
  document.getElementById('sc').textContent=q.running?'Stop the scan':'Scan the band  (3 passes)';
  draw(q.points||[],q.stations||[],q.lvl_base,q.running);
}
// WHICH PASS, AND HOW FAR. Each pass counts the measurements it actually makes, so the bars
// are real work rather than a clock. The total gives each pass one third.
const PASS_DESC={
 1:'a quick sweep of <b>every channel</b>, 88.1 to 107.9 MHz. It only listens for what is '
  +'loud - it does not judge quality yet.',
 2:'it goes to every LICENSED carrier the sweep flagged and looks at it six times. A station is '
  +'only reported at a frequency that really has a station on it, and it has to show its '
  +'<b>19 kHz stereo pilot</b> - that is how the scan tells a station from static, and a pure '
  +'tone has no pilot at all.',
 3:'it re-checks each surviving carrier <b>fourteen times</b> and keeps the ones whose pilot '
  +'keeps locking. Fourteen, because this receiver loses a station and finds it again for '
  +'seconds at a time - a shorter look can land entirely in the gap.'};
function progress(q){
  const p=q.prog||{}, run=!!q.running;
  const L=[['1','Pass 1 \u00b7 sweep every channel'],
           ['2','Pass 2 \u00b7 measure the licensed carriers'],
           ['3','Pass 3 \u00b7 re-check each carrier']];
  const C={1:'#f5a524',2:'#e8c07d',3:'#ffd166'};
  let h='';
  for(const [k,label] of L){
    const f=Math.max(0,Math.min(1,p[k]||0)), now=run&&String(q.phase)===k;
    h+='<div class="pgrow'+(now?' now':'')+'"><span class="pgl">'+label+'</span>'
     +'<span class="pgt"><i style="width:'+(f*100).toFixed(1)+'%;background:'+C[k]+'"></i></span>'
     +'<span class="pgv">'+Math.round(f*100)+'%</span></div>';
  }
  const t=Math.max(0,Math.min(1,p.total||0));
  h+='<div class="pgrow tot"><span class="pgl">Total \u00b7 all three passes</span>'
   +'<span class="pgt"><i style="width:'+(t*100).toFixed(1)+'%;background:#f7f0e4"></i></span>'
   +'<span class="pgv">'+Math.round(t*100)+'%</span></div>';
  document.getElementById('pg').innerHTML=h;
  let line;
  if(run&&(q.stage||'').indexOf('floor')>=0){
    line='<b>Now: pass 1 of 3</b> \u2014 the sweep is done. It is measuring the noise floor on '
      +'ten quiet channels, which sets the bar a station has to beat.';
  } else if(run){
    line='<b>Now: pass '+q.phase+' of 3</b> \u2014 '+PASS_DESC[q.phase]+' ('+q.i+' of '+q.n+' done)';
  } else if((q.points||[]).length){
    line='<b>Scan finished</b> \u2014 '+(q.stations||[]).length+' stations found. Press \uff0b on a '
      +'finding to keep it. A finding is only listed when the receiver held the 19 kHz pilot on '
      +'most looks, so static and tones are not on it.';
  } else {
    line='<b>Nothing scanned yet</b> \u2014 press Scan the band. Three passes, about eight minutes: '
      +'a fast sweep, then six looks at every licensed carrier, then <b>fourteen looks</b> at each '
      +'survivor on the 19 kHz stereo pilot. It is slow on purpose: this receiver loses a station '
      +'and finds it again for seconds at a time, so a short look can land entirely in the gap.';
  }
  document.getElementById('ph').innerHTML=line;
}
function X(f,w){return (f-88)/20*w}
function draw(pts,st,base,run){
  const c=document.getElementById('cv'),dpr=window.devicePixelRatio||1;
  const w=c.clientWidth,h=210,pad=16;c.width=w*dpr;c.height=h*dpr;
  const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
  const DY=h-pad, LO=-40, HI=-10;
  const YL=l=>DY-Math.max(0,Math.min(HI-LO,l-LO))/(HI-LO)*(DY-8);
  const thr=(base!=null)?base+4:null;
  x.strokeStyle='#3a2c14';x.fillStyle='#a3907a';x.font='10px system-ui';x.lineWidth=1;
  for(let f=88;f<=108;f+=1){                        // 1 MHz minors, 2 MHz labelled
    const px=Math.round(X(f,w))+.5;x.strokeStyle=(f%2)?'#241b0e':'#3a2c14';
    x.beginPath();x.moveTo(px,0);x.lineTo(px,DY);x.stroke();
    if(!(f%2))x.fillText(f,px+3,h-4);
  }
  x.strokeStyle='#3a2c14';
  for(let l=LO;l<=HI;l+=10){const py=Math.round(YL(l))+.5;x.beginPath();x.moveTo(0,py);x.lineTo(w,py);x.stroke();
    x.fillText(l+' dB',3,py-3);}
  if(thr!=null){x.strokeStyle='#fbbf24';x.setLineDash([4,4]);const by=Math.round(YL(thr))+.5;
    x.beginPath();x.moveTo(0,by);x.lineTo(w,by);x.stroke();x.setLineDash([]);}
  const ramp=(a,b)=>{const g=x.createLinearGradient(0,DY,0,0);g.addColorStop(0,a);g.addColorStop(1,b);return g};
  const G1=ramp('#3a2c14','#f5a524'), G2=ramp('#2f2413','#e8c07d'), G3=ramp('#3d2c14','#ffd166');
  const bw1=Math.max(3,w/100*0.92);  // pass 1 samples every real channel, so its bars are narrow
  const bw2=Math.max(2,w/200*1.04);  // passes 2 and 3 sit exactly on the real channels
  for(const p of pts){
    const f=p[0],lvl=p[1],pass=p[3];
    if(lvl<-90)continue;
    const px=X(f,w),py=YL(lvl);
    const bw = pass===1?bw1:bw2;
    x.globalAlpha = (thr!=null&&lvl<thr)?0.42:1;      // below the bar, fade back
    x.fillStyle = pass===3?G3:(pass===2?G2:G1);
    x.fillRect(px-bw/2,py,bw,DY-py);
  }
  x.globalAlpha=1;
  // while a scan runs the marker follows the pen, so the sweep is visible instead of
  // frozen at wherever the radio was last left; otherwise it is where you are tuned
  const cur=(run&&pts.length)?pts[pts.length-1][0]:(S.freq||98.1);
  x.strokeStyle='#f7f0e4';x.lineWidth=1.5;
  const cx=Math.round(X(cur,w))+.5;
  x.beginPath();x.moveTo(cx,0);x.lineTo(cx,DY);x.stroke();x.lineWidth=1;
  x.fillStyle='#f7f0e4';x.font='bold 10px system-ui';
  x.fillText(fmt(cur),Math.min(w-32,Math.max(2,cx+3)),12);
  x.lineWidth=1.5;x.font='9px system-ui';                    // a red line marks each station found
  for(const s of st){
    const px=Math.round(X(s.f,w))+.5;
    // high reception in full red, low reception dimmed, so the two categories read at a glance
    const strong=(base!=null&&s.db!=null&&(s.db-base)>=REC_HIGH);
    x.strokeStyle=strong?'#ff4d4d':'#7d3a3a';
    x.beginPath();x.moveTo(px,0);x.lineTo(px,DY);x.stroke();
    x.fillStyle=strong?'#ff8a8a':'#9c6a6a';
    x.fillText(fmt(s.f),Math.min(w-30,Math.max(2,px-11)),22);
  }
  x.lineWidth=1;
}
function clickChart(e){
  const c=document.getElementById('cv'),r=c.getBoundingClientRect();
  tune(Math.min(108,Math.max(88,88+((e.clientX-r.left)/r.width)*20)).toFixed(1));
}
async function tick(){S=await j('/api/state');render()}
async function tune(f){await guard('Do you want to stop the scan and tune to '+fmt(f)+' MHz?',async()=>{await j('/api/tune?f='+encodeURIComponent(f));await tick()})}
async function step(d){await tune((+S.freq+d).toFixed(1))}
async function togglePlay(){await guard(S.want_playing?'Stop playback while the scan runs?':'Start playback while the scan runs?',async()=>{await j('/api/play?on='+(S.want_playing?0:1));await tick()})}
// SILENCE IS INSTANT, SOUND ASKS. Muting must never need a second click — the whole point
// of the button is to make it quiet now. Unmuting brings audio out of speakers with no
// warning, so it confirms first, at rest as well as mid-scan.
async function toggleMute(){
  if(!S.muted){await j('/api/mute?on=1');await tick();return}
  await guard('Turn the sound on for '+fmt(S.freq)+' MHz, through the speakers?',
    async()=>{await j('/api/mute?on=0');await tick()},false,true,'Turn the sound on');
}
async function save(f,del){await guard((del?'Remove ':'Save ')+fmt(f)+' MHz while the scan runs?',async()=>{await j('/api/save?f='+f+(del?'&del=1':''));await tick()})}
// ONE BUTTON FOR BOTH, because the button's own label already says which it will do.
function toggleSave(){
  const isSaved=(S.saved||[]).some(f=>Math.abs(f-S.freq)<0.05);
  return save(S.freq, isSaved?1:0);
}
// THE VOLUME SLIDER. Sent while it is dragged but not on every pixel — the audio should follow the
// hand, not the mouse — and once more when the drag ends so the final value always lands.
let volTimer=null;
function onVol(v,now){
  v=Math.max(0,Math.min(100,Math.round(+v)));
  document.getElementById('volv').textContent=v+'%';
  const send=()=>fetch('/api/volume?v='+v).catch(()=>{});
  clearTimeout(volTimer);
  if(now) send(); else volTimer=setTimeout(send,160);
}
async function scan(){await j('/api/scan'+(S.scan&&S.scan.running?'?stop=1':'?start=1'));tick()}
// THE SOUND METER: a live level you can watch while you listen. The reading is polled
// cheaply and then animated here, so the bars move smoothly between readings rather than
// jumping once a second. High reads amber, the rest use the accent - no green anywhere.
let mDb=-60,mTgt=-60,mPk=-60;
function drawMeter(){
  const c=document.getElementById('mt'),el=document.getElementById('mdb');
  if(!c)return;
  const dpr=window.devicePixelRatio||1,w=c.clientWidth||480,h=26;
  if(c.width!==Math.round(w*dpr)){c.width=Math.round(w*dpr);c.height=Math.round(h*dpr)}
  const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
  const N=44,gap=2,sw=Math.max(2,(w-gap*(N-1))/N),lo=-40,hi=-6;
  const lit=Math.round(Math.max(0,Math.min(1,(mDb-lo)/(hi-lo)))*N);
  const pl=Math.round(Math.max(0,Math.min(1,(mPk-lo)/(hi-lo)))*N);
  for(let i=0;i<N;i++){
    const hot=i>=N-8;
    x.globalAlpha=(i<lit)?1:0.12;
    x.fillStyle=hot?'#ffd166':'#f5a524';
    x.fillRect(i*(sw+gap),0,sw,h);
  }
  if(pl>0){x.globalAlpha=1;x.fillStyle='#fbbf24';x.fillRect((pl-1)*(sw+gap),0,sw,3)}
  if(el) el.textContent=(mDb>-90)?mDb.toFixed(0)+' dB':'';
}
function meterLoop(){
  mDb+=(mTgt-mDb)*0.4;                          // rise quickly...
  if(mTgt>mPk) mPk=mTgt; else mPk=Math.max(mTgt,mPk-0.6);   // ...peak falls slowly
  drawMeter();
  requestAnimationFrame(meterLoop);
}
async function meterPoll(){
  try{const r=await j('/api/meter'); if(r&&r.db>-90) mTgt=r.db}catch(e){}
  setTimeout(meterPoll,140);
}
meterLoop();meterPoll();
// poll ~2.5x a second while sweeping so bars arrive one at a time, and gently at rest
async function loop(){await tick();setTimeout(loop,(S&&S.scan&&S.scan.running)?400:1500)}
loop();
// Closing the tab must not leave the radio playing. The sound is held by the audio
// server, not by this page, so the page says goodbye on the way out; the service has a
// watchdog for a browser that dies without getting the chance.
addEventListener('pagehide',e=>{if(!e.persisted)navigator.sendBeacon('/api/bye')});
</script></body></html>"""


def page_html():
    """The page, with the address it is actually being served on written into the heading."""
    host = "localhost" if BIND in ("0.0.0.0", "", "::") else BIND
    return PAGE.replace("__ADDR__", "%s:%d" % (host, PORT))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """🔴 THE MISSING HALF OF "STOP WHEN THE TAB CLOSES" (measured 13 October 2026).

        The page has always said goodbye on the way out with
        `navigator.sendBeacon('/api/bye')` — and sendBeacon sends a **POST**. This server only
        ever implemented `do_GET`, so every one of those goodbyes was answered with
        `501 Unsupported method ('POST')` and the handler never ran once. The radio was therefore
        silenced by the fallback watchdog alone, which waited THREE MINUTES. The body is ignored:
        a beacon carrying no data is exactly the case this handles.
        """
        self.do_GET()

    def do_GET(self):
        global _last_state_check, _cached_playing, _cached_input, _last_mute_press, _last_view
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/":
            return self._send(page_html(), "text/html")
        if p == "/api/meter":
            _last_view = time.time()                   # this poll counts as a page watching
            return self._send(json.dumps({"db": state["meter"].get("db")}))
        if p == "/api/state":
            _last_view = time.time()                   # a page is open and watching
            if time.time() - _last_state_check > 4:    # spawning pactl on every poll slows the scan
                _last_state_check = time.time()
                ensure_play()
                _cached_playing = playing()
                _cached_input = source_exists()
                if not state["scan"]["running"]:
                    live = _read_tuner()               # the tuner can move without us
                    if live:
                        with lock:
                            state["freq"] = live
            with lock:
                snap = json.loads(json.dumps(state))
            snap["playing"] = _cached_playing
            snap["input_ok"] = _cached_input
            snap["device"] = DEVICE
            snap["saved"] = saved()
            return self._send(json.dumps(snap))
        if p == "/api/tune":
            if "f" in q:
                try:
                    f = float(q["f"])
                except ValueError:
                    f = state["freq"]
                tune(min(108.0, max(87.5, f)))
            ensure_play()
            return self._send(json.dumps({"freq": state["freq"]}))
        if p == "/api/play":
            return self._send(json.dumps({"playing": play(q.get("on", "1") == "1")}))
        if p == "/api/mute":
            _last_mute_press = time.time()             # a real button press, not the scan's own
            muted = set_mute(q.get("on", "1") == "1")
            return self._send(json.dumps({"muted": muted, "playing": playing()}))
        if p == "/api/save":
            try:
                f = round(float(q.get("f", "0")), 1)
            except ValueError:
                f = 0
            if f:
                if q.get("del") == "1":
                    save_saved([x for x in saved() if x != f])
                else:
                    keep_station(f)          # adding a finding - refuses a near-duplicate
            return self._send(json.dumps({"saved": saved()}))
        if p == "/api/volume":
            return self._send(json.dumps({"volume": set_volume(q.get("v", "90"))}))
        if p == "/api/scan":
            with lock:
                running = state["scan"]["running"]
            if q.get("stop") == "1" and running:
                with lock:
                    state["scan"]["running"] = False
            elif q.get("start") == "1" and not running:
                threading.Thread(target=scan_worker, daemon=True).start()
            return self._send(json.dumps({"ok": True}))
        if p == "/api/bye":
            # The tab is closing. Nothing is watching, so nothing should be audible: the
            # sound is held by the audio server and closing the page does NOT stop it.
            # 🔴 THIS IS NOT A MUTE THE USER CHOSE, SO IT DOES NOT GO THROUGH set_mute(). That
            # writes the muted flag to disk, which would leave the radio silent the next time a
            # page was opened, as though he had pressed Mute himself. Closing a tab means "stop
            # now"; opening a page again gets the sound back, because `want_playing` is untouched.
            ensure_mute()
            return self._send(json.dumps({"muted": True}))
        self.send_error(404)


def _view_watch():
    """Sound only while a page is open.

    The PipeWire loopback that carries the radio to the speakers belongs to the audio
    server, so it keeps playing after the page is closed — which is how the radio was
    heard with no tabs open. `/api/bye` covers the normal close; this is the fallback for
    a browser that never got to say it.
    """
    while True:
        time.sleep(5)
        if time.time() - _last_view > NO_VIEW_MUTE:
            ensure_mute()


def meter_worker():
    """A live level for the page's sound meter, so you can SEE the audio while you listen.

    Capturing makes no sound - it is the same read the scan does, run continuously - and the
    reading is damped here (quick to rise, slow to fall) so the bars move like a meter
    instead of flickering with every sample block.
    """
    while True:
        if not STREAM.start():
            time.sleep(3)
            continue
        try:
            now = STREAM.now()
            lvl, _ = STREAM.stats(now - 11000, now)      # about 0.11 s of audio
            if lvl is not None and lvl > -90:
                with lock:
                    prev = state["meter"].get("db")
                    db = lvl if (prev is None or lvl > prev) else prev * 0.6 + lvl * 0.4
                    state["meter"] = {"db": round(db, 1)}
        except Exception:
            pass
        time.sleep(0.09)


if __name__ == "__main__":
    print("FM radio page on http://%s:%d/" % (BIND, PORT), flush=True)
    threading.Thread(target=_view_watch, daemon=True).start()
    threading.Thread(target=meter_worker, daemon=True).start()
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
