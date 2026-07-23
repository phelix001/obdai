# OBDAI

**Chat with an AI mechanic about your car.** Plug a cheap ELM327 adapter into your
car's OBD2 port and talk to an LLM (Claude or OpenAI) that **reads the live data on
demand**, looks at **photos you attach**, and — when something's wrong — **shows you the
parts to buy with clickable RockAuto / NAPA / YouTube links** for your exact year/make/model.

It's **one app**, started two ways:

| Command | What it does |
|---|---|
| `./2run.sh` (or `./carchat.sh`) | Open the chat. Ask anything; it reads the car and diagnoses when useful. |
| `./run.sh` (or `./oneshot.sh`) | Same app, **diagnosis-first**: runs a full diagnosis immediately, then keeps chatting. |

Inside the chat the assistant can, on its own or when you ask: read sensors / trouble
codes / monitors / VIN, run a guided live capture, produce a **full structured
diagnosis** (most likely problem, cost, cheapest fix, parts + links), and **suggest
parts** for any repair. Runs against a real adapter **or a built-in simulator**
(`--simulate`), Claude or OpenAI chosen at startup.

> ⚠️ **Advisory only.** These tools help you interpret OBD2 data — they are not a
> substitute for a mechanic. Always confirm a trouble code with a second scanner
> before spending money on parts.

---

## What you need

- **Python 3.10+**
- An **ELM327 OBD2 adapter** — USB or Bluetooth, the ~$15 kind on Amazon works fine.
- An **API key** for at least one provider:
  - Anthropic / Claude — https://console.anthropic.com/  (default)
  - OpenAI (optional) — https://platform.openai.com/api-keys

## Setup

```bash
git clone https://github.com/phelix001/obdai.git
cd obdai

python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
# open .env and paste your ANTHROPIC_API_KEY (and/or OPENAI_API_KEY)
```

Your keys live only in `.env`, which is gitignored and never leaves your machine.

## Try it with no hardware

The simulator replays a real car's data so you can try it before you ever plug in:

```bash
./2run.sh --simulate                 # chat, simulated 2010 Audi A4
./2run.sh --simulate --sim-car honda # simulate a different car
./run.sh  --simulate                 # diagnosis-first, then chat
```

## Connect a real adapter

Ignition must be **ON** (engine running if you want live sensor data — the adapter is
powered by the OBD2 port).

```bash
./2run.sh         # chat
./run.sh          # diagnosis-first, then chat
```

- **USB:** just plug it in — it auto-detects `/dev/ttyUSB*` / `/dev/ttyACM*` and the baud rate.
- **Bluetooth (Linux):** pair the adapter once (`bluetoothctl` → `scan on` / `pair` /
  `trust`), then run normally — OBDAI finds a bound `/dev/rfcomm*`, or offers to bind a
  paired adapter for you.
- **WiFi / TCP:** point it at an adapter that exposes a TCP socket (the common WiFi
  ELM327s, or any `host:port` bridge): `--port tcp:192.168.0.10:35000`.
- **Force a specific port:** `--port /dev/rfcomm0`
- **Adapter not showing up?** Diagnose it: `venv/bin/python obd_connect.py`
  (`obd_connect.py --port tcp:HOST:PORT` to test a WiFi adapter).

On the first run of a session, OBDAI reads the car's **VIN**, identifies the vehicle,
and asks you to confirm or correct it — so it never assumes the wrong car.

---

## The chat — `2run.sh`

A conversation with an AI mechanic that can **read the car and look at photos** as you
talk. You drive; it pulls data when a reading would settle the question, and when it
recommends a repair it shows you **buyable parts + a how-to video**.

The assistant's tools: current sensor values · trouble codes (stored/pending/permanent) ·
Mode 06 monitors + readiness · VAG/Audi manufacturer data · **VIN read + validation** ·
guided live capture · **suggest_parts** (RockAuto/NAPA/YouTube links for your car) ·
**full_diagnosis** (a structured report: most likely problem, cost, cheapest fix, parts).

Ask "what's wrong?" and it runs a full diagnosis; say "I'm replacing the coils" and it
tells you what to check *and* hands you the parts to buy.

