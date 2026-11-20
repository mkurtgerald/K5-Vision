"""Public diagnostic child; no product, network, identity, or camera access."""
from pathlib import Path
import sys
import threading


def wait_for_owned_probe(ready):
    ready.write_bytes(b'owned-baseline-ready\n')
    threading.Event().wait(timeout=30)


if __name__ == '__main__':
    wait_for_owned_probe(Path(sys.argv[1]))
