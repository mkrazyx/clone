import re

from pymongo import MongoClient

from config import DBNAME, DEFAULTS, LOGGER, MONGODB

# Each clone keeps its data in a MongoDB "location": URL + database + collection.
# By default that is the manager's MongoDB, a database named after the bot, and a "users" collection.
# The owner can point any clone somewhere else from the manager. The clone's settings ("config"
# collection) live in the same database as its users.
DEFAULT_COLLECTION = "users"

mongo = MongoClient(MONGODB, serverSelectionTimeoutMS=10000)
clones = mongo[DBNAME]["clones"]  # one document per clone: token, name, and where its data lives
_clients = {MONGODB: mongo}       # one MongoClient per MongoDB URL


def client_for(url):
    if url not in _clients:
        _clients[url] = MongoClient(url, serverSelectionTimeoutMS=10000)
    return _clients[url]


def mask_url(url):
    """Hide the password of a MongoDB URL so it can be shown in chat."""
    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", url)


# ───────────────────────── clones ─────────────────────────
def clone_id(username):
    """The clone's ID (and default database name) is its bot username, lowercase."""
    return str(username).lstrip("@").strip().lower()


def get_clone(bot_id):
    return clones.find_one({"_id": bot_id})


def get_clones():
    return list(clones.find())


def default_database(bot_id):
    return bot_id if bot_id != DBNAME else f"clone_{bot_id}"


# ───────────────────────── where a clone's data lives ─────────────────────────
def location(bot_id):
    """(MongoDB URL, database name, users collection name) of a clone."""
    item = clones.find_one({"_id": bot_id}, {"mongo_url": 1, "database": 1, "collection": 1}) or {}
    return (item.get("mongo_url") or MONGODB,
            item.get("database") or default_database(bot_id),
            item.get("collection") or DEFAULT_COLLECTION)


def uses_default_cluster(bot_id):
    return location(bot_id)[0] == MONGODB


def clone_db(bot_id):
    url, database, _ = location(bot_id)
    return client_for(url)[database]


def users_collection(bot_id):
    url, database, collection = location(bot_id)
    return client_for(url)[database][collection]


def validate_location(url, database, collection):
    """Raise ValueError with a readable message if a MongoDB location cannot be used."""
    if not re.fullmatch(r"mongodb(\+srv)?://\S+", url or ""):
        raise ValueError("The MongoDB URL must start with mongodb:// or mongodb+srv://")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,38}", database):
        raise ValueError("Database name: 1-38 letters, numbers, - or _ only.")
    if database in ("admin", "local", "config") or (url == MONGODB and database == DBNAME):
        raise ValueError("That database cannot be used.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", collection) or collection.startswith("system.") or collection == "config":
        raise ValueError("Collection name: letters, numbers, . - or _ only (not 'config').")


def check_database(name):
    """Validate a database name for /clone TOKEN NAME. Returns how many users it already holds."""
    validate_location(MONGODB, name, DEFAULT_COLLECTION)
    return mongo[name][DEFAULT_COLLECTION].estimated_document_count()


def connect_location(url, database, collection):
    """Prove a MongoDB location works, and create the collection if it does not exist yet."""
    validate_location(url, database, collection)
    client = client_for(url)
    client.admin.command("ping")  # raises if the server is unreachable or the login is wrong
    db = client[database]
    if collection not in db.list_collection_names():
        db.create_collection(collection)
    return db[collection].estimated_document_count()


# ───────────────────────── saving and deleting clones ─────────────────────────
def fill_settings(bot_id, fallback):
    """Make sure the clone's database has settings: keep what is there, fill the rest from `fallback`."""
    config = clone_db(bot_id)["config"]
    current = config.find_one({"_id": "settings"}) or {}
    current.pop("_id", None)

    # An old sarah-jisoo database keeps its shortener in a different document.
    legacy = config.find_one({"_id": "shortener_config"}) or {}
    if legacy.get("shortener") and legacy.get("shortener_api"):
        current.setdefault("shortsite", legacy["shortener"])
        current.setdefault("shortapi", legacy["shortener_api"])

    for key, value in fallback.items():
        current.setdefault(key, value)
    config.replace_one({"_id": "settings"}, current, upsert=True)


