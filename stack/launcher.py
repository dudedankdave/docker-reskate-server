"""One container for the helper services: rollover, status cards and the Discord bot.
TOOLS picks which run (default "rollover,status,bot"). Each one is its own process; if one
crashes it is restarted after a pause and the others are not touched. A bot without
DISCORD_TOKEN just prints its dry run and is not restarted."""
import os
import signal
import subprocess
import sys
import time

SERVICES = {
    "rollover": ["python3", "-I", "/app/rollover.py"],
    "status": ["python3", "-I", "/app/status.py"],
    "bot": ["python3", "-I", "/app/bot.py"],
}


def log(msg):
    print(time.strftime("%H:%M:%S"), "[tools]", msg, flush=True)


def main():
    names = [n.strip() for n in os.environ.get("TOOLS", "rollover,status,bot").split(",") if n.strip()]
    unknown = [n for n in names if n not in SERVICES]
    if unknown:
        sys.exit("unknown tool(s): %s (known: %s)" % (", ".join(unknown), ", ".join(SERVICES)))
    # a status webhook or bot token that is not set means that part has nothing to do
    if "status" in names and not os.environ.get("STATUS_WEBHOOK", "").strip():
        log("STATUS_WEBHOOK empty: status only logs the cards")
    procs, started = {}, {}
    stopping = []

    def stop(*_):
        stopping.append(1)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        for n in names:
            p = procs.get(n)
            if p is None or p.poll() is not None:
                if p is not None:
                    log("%s exited with %s" % (n, p.returncode))
                    if n == "bot" and p.returncode == 0 and not os.environ.get("DISCORD_TOKEN", "").strip():
                        names.remove(n)   # dry run done
                        break
                    time.sleep(5)
                started[n] = time.time()
                procs[n] = subprocess.Popen(SERVICES[n])
                log("started %s" % n)
        time.sleep(2)
    for p in procs.values():
        if p.poll() is None:
            p.terminate()
    for p in procs.values():
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    main()
