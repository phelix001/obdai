# OBDAI

**AI-assisted OBD2 diagnostics for your car.** Plug a cheap ELM327 adapter into your
car's OBD2 port, and let an LLM (Claude or OpenAI) read the live data and help you
figure out what's wrong — and what it'll cost to fix.

Two tools, one shared engine:

| Product | Launcher | What it is |
|---|---|---|
| **OBDAI OneShot** | `./run.sh` (or `./oneshot.sh`) | Guided, start-to-finish diagnosis that produces a **structured report** — most likely problem, cheapest fix, and clickable parts + how-to links. |
| **OBDAI CarChat** | `./2run.sh` (or `./carchat.sh`) | An **interactive chat** with an AI mechanic while you work on the car. It reads the car on demand, looks at photos you attach, and remembers the session. |

Both run against a real adapter **or a built-in simulator** (`--simulate`, no hardware
needed), and both let you choose Claude or OpenAI at startup.

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

The simulator replays a real car's data so you can see both tools work before you
ever plug in:

```bash
./2run.sh --simulate                 # CarChat, simulated 2010 Audi A4
./2run.sh --simulate --sim-car honda # simulate a different car
./run.sh  --simulate                 # OneShot, full simulated diagnosis
```

## Connect a real adapter

Ignition must be **ON** (engine running if you want live sensor data — the adapter is
powered by the OBD2 port).

```bash
./run.sh          # OneShot
./2run.sh         # CarChat
```

- **USB:** just plug it in — it auto-detects `/dev/ttyUSB*` / `/dev/ttyACM*` and the baud rate.
- **Bluetooth (Linux):** pair the adapter once (`bluetoothctl` → `scan on` / `pair` /
  `trust`), then run normally — OBDAI finds a bound `/dev/rfcomm*`, or offers to bind a
  paired adapter for you.
- **Force a specific port:** `--port /dev/rfcomm0`
- **Adapter not showing up?** Diagnose it: `venv/bin/python obd_connect.py`

On the first run of a session, OBDAI reads the car's **VIN**, identifies the vehicle,
and asks you to confirm or correct it — so it never assumes the wrong car.

---

## OBDAI OneShot — `run.sh`

A single guided pass that ends in a saved report:

1. Asks your symptoms.
2. Reads a baseline snapshot + trouble codes + on-board monitors (Mode 06, incl. catalyst) + manufacturer data.
3. AI decides whether a short **live capture** would help, then walks you through it (idle → rev → oil-cap test, etc.).
4. Produces a **diagnosis**: most likely problem, estimated cost, cheapest fix, clickable RockAuto/NAPA parts, and a YouTube how-to.
5. Saves the report to `reports/` and a per-vehicle record to `history/` so it can compare across visits.

```bash
./run.sh                                   # real car
./run.sh --simulate                        # demo
./run.sh --vehicle "2015 Honda Accord 2.4" # override the vehicle label
./run.sh --diagnose-file pending_XXXX.json # produce a diagnosis offline from a saved capture
```

## OBDAI CarChat — `2run.sh`

A conversation with an AI mechanic that can **read the car and look at photos** as you
talk. You drive; it pulls data when a reading would settle the question.

The assistant's tools: current sensor values · trouble codes (stored/pending/permanent) ·
Mode 06 monitors + readiness · VAG/Audi manufacturer data · **VIN read + validation** ·
guided live capture.

**Photos** — show it a part, a connector, a leak, or another scanner's screen:

| Command | What it does |
|---|---|
| `/pic` | attach the newest photo from your watch folders (phone → sync → `/pic`) |
| `/pic <file>` | attach a specific image (`~` and wildcards work) |
| `/photos` then `/pic 3` | list the 10 newest photos, attach one by number |
| `/snap` | grab a frame from a webcam (or the device camera on Android) |
| `/phone` | pull the newest photo off a USB-attached Android (adb) |
| `/help` | show all commands |

A bare image path typed in a normal message is attached automatically. (A friendlier,
tap-to-attach mobile UI is on the roadmap — see below.)

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

- **Android app (APK)** with a touch UI — Bluetooth **and** USB-OTG adapters, and
  tap-to-attach photos instead of slash commands. (Build artifacts stay out of this
  repo.)

## Privacy & safety

- **No keys in this repository.** They live only in your local `.env` (gitignored).
- Live data, reports, session transcripts, and photos stay on your machine
  (`reports/`, `history/`, `sessions/` — all gitignored). The only thing sent off-box
  is what you send to your chosen AI provider during a diagnosis or chat.
- Diagnoses are advisory. Verify before you buy.

## License

[MIT](LICENSE) © 2026 phelix001
