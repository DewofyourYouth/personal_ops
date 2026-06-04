import re

STATUS_ICONS = {
    "open": "⌛",
    "done": "✅",
    "missed": "❌",
}

PREFIXES = {
    "insight:":    "#insight",
    "hypothesis:": "#hypothesis",
    "checkin":     "#checkin",
    "task:":       "#task",
    "note:":       "#note",
    "did:":        "#win",
    "habit:":      "#habit",
    "wrong:":      "#wrong",
    "backlog:":    "#backlog",
    "someday:":    "#backlog",
    "food:":       "#food",
    "ate:":        "#food",
    "ate ":        "#food",
    "skip:":       "#skip",
    "excuse:":     "#skip",
    "excused:":    "#skip",
    "values:":     "#values",
    "value:":      "#values",
}

ENCOURAGEMENTS = [
    "Look at you, a functioning adult!",
    "Your future self just breathed a sigh of relief.",
    "Scientists confirm: doing things is better than not doing things. 🧪",
    "Task defeated 🤺. It never stood a chance.",
    "You absolute legend. Probably.",
    "Gold star 🌟. Imaginary, but still.",
    "This is going straight to your permanent record. The good one. 📓",
    "Somewhere a productivity guru is shedding a single tear of joy. 🥲",
    "Your mom would be proud. Assuming she cares about task management.",
    "That task is dead. You killed it. No regrets. 🪦",
    "Wow. Just... wow. (Keep going.)",
    "The dopamine was real. Ride it. 👊",
]




