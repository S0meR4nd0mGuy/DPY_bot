"""
cogs/ticket/transcript.py

Standalone helper functions for generating a text transcript of a channel's
message history, with bot messages excluded. Used by ticket.py (the cog),
but kept separate so the generation logic can be tested or reused on its own.

Output path:
    ./transcripts/transcript_{channel_name}_{ISODate}.txt
"""

import os
import re
import unicodedata
from datetime import datetime, timezone

import discord

TRANSCRIPTS_DIR = os.path.join(os.getcwd(), "transcripts")

def sanitize_file_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    cleaned = re.sub(r"[^\w\-]+", "-", normalized)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned.lower()


def get_iso_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def ensure_transcripts_dir() -> str:
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    return TRANSCRIPTS_DIR


async def fetch_all_messages(channel: discord.abc.Messageable) -> list[discord.Message]:
    messages = []
    async for message in channel.history(limit=None, oldest_first=True):
        messages.append(message)
    return messages


def filter_out_bot_messages(messages: list[discord.Message]) -> list[discord.Message]:
    return [msg for msg in messages if not msg.author.bot]


def format_message_line(msg: discord.Message) -> str:
    timestamp = msg.created_at.isoformat()
    author = str(msg.author)
    content = msg.content if msg.content else "[no text content]"

    line = f"[{timestamp}] {author}: {content}"

    if msg.attachments:
        urls = ", ".join(a.url for a in msg.attachments)
        line += f"\n  Attachments: {urls}"

    return line


def format_transcript(messages: list[discord.Message], channel: discord.abc.GuildChannel) -> str:
    header_lines = [
        f"Transcript for #{channel.name}",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Messages included: {len(messages)} (bot messages excluded)",
        "=" * 60,
        "",
    ]
    header = "\n".join(header_lines)
    body = "\n".join(format_message_line(msg) for msg in messages)

    return header + body + "\n"


def write_transcript_file(channel: discord.abc.GuildChannel, transcript_text: str) -> str:
    directory = ensure_transcripts_dir()
    filename = f"transcript_{sanitize_file_name(channel.name)}_{get_iso_date()}.txt"
    file_path = os.path.join(directory, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(transcript_text)

    return file_path

async def generate_transcript(channel: discord.abc.GuildChannel) -> str:
    all_messages = await fetch_all_messages(channel)
    human_messages = filter_out_bot_messages(all_messages)
    transcript_text = format_transcript(human_messages, channel)
    file_path = write_transcript_file(channel, transcript_text)

    return file_path