import discord
from discord.ext import commands
from discord import app_commands
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

client = MongoClient(config.get("mongoUrl"))
db = client["Guilds"]

def _build_schema(required: list, properties: dict, additional_properties: bool = True) -> dict:
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
    try:
        db.create_collection(collection_name, validator=schema,
                             validationLevel=validation_level,
                             validationAction=validation_action)
        print(f"[created] '{collection_name}' with schema.")
    except CollectionInvalid:
        # Collection already exists -> update validator
        db.command({
            "collMod": collection_name,
            "validator": schema,
            "validationLevel": validation_level,
            "validationAction": validation_action,
        })
        print(f"[updated] '{collection_name}' schema.")


def _get_schema(collection_name: str) -> dict:
    info = db.command("listCollections", filter={"name": collection_name})
    batch = info.get("cursor", {}).get("firstBatch", [])
    if not batch:
        return {}
    return batch[0].get("options", {}).get("validator", {})


def _drop_schema(collection_name: str) -> None:
    db.command({
        "collMod": collection_name,
        "validator": {},
        "validationLevel": "off",
    })
    print(f"[dropped schema] '{collection_name}'")


def _validate_document(collection_name: str, document: dict) -> tuple[bool, str]:
    coll = db[collection_name]
    try:
        result = coll.insert_one(document)
        coll.delete_one({"_id": result.inserted_id})
        return True, "valid"
    except OperationFailure as e:
        return False, str(e)

def _guild_config_schema() -> dict:
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
    coll = db["guild_configs"]
    doc = coll.find_one({"_id": str(guild_id)})
    if doc is None:
        doc = _default_guild_config(guild_id)
        coll.insert_one(doc)
    return doc


def _update_guild_config(guild_id: str, updates: dict) -> None:
    from datetime import datetime, timezone
    updates["updatedAt"] = datetime.now(timezone.utc)
    db["guild_configs"].update_one(
        {"_id": str(guild_id)},
        {"$set": updates},
        upsert=True,
    )

def _reset_guild_config(guild_id: str) -> dict:
    fresh = _default_guild_config(guild_id)
    db["guild_configs"].replace_one({"_id": str(guild_id)}, fresh, upsert=True)
    return fresh

def _has_non_default_values(guild_id: str) -> bool:
    coll = db["guild_configs"]
    doc = coll.find_one({"_id": str(guild_id)})
    if doc is None:
        return False  # no config yet -> effectively default

    default = _default_guild_config(guild_id)

    ignored_keys = {"_id", "createdAt", "updatedAt"}
    current_stripped = {k: v for k, v in doc.items() if k not in ignored_keys}
    default_stripped = {k: v for k, v in default.items() if k not in ignored_keys}

    return current_stripped != default_stripped


def _delete_guild_config(guild_id: str) -> bool:
    result = db["guild_configs"].delete_one({"_id": str(guild_id)})
    return result.deleted_count > 0

intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

def _is_dev_user(user_id) -> bool:
    return str(user_id) in DEV_USER_IDS


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await guild.owner.send(content=f"<@{guild.owner_id}>", embed=discord.Embed(description=f"Thanks for Inviting me to your Server!", color=discord.Color.green()))
    except discord.Forbidden:
        print(f"Failed to DM {guild.owner} (ID: {guild.owner_id})")

    if db.guild_configs.find_one({"_id": str(guild.id)}) is None:
        db.guild_configs.insert_one(_default_guild_config(str(guild.id)))
    if db.guild_configs.find_one({"_id": str(guild.id)}) is not None:
        _has_non_default_val = _has_non_default_values(str(guild.id))
        if _has_non_default_val:
            _reset_guild_config(str(guild.id))


@bot.event
async def on_guild_remove(guild: discord.Guild):
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
        dev_guild_obj = discord.Object(id=int(DEV_GUILD_ID))
        try:
            synced = await bot.tree.sync(guild=dev_guild_obj)
            print(f"Synced {len(synced)} command(s) to dev guild {DEV_GUILD_ID}")
        except Exception as e:
            print(f"Failed to sync commands to dev guild {DEV_GUILD_ID}: {e}")
    else:
        print("No devGuildId set in config — skipping dev command sync.")


if DEV_GUILD_ID:
    @bot.tree.command(
        name="emit",
        description="Dev-only: simulate on_guild_join or on_guild_remove for this guild",
        guild=discord.Object(id=int(DEV_GUILD_ID)),
    )
    @app_commands.describe(event="Which event to simulate")
    @app_commands.choices(event=[
        app_commands.Choice(name="guild_join (bot added)", value="join"),
        app_commands.Choice(name="guild_remove (bot left/kicked)", value="leave"),
    ])
    async def emit(interaction: discord.Interaction, event: app_commands.Choice[str]):
        if not _is_dev_user(interaction.user.id):
            await interaction.response.send_message(
                "You do not have access to this Command (devOnlyCommand)", ephemeral=True
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

    print("cwd:", os.getcwd())
    print("sys.path:")
    for p in sys.path:
        print("  ", p)
    async with bot:
        await bot.load_extension("cogs.ticket.ticket")
        await bot.start(token)

asyncio.run(main())
