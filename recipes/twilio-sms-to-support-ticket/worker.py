"""Run the durable SMS outbox worker with environment-supplied credentials."""

import os
import signal
import time
from threading import Event

from delivery import Outbox
from sender import TicketSender


def run(box: Outbox, sender: TicketSender, stop: Event) -> None:
    """Finish each bounded request, then stop without claiming another batch."""
    while not stop.is_set():
        box.work(sender, time.time(), limit=1)
        stop.wait(0.5)


if __name__ == "__main__":
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    box = Outbox(os.environ["TICKET_STATE_DB"])
    sender = TicketSender(os.environ["SUPPORT_TICKET_URL"], os.environ["SUPPORT_TOKEN"])
    run(box, sender, stop)
