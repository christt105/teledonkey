"""Telegram bot that feeds links to a remote mldonkey instance.

Send it an ed2k://, magnet: or .torrent link and it runs `dllink` on mldonkey.
It also offers a few read-only commands to inspect downloads.
"""

import functools
import html
import logging
import os
import re

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from formatting import format_result_line, parse_search_results, render_downloads
from mldonkey import MLDonkeyClient, MLDonkeyError

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("teledonkey")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


TOKEN = _env("TELEGRAM_BOT_TOKEN")
ALLOWED_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "").strip())
    if x
}

mld = MLDonkeyClient(
    host=os.environ.get("MLDONKEY_HOST", "127.0.0.1"),
    port=int(os.environ.get("MLDONKEY_TELNET_PORT", "4002")),
    user=os.environ.get("MLDONKEY_USER", "admin"),
    password=os.environ.get("MLDONKEY_PASSWORD", ""),
)

# ed2k links, magnet links, and plain http(s) urls (e.g. links to .torrent files).
LINK_RE = re.compile(r"(?:ed2k://|magnet:\?|https?://)\S+", re.IGNORECASE)

HELP = (
    "🫏 <b>TeleDonkey</b> — mldonkey remote control\n\n"
    "Just send me an <b>ed2k://</b>, <b>magnet:</b> or <b>.torrent</b> link and "
    "I'll add it to the downloads.\n\n"
    "<b>Commands</b>\n"
    "/search &lt;text&gt; — search the network and pick what to download\n"
    "/downloads — show active downloads\n"
    "/cancel &lt;num&gt; — cancel a download\n"
    "/pause &lt;num&gt; — pause a download\n"
    "/resume &lt;num&gt; — resume a download\n"
    "/bw — bandwidth stats\n"
    "/raw &lt;cmd&gt; — run a raw mldonkey console command\n"
    "/help — this message"
)


