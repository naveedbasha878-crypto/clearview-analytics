import socket
import threading

import webview
from waitress import create_server

from app import app


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    port = available_port()
    server = create_server(app, host="127.0.0.1", port=port, threads=8)
    threading.Thread(target=server.run, daemon=True).start()
    webview.create_window(
        "Clearview Analytics",
        f"http://127.0.0.1:{port}/",
        width=1280,
        height=850,
        min_size=(960, 640),
    )
    try:
        webview.start()
    finally:
        server.close()


if __name__ == "__main__":
    main()
