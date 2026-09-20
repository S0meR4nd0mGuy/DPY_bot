import discord
import discord as dc
from discord.ext import commands as c
from discord import app_commands as ac, Embed
import asyncio
import json
import os
from pathlib import Path
import sys

from pymongo import MongoClient
from pymongo.errors import CollectionInvalid, OperationFailure

config_path = Path(os.getcwd()) / "data" / "config.json"

with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

DEV_GUILD_ID = config.get("devGuildId")
DEV_USER_IDS = set(str(u) for u in config.get("devUserId", []))

# NOTE: move this connection string (and the tokens in config.json) into
# environment variables / a secrets file that isn't committed anywhere.
# Anything pasted into a chat or a repo should be treated as compromised
# and rotated.
client = MongoClient(config.get("mongoUrl"))
db = client["Guilds"]

def _build_schema(required: list, properties: dict, additional_properties: bool = True) -> dict:
    """
    Build a MongoDB $jsonSchema validator dict.

    required            -> list of required field names, e.g. ["name", "email"]
    properties          -> dict of field_name -> BSON type spec, e.g.
                            {"name": {"bsonType": "string"}, "age": {"bsonType": "int"}}
    additional_properties -> whether fields outside `properties` are allowed
    """
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": required,
            "properties": properties,
            "additionalProperties": additional_properties,
        }
    }


def _apply_schema(collection_name: str, schema: dict, validation_level: str = "strict",
                  validation_action: str = "error") -> None:
    """
    Apply (or create-with) a schema on a collection.

    validation_level  -> "off" | "moderate" | "strict"
    validation_action -> "error" (reject invalid docs) | "warn" (log only)
    """
    try:
        db.create_collection(collection_name, validator=schema,
                             validationLevel=validation_level,
                             validationAction=validation_action)
        print(f"[created] '{collection_name}' with schema.")
    except CollectionInvalid:
        # Collection already exists -> update its validator instead
        db.command({
            "collMod": collection_name,
            "validator": schema,
            "validationLevel": validation_level,
            "validationAction": validation_action,
        })
        print(f"[updated] '{collection_name}' schema.")


def _get_schema(collection_name: str) -> dict:
    """Return the current validator for a collection, if any."""
    info = db.command("listCollections", filter={"name": collection_name})
    batch = info.get("cursor", {}).get("firstBatch", [])
    if not batch:
        return {}
    return batch[0].get("options", {}).get("validator", {})


def _drop_schema(collection_name: str) -> None:
    """Remove validation from a collection (sets an empty validator)."""
    db.command({
        "collMod": collection_name,
        "validator": {},
        "validationLevel": "off",
    })
    print(f"[dropped schema] '{collection_name}'")


def _validate_document(collection_name: str, document: dict) -> tuple[bool, str]:
    """
    Dry-run check: try inserting then rolling back, to see if a document
    would pass the schema. Returns (is_valid, message).
    Note: this actually inserts + deletes; only use for testing.
    """
    coll = db[collection_name]
    try:
        result = coll.insert_one(document)
        coll.delete_one({"_id": result.inserted_id})
        return True, "valid"
    except OperationFailure as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# GUILD CONFIG SCHEMA (Discord bot) — mirrors the mongoose IGuildConfig shape
# ---------------------------------------------------------------------------

