# Tsunami Alert Notifier

Polls the NTWC and PTWC Atom feeds published by [tsunami.gov](https://www.tsunami.gov/?page=productRetrieval)
and broadcasts new tsunami warnings, watches, advisories, and cancellations to
a Meshtastic channel. Information statements are not sent.

The Atom feeds provide structured product IDs, update times, event details,
and bulletin/CAP links. The notifier uses the Atom product category for
filtering and includes the bulletin link in each message; it does not scrape
the web page or verify CAP digital signatures. Cancellation products are
included so a previously broadcast threat can be explicitly cleared.

- NTWC (Alaska, continental U.S. and Canada): <https://www.tsunami.gov/events/xml/PAAQAtom.xml>
- PTWC (Hawaii, Pacific and Caribbean regions): <https://www.tsunami.gov/events/xml/PHEBAtom.xml>

## Setup

```powershell
cd C:\Users\franc\scripts\tsunami
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` for your Meshtastic node and channel. The default centers are
`ntwc,ptwc`; set `TSUNAMI_CENTERS=ntwc` or `ptwc` to monitor only one center.
Keep `.env` private.

The first run records current warnings, watches, and advisories without
broadcasting them. Set `SEND_INITIAL_ALERTS=true` to send current alerts on
startup. Afterward, `state.json` prevents duplicate notifications.

## Run

Fetch once:

```powershell
python tsunami.py --once
```

Poll continuously (every five minutes by default):

```powershell
python tsunami.py
```

Set `LOG_LEVEL=DEBUG` in `.env` for troubleshooting. The channel must be from
0 through 7, and the polling interval must be at least 30 seconds.
