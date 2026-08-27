# Installation

Setup for **macOS** and **Linux**. Everything runs inside a project-local Python
virtual environment — no changes to your system Python.

## Requirements

- Python 3.9 or newer (for the standard-library `zoneinfo` module).
- A Webex Personal Access Token (see [Get a Webex token](#get-a-webex-token)).

---

## 1. Get Python

### macOS

Use Homebrew or a virtual environment only — do not modify or install packages
against the system Python.

Install Homebrew if you don't have it (https://brew.sh), then:

```bash
brew install python
```

### Linux

Use your distribution's Python 3 and the `venv` module. For example:

```bash
# Debian / Ubuntu
sudo apt install python3 python3-venv

# Fedora
sudo dnf install python3
```

Verify you have 3.9+:

```bash
python3 --version
```

---

## 2. Create the virtual environment and install dependencies

From the project directory:

```bash
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

All later commands use `./venv/bin/python`, which keeps this project isolated
from your system and any other Python projects.

> If you ever see `ZoneInfoNotFoundError`, your OS is missing its IANA timezone
> database. Uncomment the `tzdata` line in `requirements.txt` and re-run the
> install step above.

---

## 3. Configure

Copy the two example files and fill them in:

```bash
cp settings.yaml.example settings.yaml
cp credentials.txt.example credentials.txt
chmod 600 credentials.txt
```

Edit **`settings.yaml`**:

```yaml
email: you@example.com          # the address you post under in Webex
timezone: America/New_York      # your IANA timezone
message_char_limit: 500         # max chars shown per message in the report
session_gap_minutes: 90         # gap (minutes) that starts a new session
```

`settings.yaml` and `credentials.txt` are gitignored and never committed.

### Get a Webex token

1. Sign in at https://developer.webex.com.
2. Copy your **Personal Access Token** from the Getting Started / documentation
   page.
3. Paste it — and nothing else — into `credentials.txt`.

These tokens expire roughly every 12 hours, so you'll paste in a fresh one before
each run. The tool warns when the token file looks stale and reports an
authentication failure clearly if the token has expired.

---

## 4. Run

```bash
# Fetch the current month's activity into data/, then render the summary:
./venv/bin/python fetcher.py
./venv/bin/python report.py
```

This produces `webex-summary-<month>.txt` in the project directory. To target a
specific period instead of the current month:

```bash
./venv/bin/python fetcher.py --month 2026-08
./venv/bin/python report.py  --month 2026-08
```

See [README.md](README.md) for what the tool does and how the data is structured.