def restricted(func):
    """Reject users that are not in ALLOWED_USER_IDS (open if the list is empty)."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if ALLOWED_IDS and (user is None or user.id not in ALLOWED_IDS):
            log.warning("Denied user id=%s username=%s", getattr(user, "id", "?"),
                        getattr(user, "username", "?"))
            if update.message:
                await update.message.reply_text(
                    f"⛔ Not authorized.\nYour Telegram ID is: {user.id if user else '?'}"
                )
            return
        return await func(update, context)

    return wrapper


async def _reply_html(update: Update, text: str) -> None:
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


# --- Command handlers -------------------------------------------------------


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_html(update, HELP)


@restricted
async def cmd_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        raw = await mld.view_downloads()
    except MLDonkeyError as exc:
        await _reply_html(update, f"⚠️ {html.escape(str(exc))}")
        return
    await _reply_html(update, render_downloads(raw))


@restricted
async def cmd_bw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        raw = await mld.bandwidth()
    except MLDonkeyError as exc:
        await _reply_html(update, f"⚠️ {html.escape(str(exc))}")
        return
    body = html.escape(raw) or "No data."
    await _reply_html(update, f"📊 <b>Bandwidth</b>\n<pre>{body}</pre>")


def _num_action(label: str, method_name: str):
    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args or not context.args[0].isdigit():
            await _reply_html(update, f"Usage: /{label} &lt;num&gt;")
            return
        num = int(context.args[0])
        try:
            raw = await getattr(mld, method_name)(num)
        except MLDonkeyError as exc:
            await _reply_html(update, f"⚠️ {html.escape(str(exc))}")
            return
        msg = html.escape(raw.strip()) or f"OK — {label} #{num}"
        await _reply_html(update, f"✅ <b>{label} #{num}</b>\n<pre>{msg}</pre>")

    return handler


@restricted
async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply_html(update, "Usage: /raw &lt;mldonkey console command&gt;")
        return
    command = " ".join(context.args)
    try:
        raw = await mld.run(command)
    except MLDonkeyError as exc:
        await _reply_html(update, f"⚠️ {html.escape(str(exc))}")
        return
    body = html.escape(raw)[:3800] or "(no output)"
    await _reply_html(update, f"<pre>{body}</pre>")


# --- Search -----------------------------------------------------------------

SEARCH_PER_PAGE = 5
# mldonkey can return well over a thousand hits; keep the biggest slice usable.
MAX_SEARCH_RESULTS = 60


def _render_search(state: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the message text and inline keyboard for one page of results."""
    results = state["results"]
    downloaded: set[int] = state["downloaded"]
    total = state.get("total", len(results))
    pages = max(1, (len(results) + SEARCH_PER_PAGE - 1) // SEARCH_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * SEARCH_PER_PAGE
    chunk = results[start : start + SEARCH_PER_PAGE]

    found = f"{len(results)} of {total}" if total > len(results) else f"{total}"
    lines = [
        f"🔎 <b>{html.escape(state['query'])}</b> — "
        f"{found} result(s), showing {start + 1}–{start + len(chunk)}"
    ]
    buttons = []
    for offset, r in enumerate(chunk):
        idx = start + offset + 1  # 1-based number shown to the user
        lines.append("")
        lines.append(format_result_line(idx, r))
        done = r["num"] in downloaded
        buttons.append(
            InlineKeyboardButton(
                f"{'✅' if done else '⬇️'} {idx}",
                callback_data=f"dl:{r['num']}",
            )
        )

    # Download buttons, wrapped at most 5 per row.
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    # Pagination row.
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"sp:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"sp:{page + 1}"))
    if len(nav) > 1:
        keyboard.append(nav)
    if downloaded:
        lines.append("")
        lines.append(f"<i>✅ added {len(downloaded)} so far — tap more or /downloads</i>")
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


@restricted
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply_html(update, "Usage: /search &lt;text to search&gt;")
        return
    query = " ".join(context.args)
    status = await update.message.reply_text(
        f"🔎 Searching the network for <b>{html.escape(query)}</b>…",
        parse_mode=ParseMode.HTML,
    )
    try:
        raw = await mld.search(query)
    except MLDonkeyError as exc:
        await status.edit_text(f"⚠️ {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
        return

    all_results = parse_search_results(raw)
    if not all_results:
        body = html.escape(raw.strip())[:3500] or "No results."
        await status.edit_text(
            f"🔎 No parseable results for <b>{html.escape(query)}</b>.\n<pre>{body}</pre>",
            parse_mode=ParseMode.HTML,
        )
        return

    # mldonkey lists results largest-first; keep the top slice usable as buttons.
    results = all_results[:MAX_SEARCH_RESULTS]
    state = {
        "query": query,
        "results": results,
        "total": len(all_results),
        "downloaded": set(),
    }
    context.user_data["search"] = state
    text, keyboard = _render_search(state, 0)
    await status.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


@restricted
async def on_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if data == "noop":
        await query.answer()
        return

    state = context.user_data.get("search")
    if not state:
        await query.answer("Search expired — run /search again.", show_alert=True)
        return

    if data.startswith("sp:"):
        page = int(data[3:])
        await query.answer()
        text, keyboard = _render_search(state, page)
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        return

    if data.startswith("dl:"):
        num = int(data[3:])
        try:
            await mld.download_result(num)
        except MLDonkeyError as exc:
            await query.answer(f"⚠️ {exc}", show_alert=True)
            return
        state["downloaded"].add(num)
        await query.answer("Added to downloads ✅")
        # Re-render the current page so the chosen result flips to ✅.
        page = next(
            (
                i // SEARCH_PER_PAGE
                for i, r in enumerate(state["results"])
                if r["num"] == num
            ),
            0,
        )
        text, keyboard = _render_search(state, page)
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )


@restricted
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    links = LINK_RE.findall(text)
    if not links:
        await _reply_html(update, "Send me an ed2k://, magnet: or .torrent link. /help for more.")
        return

    results = []
    for link in links:
        short = link[:50] + ("…" if len(link) > 50 else "")
        try:
            raw = await mld.add_link(link)
        except MLDonkeyError as exc:
            results.append(f"⚠️ {html.escape(short)}\n{html.escape(str(exc))}")
            continue
        note = html.escape(raw.strip()[:200])
        line = f"✅ Added <code>{html.escape(short)}</code>"
        if note:
            line += f"\n<i>{note}</i>"
        results.append(line)
    await _reply_html(update, "\n\n".join(results))


async def on_startup(app: Application) -> None:
    """Notify the allowed users that the bot just came online."""
    text = (
        "🫏 <b>TeleDonkey is online</b> ✅\n"
        f"Connected to mldonkey at <code>{mld.host}:{mld.port}</code>.\n"
        "Send a link or /help."
    )
    # Register the command list so it shows up in Telegram's "/" autocomplete menu.
    await app.bot.set_my_commands(
        [
            BotCommand("search", "Search the network (/search <text>)"),
            BotCommand("downloads", "Show active downloads"),
            BotCommand("cancel", "Cancel a download (/cancel <num>)"),
            BotCommand("pause", "Pause a download (/pause <num>)"),
            BotCommand("resume", "Resume a download (/resume <num>)"),
            BotCommand("bw", "Bandwidth stats"),
            BotCommand("raw", "Run a raw mldonkey console command (/raw <cmd>)"),
            BotCommand("help", "Show help"),
        ]
    )

    for uid in ALLOWED_IDS:
        try:
            await app.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except Exception as exc:  # don't let a bad id stop startup
            log.warning("Could not send startup message to %s: %s", uid, exc)


def main() -> None:
    if not ALLOWED_IDS:
        log.warning(
            "ALLOWED_USER_IDS is empty — the bot will accept commands from ANYONE. "
            "Set it to your Telegram user id(s)."
        )

    app = Application.builder().token(TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler(["search", "s"], cmd_search))
    app.add_handler(CommandHandler(["downloads", "dl"], cmd_downloads))
    app.add_handler(CommandHandler("bw", cmd_bw))
    app.add_handler(CommandHandler("cancel", _num_action("cancel", "cancel")))
    app.add_handler(CommandHandler("pause", _num_action("pause", "pause")))
    app.add_handler(CommandHandler("resume", _num_action("resume", "resume")))
    app.add_handler(CommandHandler("raw", cmd_raw))
    app.add_handler(CallbackQueryHandler(on_search_callback, pattern=r"^(dl|sp|noop)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("TeleDonkey starting (mldonkey %s:%s)", mld.host, mld.port)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
