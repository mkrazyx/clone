import logging
import os

# ──────────────── Environment variables ────────────────
# Set these on Koyeb. The defaults below are test values.
BOTTOKEN = os.getenv("BOTTOKEN", "8191103559:AAG-6navNOYM0pSAZqDYzzwsLBSXS-EsRHI")
MONGODB = os.getenv("MONGODB", "mongodb+srv://herostore:herostore@herostore.ywtvule.mongodb.net/?appName=herostore")
DBNAME = os.getenv("DBNAME", "manager")
APP_ID = int(os.getenv("APP_ID", "1736204"))
API_HASH = os.getenv("API_HASH", "890d40e0f91a4de32dec2965444b2cbe")
OWNER = int(os.getenv("OWNER", "1058015838"))
FILES = int(os.getenv("FILES", "-1001205507869"))
FORCESUB = int(os.getenv("FORCESUB", "0"))
SHORTSITE = os.getenv("SHORTSITE", "nowshort.com")
SHORTAPI = os.getenv("SHORTAPI", "c576b9cdd34ceb572a8df1f57eabf7c11efbda3a")
ADMINS = [int(x) for x in os.getenv("ADMINS", "1058015838").replace(",", " ").split()]
WORKERS = int(os.getenv("WORKERS", "4"))
PORT = int(os.getenv("PORT", "8082"))
MONGO_LIMIT_MB = int(os.getenv("MONGO_LIMIT_MB", "512"))

# Settings every new clone starts with. Each clone can then change them from the manager.
DEFAULTS = {
    "files": FILES,          # channel where the files are stored
    "forcesub": FORCESUB,    # channel users must join (0 = off)
    "shortsite": SHORTSITE,  # link shortener site ("" = off)
    "shortapi": SHORTAPI,    # link shortener API key
    "admins": ADMINS,        # users who can use the admin commands of the clone
}

ADMIN_USERNAME = "@MrKrazyBot"

START_MESSAGE = """<b>🌟 Hello {first},

I'm A File Store Bot 🤖 designed to provide files 📁 and links 🔗</b>"""

FORCE_SUB_MESSAGE = """<b>🌟 Hello {first},

You need to join our channel to use this bot.</b>"""

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
SEP = "━" * 13  # divider line under the titles

LOGGER = logging.getLogger("clonebot")
