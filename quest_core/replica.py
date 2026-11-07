"""Which replica this process is: "A", "B", "C", ...

Docker Compose scaling starts copies of one service, named <prefix>1, <prefix>2,
<prefix>3 (e.g. esd-a3-quest-app-1). A container is not told its own number, so
we look it up: Docker's DNS knows each copy's name and IP address, and the copy
whose IP matches ours is us. Number 1 becomes "A", 2 becomes "B", and so on.

REPLICA_DNS_PREFIX (set in docker-compose.yml) is the name before the number.
REPLICA_ID, if set, is used as-is instead (e.g. when running outside Docker).
Outside Docker with neither set, the replica is "local".
"""
import os
import socket
import string


def _find_replica_id():
    if os.environ.get("REPLICA_ID"):
        return os.environ["REPLICA_ID"]
    prefix = os.environ.get("REPLICA_DNS_PREFIX")
    if not prefix:
        return "local"
    try:
        own_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        return "local"
    for number in range(1, 27):
        try:
            if socket.gethostbyname(f"{prefix}{number}") == own_ip:
                return string.ascii_uppercase[number - 1]
        except OSError:
            continue  # no copy with this number (or not in DNS yet)
    return "local"


REPLICA_ID = _find_replica_id()