def save_clone(bot_id, token, username, display_name, database=None):
    """Register a clone (or update it). Its data location is kept when only the token changes."""
    old = clones.find_one({"_id": bot_id}) or {}
    clones.replace_one({"_id": bot_id}, {
        **old,
        "_id": bot_id,
        "token": token,
        "username": username,
        "display_name": display_name,
        "database": database or old.get("database") or default_database(bot_id),
    }, upsert=True)
    fill_settings(bot_id, DEFAULTS)
    set_info(bot_id, username, display_name)


def set_location(bot_id, url, database, collection):
    """Point a clone at another MongoDB location. Its settings move with it, unless that
    location already has settings of its own (then those are used)."""
    carried = settings(bot_id)
    item = clones.find_one({"_id": bot_id})
    clones.update_one({"_id": bot_id}, {"$set": {
        "mongo_url": None if url == MONGODB else url,  # the default URL is stored as "nothing"
        "database": database,
        "collection": collection,
    }})
    fill_settings(bot_id, carried)
    set_info(bot_id, item["username"], item["display_name"])


def set_info(bot_id, username, display_name):
    """Keep the bot's name up to date, in the manager and in the clone's own database."""
    clones.update_one({"_id": bot_id}, {"$set": {"username": username, "display_name": display_name}})
    clone_db(bot_id)["config"].update_one(
        {"_id": "info"}, {"$set": {"username": username, "name": display_name}}, upsert=True)


def delete_clone(bot_id):
    """Remove a clone. The default database is dropped. A database or collection the owner chose
    is never dropped as a whole; only this clone's users and settings are removed from it."""
    item = clones.find_one({"_id": bot_id}) or {}
    url, database, collection = location(bot_id)
    default = not item.get("mongo_url") and database == default_database(bot_id) and collection == DEFAULT_COLLECTION
    clones.delete_one({"_id": bot_id})

    client = client_for(url)
    if default:
        client.drop_database(database)
    else:
        client[database][collection].drop()
        client[database]["config"].delete_many({"_id": {"$in": ["settings", "info"]}})


# ───────────────────────── settings ─────────────────────────
def settings(bot_id):
    """All settings of a clone, with defaults filled in."""
    saved = clone_db(bot_id)["config"].find_one({"_id": "settings"}) or {}
    result = {**DEFAULTS, **saved}
    result["files"] = int(result["files"])
    result["forcesub"] = int(result["forcesub"])
    result["admins"] = [int(x) for x in result["admins"]]
    return result


def put(bot_id, **values):
    """Save one or more settings, e.g. put(bot_id, files=-100123)."""
    clone_db(bot_id)["config"].update_one({"_id": "settings"}, {"$set": values}, upsert=True)


# ───────────────────────── users ─────────────────────────
def add_user(bot_id, user_id):
    users_collection(bot_id).update_one({"_id": user_id}, {"$setOnInsert": {"_id": user_id}}, upsert=True)


def del_user(bot_id, user_id):
    users_collection(bot_id).delete_one({"_id": user_id})


def all_users(bot_id):
    return [user["_id"] for user in users_collection(bot_id).find({}, {"_id": 1}, batch_size=500)]


def user_count(bot_id):
    return users_collection(bot_id).count_documents({})


# ───────────────────────── storage stats ─────────────────────────
def storage(database):
    total = 0
    for name in database.list_collection_names():
        try:
            stats = database.command("collStats", name)
            total += stats.get("storageSize", 0) + stats.get("totalIndexSize", 0)
        except Exception:
            LOGGER.exception("Failed to read collection stats for %s", name)
    return total


def bot_storage(bot_id):
    return storage(clone_db(bot_id))


def cluster_storage():
    """Storage used on the manager's MongoDB (the manager plus every clone that uses it)."""
    names = {DBNAME}
    for item in clones.find({}, {"database": 1, "mongo_url": 1}):
        if item.get("database") and not item.get("mongo_url"):
            names.add(item["database"])
    return sum(storage(mongo[name]) for name in names)