## Diagnosis-first — `run.sh`

The same app, but it **runs a full diagnosis the moment it connects**, then leaves you in
the chat to dig in. This is the old one-shot flow, now inside the one app.

```bash
./run.sh                                   # connect, diagnose, then chat
./run.sh --simulate                        # demo
./run.sh --port tcp:192.168.0.10:35000     # WiFi adapter
# offline: produce a diagnosis from a previously-saved capture (no hardware):
venv/bin/python obd_diagnose.py --diagnose-file pending_XXXX.json
```

Diagnoses save to `reports/` and a per-vehicle record to `history/` so the assistant can
compare across visits.

**Photos** — show it a part, a connector, a leak, or another scanner's screen:

| Command | What it does |
|---|---|
| `/pic` | attach the newest photo from your watch folders (phone → sync → `/pic`) |
| `/pic <file>` | attach a specific image (`~` and wildcards work) |
| `/photos` then `/pic 3` | list the 10 newest photos, attach one by number |
| `/snap` | grab a frame from a webcam (or the device camera on Android) |
| `/phone` | pull the newest photo off a USB-attached Android (adb) |
| `/help` | show all commands |

A bare image path typed in a normal message is attached automatically. (For a
tap-to-attach experience, use the mobile web UI below.)

### Mobile web UI (phone-friendly)

The chat also has a **web interface** — chat bubbles, and a 📎 button that opens your
camera or gallery, so you attach photos with a tap instead of slash commands. It's the
same engine underneath (same tools, same photo pipeline).

```bash
./webui.sh --simulate      # try it with no hardware
./webui.sh                 # real ELM327 (USB-OTG or Bluetooth)
# then open http://localhost:8000 in your browser
```

It's built to run **on your phone under [Termux](https://termux.dev/)** so the whole
thing — adapter, AI, UI — lives on the device you carry to the car:

```bash
pkg install python
pip install -r requirements.txt
python webui/server.py --simulate
# open http://localhost:8000 in the phone's browser; tap 📎 to add a photo
```

USB-OTG and Bluetooth adapters both work under Termux. (A packaged APK is a possible
future step; build artifacts would stay out of this repo.)

**Sessions** save automatically and resume from a picker on startup:

```bash
./2run.sh                       # pick up a past session, or start new
./2run.sh --new                 # skip the picker, start fresh
./2run.sh --session sessions/session_XXXX.json
./2run.sh --simulate --sim-car honda
```

---

## Configuration

All optional except a provider key. Set in `.env` (see `.env.example`):

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude key (default provider) |
| `OPENAI_API_KEY` | OpenAI key (optional) |
| `OPENAI_MODEL` | OpenAI model, default `gpt-4o` |
| `OBD_PHOTO_DIRS` | extra folders `/pic` scans for the newest photo (colon-separated) |

Optional system tools: **ffmpeg** for `/snap` (webcam capture), **adb** for `/phone`,
**bluez** for Bluetooth adapters.

## Running the tests

```bash
venv/bin/python -m pytest -q
```

## Vehicle support

The standard OBD2 modes (live data, DTCs, Mode 06 monitors, VIN) work on **any** OBD2
car (1996+ in the US). The manufacturer-specific extras (`audi_caeb_dids.json`: boost,
oil temp, fuel-rail pressure, misfire counters) are tuned for **VW/Audi 2.0T (CAEB)**
engines; other cars simply skip them. The simulator ships an Audi and a Honda profile.

## Roadmap

- **Packaged Android APK** — the mobile web UI already gives a touch interface with
  tap-to-attach photos (run it in Termux today); a signed, installable APK wrapper is a
  possible next step. Build artifacts would stay out of this repo.

## Privacy & safety

- **No keys in this repository.** They live only in your local `.env` (gitignored).
- Live data, reports, session transcripts, and photos stay on your machine
  (`reports/`, `history/`, `sessions/` — all gitignored). The only thing sent off-box
  is what you send to your chosen AI provider during a diagnosis or chat.
- Diagnoses are advisory. Verify before you buy.

## License

[MIT](LICENSE) © 2026 phelix001
