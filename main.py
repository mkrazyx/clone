"""Entry point: starts the manager, the health endpoint and every saved clone, then watches them."""
import asyncio
import time
from html import escape

from aiohttp import web

import manager
from clone import register
from config import BOTTOKEN, LOGGER, PORT
from database import get_clones, set_info
from manager import Bot, create_manager, label, notify, running, workers

WATCHDOG_INTERVAL = 60  # seconds between health checks
LIVENESS_TIMEOUT = 30   # seconds to wait for get_me()
MAX_RETRIES = 2         # restart attempts per bot, then it is left alone (use Restart in /mybots after fixing it)
MAX_BACKOFF = 3600      # never wait more than 1 hour between restart attempts


async def health(request):
    connected = manager.MANAGER.client.is_connected
    return web.json_response(
        {
            "status": "ok" if connected else "unhealthy",
            "manager": connected,
            "clones_running": sum(running(bot_id) for bot_id in workers),
            "clones_total": len(workers),
        },
        status=200 if connected else 503)


async def alive(client):
    """True only if the client is connected AND answers a real API call."""
    if not client.is_connected:
        return False
    try:
        await asyncio.wait_for(client.get_me(), LIVENESS_TIMEOUT)
        return True
    except Exception:
        return False


async def watchdog():
    """Every minute, restart the manager or any clone that stopped answering (with growing back-off)."""
    state = {}  # bot key -> [failed restarts in a row, earliest time of the next attempt]

    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)

        # The database is the source of truth: stop bots that were deleted from it (e.g. it was wiped).
        try:
            known = {item["_id"] for item in await asyncio.to_thread(get_clones)}
            for bot_id in [b for b in workers if b not in known]:
                LOGGER.warning("Clone %s is no longer in the database; stopping it", bot_id)
                await workers.pop(bot_id).stop()
                state.pop(bot_id, None)
        except Exception:
            LOGGER.exception("Could not check the clone list")

        for key, bot in [("manager", manager.MANAGER), *workers.items()]:
            if await alive(bot.client):
                state[key] = [0, 0.0]
                continue

            fails, next_try = state.setdefault(key, [0, 0.0])
            if fails >= MAX_RETRIES or time.monotonic() < next_try:
                continue  # gave up on this bot, or it is not time for the next try yet

            LOGGER.warning("%s is not responding; restarting (failed attempts so far: %s)", key, fails)
            try:
                await bot.restart()
                state[key] = [0, 0.0]
                await notify(f"🔄 <b>Auto-Restarted</b>\n\n{bot.title}")
            except Exception as e:
                fails += 1
                wait = min(WATCHDOG_INTERVAL * 2 ** fails, MAX_BACKOFF)
                state[key] = [fails, time.monotonic() + wait]
                LOGGER.exception("%s restart failed", key)
                error = escape(str(e))
                if fails < MAX_RETRIES:
                    await notify(
                        f"❌ <b>Restart Failed</b>\n\n{bot.title}\nAttempt: {fails}/{MAX_RETRIES}\n"
                        f"Next try in: {wait}s\nError: <code>{error}</code>")
                else:
                    await notify(
                        f"🛑 <b>Gave Up</b>\n\n{bot.title}\nStill failing after {MAX_RETRIES} tries.\n"
                        f"Error: <code>{error}</code>\n\nFix the problem, then press Restart in its settings (/mybots).")


async def main():
    if not BOTTOKEN:
        raise RuntimeError("BOTTOKEN environment variable is missing")

    manager_bot = create_manager()
    await manager_bot.start()

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    watch = None

    try:
        LOGGER.info("Manager started @%s", manager_bot.username)
        await notify(f"🟢 <b>Manager Online</b>\n\n{manager_bot.title}")

        # Health endpoint first, so the host sees the app as alive while the clones boot.
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()

        for item in await asyncio.to_thread(get_clones):
            bot_id = item["_id"]
            # Registered before starting, so a clone that fails at boot is still retried by the watchdog.
            worker = workers[bot_id] = Bot(f"clone_{bot_id}", item["token"], bot_id, register)
            try:
                await worker.start()
                await asyncio.to_thread(set_info, bot_id, worker.username, worker.name)  # keep names fresh
                await notify(f"🟢 <b>Clone Online</b>\n\n{worker.title}")
            except Exception as e:
                LOGGER.exception("Clone %s failed to start", bot_id)
                await notify(
                    f"❌ <b>Clone Failed To Start</b>\n\n{escape(label(item))}\n"
                    f"Error: <code>{escape(str(e))}</code>\n\nThe watchdog will retry up to {MAX_RETRIES} times.")

        watch = asyncio.create_task(watchdog())
        await asyncio.Event().wait()
    finally:
        if watch:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        for worker in list(workers.values()):
            await worker.stop()
        await manager_bot.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