def _guild_config_schema() -> dict:
    """
    Equivalent of:
        interface IGuildConfig {
            guildId: string;
            logs: { moderation: { enabled: boolean; channelId: string; } }
        }
    Here `guildId` IS the document's `_id` (rather than a separate field),
    so a guild's config is looked up directly by primary key:
        db.guild_configs.find_one({"_id": guild_id})
    Plus createdAt/updatedAt, mirroring mongoose's `{ timestamps: true }`.
    """
    return _build_schema(
        required=["_id"],
        properties={
            "_id": {"bsonType": "string", "description": "Discord guild (server) ID — used as the primary key"},

            "logs": {
                "bsonType": "object",
                "properties": {
                    "moderation": {
                        "bsonType": "object",
                        "properties": {
                            "enabled": {"bsonType": "bool"},
                            "channelId": {"bsonType": ["string", "null"]},
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },

            "createdAt": {"bsonType": "date"},
            "updatedAt": {"bsonType": "date"},
        },
        additional_properties=False,
    )


def _default_guild_config(guild_id: str) -> dict:
    """Default document to insert for a newly-seen guild. Add fields here as your schema grows."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return {
        "_id": str(guild_id),
        "logs": {
            "moderation": {
                "enabled": False,
                "channelId": None,
            },
        },
        "createdAt": now,
        "updatedAt": now,
    }


def _get_guild_config(guild_id: str) -> dict:
    """Fetch a guild's config by its guildId (== _id), creating a default one if it doesn't exist yet."""
    coll = db["guild_configs"]
    doc = coll.find_one({"_id": str(guild_id)})
    if doc is None:
        doc = _default_guild_config(guild_id)
        coll.insert_one(doc)
    return doc


def _update_guild_config(guild_id: str, updates: dict) -> None:
    """Merge-update fields in a guild's config (dot-notation supported for nested fields,
    e.g. {"logs.moderation.enabled": True, "logs.moderation.channelId": "123"})."""
    from datetime import datetime, timezone
    updates["updatedAt"] = datetime.now(timezone.utc)
    db["guild_configs"].update_one(
        {"_id": str(guild_id)},
        {"$set": updates},
        upsert=True,
    )

def _reset_guild_config(guild_id: str) -> dict:
    """
    Reset a guild's config back to defaults, keeping only its _id.

    Use this in on_guild_join — it doesn't matter whether the bot has never
    seen this guild before or has stale leftover data from a previous stay
    (e.g. it was kicked and re-invited): replace_one with upsert=True just
    overrides whatever is there (or inserts fresh if nothing is there).
    """
    fresh = _default_guild_config(guild_id)
    db["guild_configs"].replace_one({"_id": str(guild_id)}, fresh, upsert=True)
    return fresh

def _has_non_default_values(guild_id: str) -> bool:
    """
    Check whether a guild's config has any customized (non-default) values.
    Ignores _id and the createdAt/updatedAt timestamps, since those are
    expected to differ naturally and aren't "settings".

    Returns True  -> at least one setting differs from the default
    Returns False -> everything is still at default (or no doc exists at all)
    """
    coll = db["guild_configs"]
    doc = coll.find_one({"_id": str(guild_id)})
    if doc is None:
        return False  # no config yet = effectively default

    default = _default_guild_config(guild_id)

    ignored_keys = {"_id", "createdAt", "updatedAt"}
    current_stripped = {k: v for k, v in doc.items() if k not in ignored_keys}
    default_stripped = {k: v for k, v in default.items() if k not in ignored_keys}

    return current_stripped != default_stripped


def _delete_guild_config(guild_id: str) -> bool:
    """
    Remove a guild's config doc entirely from the DB (called when the bot
    leaves/is removed from a guild). Returns True if a doc was actually
    deleted, False if there was nothing to delete.
    """
    result = db["guild_configs"].delete_one({"_id": str(guild_id)})
    return result.deleted_count > 0

intents = dc.Intents.default()
intents.members = True
intents.presences = True
intents.guilds = True
bot = c.Bot(command_prefix='!', intents=intents)


def _is_dev_user(user_id) -> bool:
    return str(user_id) in DEV_USER_IDS


@bot.event
async def on_guild_join(guild: dc.Guild):
    try:
        await guild.owner.send(content=f"<@{guild.owner_id}>", embed=Embed(description=f"Thanks for Inviting me to your Server!", color=dc.Color.green()))
    except discord.Forbidden:
        print(f"Failed to DM {guild.owner} (ID: {guild.owner_id})")

    if db.guild_configs.find_one({"_id": str(guild.id)}) is None:
        db.guild_configs.insert_one(_default_guild_config(str(guild.id)))
    if db.guild_configs.find_one({"_id": str(guild.id)}) is not None:
        _has_non_default_val = _has_non_default_values(str(guild.id))
        if _has_non_default_val:
            _reset_guild_config(str(guild.id))


@bot.event
async def on_guild_remove(guild: dc.Guild):
    """
    Fires when the bot leaves a guild (kicked, banned, or manually removed).
    NOTE: discord.py's event is called `on_guild_remove`, not
    `on_guild_leave` — there is no `on_guild_leave` hook in discord.py.

    Cleans up by deleting that guild's config doc from the DB entirely,
    rather than just resetting it to defaults (as on_guild_join does).
    """
    try:
        deleted = _delete_guild_config(str(guild.id))
        if deleted:
            print(f"[removed] guild_configs doc for guild {guild.id} ({guild.name})")
        else:
            print(f"[no-op] no guild_configs doc existed for guild {guild.id} ({guild.name})")
    except Exception as e:
        print(f"Failed to remove config for guild {guild.id} ({guild.name}): {e}")


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    if DEV_GUILD_ID:
        dev_guild_obj = dc.Object(id=int(DEV_GUILD_ID))
        try:
            synced = await bot.tree.sync(guild=dev_guild_obj)
            print(f"Synced {len(synced)} command(s) to dev guild {DEV_GUILD_ID}")
        except Exception as e:
            print(f"Failed to sync commands to dev guild {DEV_GUILD_ID}: {e}")
    else:
        print("No devGuildId set in config — skipping dev command sync.")


# ---------------------------------------------------------------------------
# DEV-ONLY: /simulate — manually trigger on_guild_join / on_guild_remove
# against the current guild, to test the DB logic without actually
# adding/removing the bot from a server. Registered only on devGuildId,
# and gated to the IDs listed in config["devUserId"].
# ---------------------------------------------------------------------------

if DEV_GUILD_ID:
    @bot.tree.command(
        name="simulate",
        description="Dev-only: simulate on_guild_join or on_guild_remove for this guild",
        guild=dc.Object(id=int(DEV_GUILD_ID)),
    )
    @ac.describe(event="Which event to simulate")
    @ac.choices(event=[
        ac.Choice(name="guild_join (bot added)", value="join"),
        ac.Choice(name="guild_remove (bot left/kicked)", value="leave"),
    ])
    async def simulate(interaction: dc.Interaction, event: ac.Choice[str]):
        if not _is_dev_user(interaction.user.id):
            await interaction.response.send_message(
                "You are not authorized to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command must be run inside a guild.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if event.value == "join":
            await on_guild_join(guild)
            await interaction.followup.send(
                f"Simulated `on_guild_join` for **{guild.name}** — config was created/reset in the DB.",
                ephemeral=True,
            )
        else:
            await on_guild_remove(guild)
            await interaction.followup.send(
                f"Simulated `on_guild_remove` for **{guild.name}** — config was deleted from the DB (if it existed).",
                ephemeral=True,
            )


async def main():
    development_mode = "--development" in sys.argv[1:]
    print(f"Attempting to log into {'Development' if development_mode else 'Production'} mode")
    token = config["devToken"] if development_mode else config["token"]
    async with bot:
        await bot.start(token)

asyncio.run(main())