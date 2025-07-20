import os
import requests
from datetime import datetime, timezone
from flask import Flask, redirect, request, session, url_for, render_template, jsonify
from dotenv import load_dotenv
from flask_cors import CORS
import json

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

load_dotenv()

app = Flask(__name__)
CORS(app, origins=["https://bloxpanel.github.io"], supports_credentials=True)
app.secret_key = os.getenv("SECRET_KEY")

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
API_BASE_URL = "https://discord.com/api"


def parse_roblox_date(date_str):
    """
    Parse Roblox ISO8601 date string (e.g. 2020-01-01T00:00:00.000Z) into
    an aware datetime object (UTC timezone).
    """
    if not date_str:
        return None
    try:
        # Try parsing with strptime and adding timezone info manually
        dt_naive = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        return dt_naive.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            # fallback to fromisoformat with replacement
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            return None


@app.route("/")
def home():
    return jsonify({"message": "Flask backend is running"})

@app.route("/api/user")
def get_user():
    user = session.get("user")
    if user:
        return jsonify({"logged_in": True, "username": user["username"]})
    return jsonify({"logged_in": False})


@app.route("/api/player")
def api_player():
    username = request.args.get("username")
    if not username:
        return jsonify({"error": "No username provided"}), 400

    user_data = get_roblox_user_data(username)
    if not user_data:
        return jsonify({"error": "User not found or failed to fetch data"}), 404

    return jsonify(user_data)


@app.route("/login")
def login():
    discord_login_url = (
        f"{API_BASE_URL}/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"
    )
    return redirect(discord_login_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    print("🔁 /callback route hit")

    if not code:
        print("❌ No code received from Discord.")
        return "Missing code", 400

    print("🔑 Authorization code received:", code)

    # Exchange code for token
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": "identify",
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        token_res = requests.post(f"{API_BASE_URL}/oauth2/token", data=data, headers=headers)
        token_res.raise_for_status()
        token_json = token_res.json()
        access_token = token_json.get("access_token")
        print("✅ Access token retrieved:", access_token)
    except Exception as e:
        print("❌ Failed to exchange token:", str(e))
        return "Token exchange failed", 400

    if not access_token:
        print("❌ Access token missing in token response.")
        return "Failed to get access token", 400

    # Fetch user info
    try:
        user_res = requests.get(
            f"{API_BASE_URL}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_res.raise_for_status()
        user = user_res.json()
        print("👤 Discord user info fetched:", user)
    except Exception as e:
        print("❌ Failed to fetch user info:", str(e))
        return "Failed to fetch user info", 400

    session["user"] = user

    try:
        send_login_log(user)
        print("📨 Webhook log sent successfully.")
    except Exception as e:
        print("❌ Failed to send webhook:", str(e))

    # Add token to frontend URL
    return redirect(f"https://bloxpanel.github.io/?token={access_token}")


def send_login_log(user):
    try:
        username = f"{user['username']}#{user.get('discriminator', '0000')}"
        user_id = user['id']
        avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{user['avatar']}.png"

        embed = {
            "title": "🔐 New Login",
            "description": f"**{username}** just logged into the dashboard.",
            "color": 0x3498db,
            "thumbnail": {"url": avatar_url},
            "fields": [
                {"name": "User ID", "value": user_id, "inline": True},
                {"name": "Locale", "value": user.get("locale", 'Unknown'), "inline": True}
            ]
        }

        payload = {"embeds": [embed]}
        headers = {"Content-Type": "application/json"}

        response = requests.post(DISCORD_WEBHOOK_URL, headers=headers, data=json.dumps(payload))
        response.raise_for_status()
        print("✅ Webhook POST status:", response.status_code)
    except Exception as e:
        print("❌ Exception in send_login_log():", str(e))

@app.route("/check-access", methods=["GET"])
def check_access():
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return jsonify({"allowed": False, "error": "Missing or invalid auth header"}), 401

    token = auth_header.split(" ")[1]

    # Fetch user info using the token
    try:
        user_res = requests.get(
            f"{API_BASE_URL}/users/@me",
            headers={"Authorization": f"Bearer {token}"}
        )
        user_res.raise_for_status()
        user = user_res.json()
        user_id = user.get("id")
    except Exception as e:
        print("❌ Error validating token:", str(e))
        return jsonify({"allowed": False, "error": "Token validation failed"}), 401

    # Load allowed users list
    try:
        with open("allowed_users.json", "r") as f:
            allowed_users = json.load(f).get("allowedUsers", [])

        is_allowed = user_id in allowed_users
        return jsonify({"allowed": is_allowed, "user": user})
    except Exception as e:
        print("❌ Error reading whitelist:", str(e))
        return jsonify({"allowed": False, "error": "Server error"}), 500



@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/roblox", methods=["GET", "POST"])
def roblox_lookup():
    if request.method == "POST":
        username = request.form.get("username")
        if not username:
            return jsonify({"error": "Username is required"}), 400

        # Step 1: Get user ID
        res = requests.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username]},
            headers={"Content-Type": "application/json"},
        )

        if res.status_code != 200 or not res.json().get("data"):
            return jsonify({"error": "User not found"}), 404

        user = res.json()["data"][0]
        user_id = user["id"]

        # Step 2: Get account info
        acc_info = requests.get(f"https://users.roblox.com/v1/users/{user_id}").json()
        created_at = acc_info.get("created")

        # Calculate account age safely
        created_date = parse_roblox_date(created_at)
        now = datetime.now(timezone.utc)
        if created_date is None:
            account_age_days = "N/A"
        else:
            account_age_days = (now - created_date).days

        # Step 3: Get friends count
        friends_data = requests.get(
            f"https://friends.roblox.com/v1/users/{user_id}/friends/count"
        ).json()
        friends_count = friends_data.get("count", "N/A")

        # Step 4: Get avatar image URLs
        avatar_url = None
        avatar_bust_url = None

        thumb_headshot = requests.get(
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png&isCircular=true"
        ).json()

        thumb_bust = requests.get(
            f"https://thumbnails.roblox.com/v1/users/avatar-bust?userIds={user_id}&size=420x420&format=Png"
        ).json()

        if thumb_headshot.get("data"):
            avatar_url = thumb_headshot["data"][0].get("imageUrl")

        if thumb_bust.get("data"):
            avatar_bust_url = thumb_bust["data"][0].get("imageUrl")

        return jsonify(
            {
                "name": user["name"],
                "accountAge": account_age_days,
                "friends": friends_count,
                "followers": "N/A",  # Add logic if needed
                "following": "N/A",  # Add logic if needed
                "voiceChat": "Not Eligible",  # Add voiceChat API if needed
                "safeChat": "Disabled",  # Add logic if needed
                "language": "en-us",
                "avatarUrl": avatar_url,
                "avatarBustUrl": avatar_bust_url,
            }
        )

    return render_template("roblox.html")


