import os
import webbrowser
from threading import Timer
from app import app

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))

def open_browser():
    webbrowser.open_new(f"http://{HOST}:{PORT}/")

if __name__ == "__main__":
    Timer(1.5, open_browser).start()
    app.run(host=HOST, port=PORT, debug=False)
