import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

from loguru import logger
from pymongo import MongoClient
from pymongo.errors import CollectionInvalid, OperationFailure

# Loguru ships with a default stderr handler already attached; remove it so
# we can control the sink, level, and format ourselves (stdout, env-gated
# level, same layout the old stdlib format used).
LOG_LEVEL = "DEBUG" if os.environ.get("DEBUG") else "INFO"
logger.remove()
logger.add(
    sys.stdout,
    level=LOG_LEVEL,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> [<level>{level: <8}</level>] <cyan>{name}</cyan>: <level>{message}</level>",
    colorize=True,
    backtrace=True,
    diagnose=False,  # True renders local variable values in tracebacks — verbose but handy while developing
)

config_path = Path(os.getcwd()) / "data" / "config.json"

ticket_config_path = Path(os.getcwd()) / "data" / "Ticket" / "server_ticket_config.json"

open_tickets_path = Path(os.getcwd()) / "data" / "Ticket" / "open_tickets.json"

ticket_types_path = Path(os.getcwd()) / "data" / "Ticket" / "ticket_types.json"

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
        return False

    default = _default_guild_config(guild_id)

    ignored_keys = {"_id", "createdAt", "updatedAt"}
    current_stripped = {k: v for k, v in doc.items() if k not in ignored_keys}
    default_stripped = {k: v for k, v in default.items() if k not in ignored_keys}

    return current_stripped != default_stripped


def _delete_guild_config(guild_id: str) -> bool:
    result = db["guild_configs"].delete_one({"_id": str(guild_id)})
    return result.deleted_count > 0

def _default_ticket_type_code_names() -> dict:
    return {
        "general-ticket": {
            "uuid": str(uuid.uuid4()),
            "supportRoleIds": [],
        },
    }


def _default_server_ticket_config(panel_uuid: str) -> dict:
    return {
        "panelUuid": panel_uuid,
        "panelChannelId": None,
        "ticketsOpened": 0,
    }


def _default_ticket_types(panel_uuid: str) -> dict:
    return {
        panel_uuid: _default_ticket_type_code_names(),
    }


def _default_open_tickets_entry() -> dict:
    return {}

# noinspection broad-exception
def _add_open_ticket(guild_id: str, channel_id: str, ticket_type_uuid: str,
                      channel_name: str, owner_id: str, owner_username: str) -> None:
    data = _read_json(open_tickets_path)
    guild_tickets = data.setdefault(str(guild_id), {})
    guild_tickets[str(channel_id)] = {
        "ticketTypeUuid": ticket_type_uuid,
        "channelName": channel_name,
        "ownerId": str(owner_id),
        "ownerUsername": owner_username,
    }
    _write_json(open_tickets_path, data)
    logger.info(f"[_add_open_ticket] recorded open ticket {channel_id} ({channel_name}) for guild {guild_id}")

    try:
        config_data = _read_json(ticket_config_path)
        guild_config = config_data.get(str(guild_id))
        if guild_config is not None:
            guild_config["ticketsOpened"] = guild_config.get("ticketsOpened", 0) + 1
            _write_json(ticket_config_path, config_data)
            logger.debug(
                f"[_add_open_ticket] bumped ticketsOpened for guild {guild_id} "
                f"to {guild_config['ticketsOpened']}"
            )
        else:
            logger.warning(f"[_add_open_ticket] no server_ticket_config entry for guild {guild_id}, skipped counter bump")
    except Exception:
        logger.exception(f"[_add_open_ticket] failed to bump ticketsOpened for guild {guild_id}")


def _remove_open_ticket(guild_id: str, channel_id: str) -> bool:
    data = _read_json(open_tickets_path)
    guild_tickets = data.get(str(guild_id))
    if guild_tickets is None or str(channel_id) not in guild_tickets:
        logger.info(f"[_remove_open_ticket] ticket {channel_id} not found for guild {guild_id}, no-op")
        return False

    guild_tickets.pop(str(channel_id))
    _write_json(open_tickets_path, data)
    logger.info(f"[_remove_open_ticket] removed open ticket {channel_id} for guild {guild_id}")
    return True


def _get_open_ticket(guild_id: str, channel_id: str) -> dict | None:
    data = _read_json(open_tickets_path)
    return data.get(str(guild_id), {}).get(str(channel_id))