@app.route("/discord", methods=["GET", "POST"])
def discord_info():
    return render_template("discord.html")


def get_roblox_user_data(username):
    try:
        user_lookup_url = "https://users.roblox.com/v1/usernames/users"
        response = requests.post(
            user_lookup_url, json={"usernames": [username], "excludeBannedUsers": False}
        )

        if response.status_code != 200:
            print("Failed to get user ID")
            return None

        data = response.json()
        if not data.get("data"):
            print("Username not found")
            return None

        user = data["data"][0]
        user_id = user["id"]

        profile_response = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
        if profile_response.status_code != 200:
            print("Failed to fetch profile info")
            return None

        profile = profile_response.json()

        thumbnail_response = requests.get(
            f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
        )
        thumbnail_data = thumbnail_response.json()
        avatar_url = ""
        if thumbnail_data.get("data") and len(thumbnail_data["data"]) > 0:
            avatar_url = thumbnail_data["data"][0].get("imageUrl", "")

        created_str = profile.get("created")
        created_date = parse_roblox_date(created_str)
        if created_date:
            account_age = (datetime.now(timezone.utc) - created_date).days
        else:
            account_age = "-"

        return {
            "username": profile.get("name"),
            "display_name": profile.get("displayName"),
            "id": user_id,
            "created": created_str,
            "accountAge": account_age,
            "description": profile.get("description"),
            "avatarBustUrl": avatar_url,
            "friends": "No active Logic",  # placeholder
            "followers": "No active Logic",  # placeholder
            "following": "No active Logic",  # placeholder
            "voiceChat": "No active Logic",
            "safeChat": "No active Logic",
            "language": "No active Logic",
        }

    except Exception as e:
        print(f"Exception occurred: {e}")
        return None


@app.route("/details", methods=["GET"])
def details():
    username = request.args.get("username")
    if not username:
        return render_template("details.html", error="No username provided")

    user_data = get_roblox_user_data(username)

    if not user_data:
        return render_template("details.html", error="Failed to load user data", username=username)

    return render_template("details.html", user=user_data)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    return render_template("settings.html")


if __name__ == "__main__":
    app.run(debug=True)
