import os
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TOKEN = os.environ["OPS_BOT_TOKEN"]
ALLOWED_USER = int(os.environ["OPS_CHAT_ID"])
cwd = os.getcwd()
LOG_DIR = os.path.expanduser(f"{cwd}/ops/log")

print(LOG_DIR)

os.makedirs(LOG_DIR, exist_ok=True)

PREFIXES = {
    "insight:": "#insight",
    "hypothesis:": "#hypothesis",
    "checkin": "#checkin",
    "task:": "#task",
    "note:": "#note",
}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return

    text = update.message.text.strip()
    now = datetime.now().strftime("%H:%M")
    lower = text.lower()

    tag = "#log"
    content = text
    for prefix, t in PREFIXES.items():
        if lower.startswith(prefix):
            tag = t
            content = text[len(prefix):].strip()
            break

    log_file = os.path.join(LOG_DIR, f"{datetime.now().date()}.md")
    entry = f"\n## {now} {tag}\n{content}\n"

    with open(log_file, "a") as f:
        f.write(entry)

    await update.message.reply_text(f"Logged {tag} ✓")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()