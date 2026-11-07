#!/usr/bin/env python3
import socket
import sys
import time

def wait_for_port(port, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"Port {port} not ready after {timeout_s}s")

if __name__ == "__main__":
    port = int(sys.argv[1])
    timeout = float(sys.argv[2])
    wait_for_port(port, timeout)