def _get_open_tickets_for_guild(guild_id: str) -> dict:
    data = _read_json(open_tickets_path)
    return data.get(str(guild_id), {})


# noinspection shadowing-names
def _read_json(path: Path) -> dict:
    abs_path = path.resolve()
    exists = abs_path.exists()
    logger.debug(f"[_read_json] reading {abs_path} (exists={exists})")
    if not exists:
        logger.error(f"[_read_json] FILE DOES NOT EXIST: {abs_path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    logger.debug(f"[_read_json] loaded {len(data)} key(s) from {abs_path.name}: {list(data.keys())}")
    return data


# noinspection shadowing-names
def _write_json(path: Path, data: dict) -> None:
    abs_path = path.resolve()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    logger.debug(f"[_write_json] wrote {len(data)} key(s) to {abs_path} -> {list(data.keys())}")


def _upsert_guild_entry(path: Path, guild_id: str, default_value: dict) -> None:
    logger.debug(f"[_upsert_guild_entry] path={path.resolve()} guild_id={guild_id} default={default_value}")
    data = _read_json(path)
    existed_before = guild_id in data
    data[str(guild_id)] = json.loads(json.dumps(default_value))  # deep copy
    _write_json(path, data)
    logger.info(
        f"[_upsert_guild_entry] {'overwrote existing' if existed_before else 'created new'} "
        f"entry for guild {guild_id} in {path.name}"
    )


def _remove_guild_entry(path: Path, guild_id: str) -> bool:
    logger.debug(f"[_remove_guild_entry] path={path.resolve()} guild_id={guild_id}")
    data = _read_json(path)
    if str(guild_id) in data:
        data.pop(str(guild_id))
        _write_json(path, data)
        logger.info(f"[_remove_guild_entry] removed guild {guild_id} from {path.name}")
        return True
    logger.info(f"[_remove_guild_entry] guild {guild_id} not present in {path.name}, no-op")
    return False


intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)


def _is_dev_user(user_id) -> bool:
    return str(user_id) in DEV_USER_IDS


# noinspection broad-exception
@bot.event
async def on_guild_join(guild: discord.Guild):
    logger.info(f"=== on_guild_join START for guild {guild.id} ({guild.name}) ===")

    try:
        owner = guild.owner
        if owner is None:
            logger.debug(f"[on_guild_join] guild.owner was None, fetching owner {guild.owner_id} explicitly")
            owner = await guild.fetch_member(guild.owner_id)

        await owner.send(
            content=f"<@{guild.owner_id}>",
            embed=discord.Embed(
                description="Thanks for Inviting me to your Server!",
                color=discord.Color.green(),
            ),
        )
        logger.debug(f"[on_guild_join] DM sent to owner {guild.owner_id}")
    except discord.Forbidden:
        logger.warning(f"[on_guild_join] Failed to DM {guild.owner} (ID: {guild.owner_id}) — Forbidden")
    except Exception:
        logger.exception(f"[on_guild_join] Unexpected error while DMing owner of guild {guild.id}")

    try:
        logger.debug(f"[on_guild_join] checking Mongo guild_configs for guild {guild.id}")
        if db.guild_configs.find_one({"_id": str(guild.id)}) is None:
            db.guild_configs.insert_one(_default_guild_config(str(guild.id)))
            logger.info(f"[on_guild_join] inserted new guild_configs doc for {guild.id}")
        else:
            if _has_non_default_values(str(guild.id)):
                _reset_guild_config(str(guild.id))
                logger.info(f"[on_guild_join] reset non-default guild_configs doc for {guild.id}")
            else:
                logger.debug(f"[on_guild_join] guild_configs doc for {guild.id} already at default, no-op")
    except Exception:
        logger.exception(f"[on_guild_join] Mongo guild_configs step FAILED for guild {guild.id}")

    # Shared UUID linking this guild's server_ticket_config entry to its
    # ticket_types entry — generated once so both files stay in sync.
    panel_uuid = str(uuid.uuid4())
    logger.debug(f"[on_guild_join] generated panelUuid={panel_uuid} for guild {guild.id}")

    try:
        _upsert_guild_entry(ticket_config_path, str(guild.id), _default_server_ticket_config(panel_uuid))
    except Exception:
        logger.exception(f"[on_guild_join] server_ticket_config.json update FAILED for guild {guild.id}")

    try:
        _upsert_guild_entry(ticket_types_path, str(guild.id), _default_ticket_types(panel_uuid))
    except Exception:
        logger.exception(f"[on_guild_join] ticket_types.json update FAILED for guild {guild.id}")

    try:
        _upsert_guild_entry(open_tickets_path, str(guild.id), _default_open_tickets_entry())
    except Exception:
        logger.exception(f"[on_guild_join] open_tickets.json update FAILED for guild {guild.id}")

    logger.info(f"=== on_guild_join END for guild {guild.id} ({guild.name}) ===")

