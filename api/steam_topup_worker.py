"""Общий stateless worker пилота, отдельно от очередей магазинов."""
import os
import signal
import time
import logging
from domains.steam_topups import SteamTopups


def main():
    stopped = False

    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    service = SteamTopups(lambda: os.environ['DATABASE_URL'])
    while not stopped:
        try:
            service.process_once()
        except Exception:
            logging.error('Steam topup worker: storage unavailable')
        time.sleep(1)


if __name__ == '__main__':
    main()
