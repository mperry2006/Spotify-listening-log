"""
One-time helper: log in to Spotify and print a refresh token.

Run this ONCE on your own computer:
    python get_refresh_token.py

It opens your browser, you approve access, and it prints a refresh token.
Save that token as a GitHub secret (see README). Uses only the Python
standard library, so there is nothing to install.
"""

import base64
import http.server
import json
import secrets
import urllib.parse
import urllib.request
import webbrowser

# --- Fill these in from your Spotify developer dashboard ---
CLIENT_ID = "PASTE_YOUR_CLIENT_ID"
CLIENT_SECRET = "PASTE_YOUR_CLIENT_SECRET"
# Must match the Redirect URI in your Spotify app settings exactly.
REDIRECT_URI = "http://127.0.0.1:8888/callback"
# ------------------------------------------------------------

SCOPES = "user-read-recently-played user-top-read"
STATE = secrets.token_urlsafe(16)
result = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        if params.get("state", [None])[0] != STATE:
            result["error"] = "State mismatch - try again."
        elif "error" in params:
            result["error"] = params["error"][0]
        else:
            result["code"] = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Done - you can close this tab and go back to your terminal."
        self.wfile.write(f"<h2>{msg}</h2>".encode())

    def log_message(self, *args):
        pass  # keep the terminal quiet


def exchange_code(code):
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    if "PASTE_YOUR" in CLIENT_ID or "PASTE_YOUR" in CLIENT_SECRET:
        raise SystemExit("Edit CLIENT_ID and CLIENT_SECRET at the top of this file first.")

    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": STATE,
    })

    parsed = urllib.parse.urlparse(REDIRECT_URI)
    server = http.server.HTTPServer((parsed.hostname, parsed.port), CallbackHandler)

    print("Opening your browser to log in to Spotify...")
    print("If it doesn't open, paste this link into your browser:\n")
    print(auth_url, "\n")
    webbrowser.open(auth_url)

    server.handle_request()  # wait for the single redirect back
    server.server_close()

    if "error" in result:
        raise SystemExit(f"Login failed: {result['error']}")

    tokens = exchange_code(result["code"])
    print("\nSuccess! Your refresh token is:\n")
    print(tokens["refresh_token"])
    print("\nSave it as the GitHub secret SPOTIFY_REFRESH_TOKEN. Don't share it or commit it.")


if __name__ == "__main__":
    main()