# noinspection broad-exception
@bot.event
async def on_guild_remove(guild: discord.Guild):
    logger.info(f"=== on_guild_remove START for guild {guild.id} ({guild.name}) ===")

    try:
        deleted = _delete_guild_config(str(guild.id))
        if deleted:
            logger.info(f"[on_guild_remove] removed guild_configs doc for guild {guild.id} ({guild.name})")
        else:
            logger.info(f"[on_guild_remove] no guild_configs doc existed for guild {guild.id} ({guild.name})")
    except Exception:
        logger.exception(f"[on_guild_remove] Failed to remove guild_configs doc for guild {guild.id} ({guild.name})")

    try:
        _remove_guild_entry(ticket_config_path, str(guild.id))
    except Exception:
        logger.exception(f"[on_guild_remove] server_ticket_config.json update FAILED for guild {guild.id}")

    try:
        _remove_guild_entry(ticket_types_path, str(guild.id))
    except Exception:
        logger.exception(f"[on_guild_remove] ticket_types.json update FAILED for guild {guild.id}")

    try:
        _remove_guild_entry(open_tickets_path, str(guild.id))
    except Exception:
        logger.exception(f"[on_guild_remove] open_tickets.json update FAILED for guild {guild.id}")

    logger.info(f"=== on_guild_remove END for guild {guild.id} ({guild.name}) ===")

# noinspection broad-exception
@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    if DEV_GUILD_ID:
        dev_guild_obj = discord.Object(id=int(DEV_GUILD_ID))
        try:
            synced = await bot.tree.sync(guild=dev_guild_obj)
            logger.info(f"Synced {len(synced)} command(s) to dev guild {DEV_GUILD_ID}")
        except Exception:
            logger.exception(f"Failed to sync commands to dev guild {DEV_GUILD_ID}")
    else:
        logger.info("No devGuildId set in config — skipping dev command sync.")


# noinspection unused-parameter
@bot.event
async def on_error(event_method, *args, **kwargs):
    logger.exception(f"Unhandled exception in event handler '{event_method}'")


if DEV_GUILD_ID:
    # noinspection broad-exception
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

        try:
            if event.value == "join":
                await on_guild_join(guild)
                await interaction.followup.send(
                    f"Simulated `on_guild_join` for **{guild.name}** — check console for debug output.",
                    ephemeral=True,
                )
            else:
                await on_guild_remove(guild)
                await interaction.followup.send(
                    f"Simulated `on_guild_remove` for **{guild.name}** — check console for debug output.",
                    ephemeral=True,
                )
        except Exception:
            logger.exception(f"[/emit] Simulation of '{event.value}' failed for guild {guild.id}")
            await interaction.followup.send(
                f"Simulation raised an exception — check console/logs for the traceback.",
                ephemeral=True,
            )


async def main():
    development_mode = "--development" in sys.argv[1:]
    logger.info(f"Attempting to log into {'Development' if development_mode else 'Production'} mode")
    token = config["devToken"] if development_mode else config["token"]

    logger.debug(f"cwd: {os.getcwd()}")
    logger.debug(f"ticket_config_path resolves to: {ticket_config_path.resolve()} (exists={ticket_config_path.exists()})")
    logger.debug(f"ticket_types_path resolves to: {ticket_types_path.resolve()} (exists={ticket_types_path.exists()})")
    logger.debug(f"open_tickets_path resolves to: {open_tickets_path.resolve()} (exists={open_tickets_path.exists()})")
    for p in sys.path:
        logger.debug(f"  sys.path: {p}")

    async with bot:
        await bot.load_extension("cogs.ticket.ticket")
        await bot.start(token)

asyncio.run(main())