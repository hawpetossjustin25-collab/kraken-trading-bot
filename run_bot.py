#!/usr/bin/env python3
"""
Supervisor: keeps the trading bot running forever.
Restarts on crash with backoff, logs restarts, writes supervisor.log.
Persistent memory lives in trade_history.json / q_table.json / bot_state.json.
"""
import os
import subprocess
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(BASE_DIR, "bot.py")
LOG = os.path.join(BASE_DIR, "supervisor.log")

def log(msg: str):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [SUPERVISOR] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def main():
    crashes = 0
    backoff = 5
    log("Supervisor started — bot will auto-restart on any exit/crash.")
    while True:
        start = time.time()
        log(f"Launching bot (crash #{crashes})")
        rc = subprocess.call([sys.executable, BOT], cwd=BASE_DIR)
        uptime = time.time() - start
        crashes += 1
        log(f"Bot exited (rc={rc}) after {uptime:.0f}s")

        if uptime > 300:
            # ran fine for a while — treat as clean stop, quick restart
            backoff = 5
            log("Clean long run — restarting in 5s")
        else:
            # crash-loop protection: exponential backoff capped at 5 min
            backoff = min(backoff * 2, 300)
            log(f"Possible crash — backoff {backoff}s before restart")

        time.sleep(backoff)

if __name__ == "__main__":
    main()
