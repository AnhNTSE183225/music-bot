import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

async def test():
    client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("MUSICBOT_DISCORD_REDIRECT_URI", "https://theanh.tail38dac5.ts.net/callback")

    print(f"Testing Client ID: {client_id}")
    print(f"Testing Client Secret Length: {len(client_secret)}")
    print(f"Testing Redirect URI: {redirect_uri}")

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": "dummy_test_code",
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with httpx.AsyncClient() as client:
        r = await client.post("https://discord.com/api/oauth2/token", data=payload, headers=headers)
        print(f"\nDiscord HTTP Status: {r.status_code}")
        print(f"Discord Response Body: {r.text}\n")

        if "invalid_client" in r.text:
            print("=> RESULT: Discord rejected the DISCORD_CLIENT_SECRET or DISCORD_CLIENT_ID.")
            print("   Please verify them in Discord Developer Portal -> OAuth2.")
        elif "invalid_grant" in r.text:
            print("=> RESULT: SUCCESS! Discord validated your Client ID and Client Secret.")
        else:
            print(f"=> RESULT: Unexpected response: {r.text}")

if __name__ == "__main__":
    asyncio.run(test())
