import uuid as uuid_module
from dataclasses import dataclass
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.types import (
    PeerUser, User, Message, MessageEntityMentionName,
    ReplyInlineMarkup, KeyboardButtonRow, KeyboardButtonCallback,
)
from typing import Optional
import asyncio
import logging
import os
import re
import time
import typing


logging.basicConfig(level=logging.INFO)


api_id = int(os.environ['TELEGRAM_API_ID'])
api_hash = os.environ['TELEGRAM_API_HASH']
bot_token = os.environ['TELEGRAM_BOT_TOKEN']


user_client = TelegramClient('user', api_id, api_hash)
bot_client = TelegramClient('bot', api_id, api_hash)


@dataclass
class ProxyRule:
    chat_id: int
    sandbox_chat_id: int
    media_share_chat_id: int

    def has_chat_id(self, chat_id: int):
        return chat_id in (self.chat_id, self.sandbox_chat_id, self.media_share_chat_id)


RULES = [
    ProxyRule(
        chat_id=int(os.environ['PROXY_CHAT_ID']),
        sandbox_chat_id=int(os.environ['PROXY_SANDBOX_CHAT_ID']),
        media_share_chat_id=int(os.environ['PROXY_MEDIA_SHARE_CHAT_ID']),
    ),
]

def select_rule(chat_id: int) -> Optional[ProxyRule]:
    for rule in RULES:
        if rule.has_chat_id(chat_id):
            return rule
    return None


def now():
    return int(1000 * time.time())


@dataclass
class AuthorMeta:
    user_id: int
    first_name: str

    @classmethod
    def from_user(cls, user: Optional[User]) -> Optional['AuthorMeta']:
        if not user:
            return None
        if not user.first_name:
            return None
        return cls(user.id, user.first_name)


@dataclass(frozen=True)
class MessageInstanceId:
    chat_id: int
    message_id: int


@dataclass
class MessageChain:
    author: Optional[AuthorMeta] = None

    source: Optional[MessageInstanceId] = None
    target: Optional[MessageInstanceId] = None
    shared: Optional[MessageInstanceId] = None

    timestamp_ms: int = -1

    def __post_init__(self):
        self.timestamp_ms = now()

    def is_expired(self, threshold_ms=10 * 60 * 1000):
        return self.timestamp_ms + threshold_ms < now()


CHAINS: list[MessageChain] = []

# Button proxy map:
# (main_chat_id, main_msg_id, proxy_data: bytes) -> (sandbox_chat_id, sandbox_msg_id, original_data: bytes)
# Cleaned up alongside chains (10 min TTL).
BUTTON_MAP: dict[tuple, tuple] = {}
BUTTON_MAP_TS: dict[tuple, int] = {}  # key -> created_at ms


def select_chain(source: Optional[MessageInstanceId] = None,
                 target: Optional[MessageInstanceId] = None,
                 shared: Optional[MessageInstanceId] = None) -> Optional[MessageChain]:
    for chain in CHAINS:
        if source is not None and chain.source != source:
            continue
        if target is not None and chain.target != target:
            continue
        if shared is not None and chain.shared != shared:
            continue
        return chain
    return None


def cleanup_chains():
    global CHAINS, BUTTON_MAP, BUTTON_MAP_TS

    new_chains = []
    shared = {}
    for chain in CHAINS:
        if chain.is_expired():
            continue

        # merge chains that have the same shared message.
        previous = shared.get(chain.shared, None)
        if previous is not None:
            if chain.target is not None and previous.target is None:
                previous.target = chain.target
            if chain.source is not None and previous.source is None:
                previous.source = chain.source
            if chain.author is not None and previous.author is None:
                previous.author = chain.author
            continue

        if chain.shared is not None:
            shared[chain.shared] = chain
        new_chains.append(chain)

    CHAINS = new_chains
    logging.info(f'Current chains: {CHAINS}')

    # Clean up expired button mappings
    threshold = now() - 10 * 60 * 1000
    expired_keys = [k for k, ts in BUTTON_MAP_TS.items() if ts < threshold]
    for k in expired_keys:
        BUTTON_MAP.pop(k, None)
        BUTTON_MAP_TS.pop(k, None)


def build_proxy_markup(
    markup,
    sandbox_chat_id: int,
    sandbox_msg_id: int,
    main_chat_id: int,
    main_msg_id: int,
) -> Optional[ReplyInlineMarkup]:
    """Copy inline keyboard, replacing callback_data with proxy UUIDs."""
    if not markup or not hasattr(markup, 'rows'):
        return None

    new_rows = []
    for row in markup.rows:
        new_btns = []
        for btn in row.buttons:
            if hasattr(btn, 'data'):
                proxy_data = uuid_module.uuid4().bytes[:8]
                key = (main_chat_id, main_msg_id, proxy_data)
                BUTTON_MAP[key] = (sandbox_chat_id, sandbox_msg_id, btn.data)
                BUTTON_MAP_TS[key] = now()
                new_btns.append(KeyboardButtonCallback(text=btn.text, data=proxy_data))
            else:
                new_btns.append(btn)
        new_rows.append(KeyboardButtonRow(buttons=new_btns))

    return ReplyInlineMarkup(rows=new_rows)


def update_proxy_markup(
    markup,
    sandbox_chat_id: int,
    sandbox_msg_id: int,
    main_chat_id: int,
    main_msg_id: int,
) -> Optional[ReplyInlineMarkup]:
    """Rebuild proxy markup for an edited message, removing stale keys first."""
    stale = [k for k in list(BUTTON_MAP) if k[0] == main_chat_id and k[1] == main_msg_id]
    for k in stale:
        BUTTON_MAP.pop(k, None)
        BUTTON_MAP_TS.pop(k, None)
    return build_proxy_markup(markup, sandbox_chat_id, sandbox_msg_id, main_chat_id, main_msg_id)


def is_tiktoker_message(message: Message) -> bool:
    text = message.message
    if not text:
        return False
    domains = [
        'tiktok.com',
        'vm.tiktok.com',
        'tiktokcdn.com',
        'spotify.com',
        'youtube.com',
        'youtu.be',
        'instagram.com',
    ]
    return any(domain in text for domain in domains)


def try_patch_name(message) -> Message:
    message_id = MessageInstanceId(message.chat_id, message.id)
    logging.info(f'Patching {message_id}')

    chain = None
    for c in CHAINS:
        if not c.is_expired() and c.target and c.target.chat_id == message_id.chat_id:
            if chain is None or c.timestamp_ms > chain.timestamp_ms:
                chain = c

    if not chain or chain.author is None:
        logging.info(f'Cannot patch, bad chain: {chain}')
        return message

    text = message.message
    match = re.search(r'^Downloaded: (.*)$', text, re.MULTILINE)
    if not match:
        logging.info(f'Cannot patch, message not matched')
        return message

    if not message.entities:
        logging.info(f'Cannot patch, message has no entities')
        return message

    offset, length = match.start(1), len(match.group(1))
    message.message = text[:offset] + chain.author.first_name + text[offset+length:]

    for entity in message.entities:
        if isinstance(entity, MessageEntityMentionName) and entity.offset == offset and entity.length == length:
            entity.length = len(chain.author.first_name)
            entity.user_id = chain.author.user_id
            continue

        if entity.offset > offset:
            entity.offset -= len(match.group(1))
            entity.offset += len(chain.author.first_name)

    return message


async def fetch_user(client: TelegramClient, message: Message) -> Optional[User]:
    peer = message.from_id or message.peer_id
    if not isinstance(peer, PeerUser):
        return None

    try:
        user = await client.get_entity(peer)
        return typing.cast(User, user)
    except ValueError as e:
        logging.warning(f'Failed to fetch peer {peer.stringify()}', e)
        return None


@bot_client.on(events.NewMessage(incoming=True))
async def bot_message_handler(event: events.NewMessage.Event):
    cleanup_chains()

    if not event.chat_id:
        return
    message_id = MessageInstanceId(event.chat_id, event.message.id)

    rule = select_rule(message_id.chat_id)
    if not rule:
        return

    if event.chat_id == rule.chat_id:
        logging.info(f'Processing original message {message_id}')

        message = event.message
        if not is_tiktoker_message(message):
            logging.info(f'Skipping message id={message_id}, not matched by filter')
            return

        user = await fetch_user(bot_client, message)
        if user is None:
            logging.warning(f'Failed to fetch user for message {message_id}')

        sandboxed = await user_client.send_message(rule.sandbox_chat_id, message)
        sandboxed_id = MessageInstanceId(rule.sandbox_chat_id, sandboxed.id)

        chain = MessageChain(
            author=AuthorMeta.from_user(user),
            source=message_id,
            target=sandboxed_id,
        )
        logging.info(f'New chain: {chain}')
        CHAINS.append(chain)

    if event.chat_id == rule.media_share_chat_id:
        logging.info(f'Processing media share message {message_id}')

        target = await event.message.forward_to(rule.chat_id, drop_author=True)
        target_id = MessageInstanceId(rule.chat_id, target.id)

        chain = select_chain(shared=message_id)
        if chain:
            chain.target = target_id
            logging.info(f'Updated chain: {chain}')
        else:
            # Previous chain may be missing if message was received before sender got confirmation.
            chain = MessageChain(
                shared=message_id,
                target=target_id,
            )
            logging.info(f'New chain: {chain}')
            CHAINS.append(chain)


@bot_client.on(events.CallbackQuery())
async def callback_query_handler(event: events.CallbackQuery.Event):
    cleanup_chains()

    key = (event.chat_id, event.message_id, event.data)
    mapping = BUTTON_MAP.get(key)
    if not mapping:
        logging.info(f'No button mapping for key chat={event.chat_id} msg={event.message_id}')
        await event.answer('Кнопка устарела')
        return

    sandbox_chat_id, sandbox_msg_id, original_data = mapping
    logging.info(f'Proxying button click to sandbox msg={sandbox_msg_id} data={original_data!r}')

    try:
        msg = await user_client.get_messages(sandbox_chat_id, ids=sandbox_msg_id)
        await msg.click(data=original_data)
        await event.answer()
    except Exception as e:
        logging.warning(f'Failed to proxy button click: {e}')
        await event.answer('Ошибка при нажатии кнопки')


@user_client.on(events.NewMessage(incoming=True))
async def user_message_handler(event: events.NewMessage.Event):
    cleanup_chains()

    if not event.chat_id:
        return
    message_id = MessageInstanceId(event.chat_id, event.message.id)

    rule = select_rule(event.chat_id)
    if not rule:
        return

    if event.chat_id == rule.sandbox_chat_id:
        logging.info(f'Processing sandbox message {message_id}')

        message = event.message
        message = try_patch_name(message)

        user = await fetch_user(user_client, message)
        if user is None:
            logging.warning(f'Failed to fetch user for message {message_id}')

        if message.media:
            shared = await user_client.send_message(rule.media_share_chat_id, message)
            shared_id = MessageInstanceId(rule.media_share_chat_id, shared.id)
            chain = MessageChain(
                author=AuthorMeta.from_user(user),
                source=message_id,
                shared=shared_id,
            )
        else:
            passed = await bot_client.send_message(rule.chat_id, message)
            passed_id = MessageInstanceId(rule.chat_id, passed.id)
            chain = MessageChain(
                author=AuthorMeta.from_user(user),
                source=message_id,
                target=passed_id,
            )

            # Proxy inline keyboard buttons if present
            if message.reply_markup:
                markup = build_proxy_markup(
                    message.reply_markup,
                    sandbox_chat_id=message_id.chat_id,
                    sandbox_msg_id=message_id.message_id,
                    main_chat_id=rule.chat_id,
                    main_msg_id=passed.id,
                )
                if markup:
                    await bot_client.edit_message(rule.chat_id, passed.id, buttons=markup)
                    logging.info(f'Proxied {len(markup.rows)} button rows to main chat')
                else:
                    logging.info(f'Message has reply_markup but no callback buttons to proxy')
            else:
                logging.info(f'Message has no reply_markup')

        logging.info(f'New chain: {chain}')
        CHAINS.append(chain)


@bot_client.on(events.MessageEdited())
async def bot_message_edited(event: events.MessageEdited.Event):
    cleanup_chains()

    if not event.chat_id:
        return
    message_id = MessageInstanceId(event.chat_id, event.message.id)

    chain = select_chain(shared=message_id)
    if not chain:
        logging.info(f'Skipping edited shared message {message_id}, no chain found')
        return

    message = event.message
    if not chain.target:
        logging.warning(f'Unexpected chain edit: {chain}')
        return

    await bot_client.edit_message(
        entity=chain.target.chat_id,
        message=chain.target.message_id,  # type: ignore
        text=message.message,
        file=message.media,  # type: ignore
        formatting_entities=message.entities,
    )


@user_client.on(events.MessageEdited())
async def user_message_edited(event: events.MessageEdited.Event):
    cleanup_chains()

    if not event.chat_id:
        return
    message_id = MessageInstanceId(event.chat_id, event.message.id)

    chain = select_chain(source=message_id)
    if not chain:
        logging.info(f'Skipping edited source message {message_id}, no chain found')
        return

    message = event.message
    message = try_patch_name(message)

    if message.media and not chain.shared:
        logging.warning(f'Skipping message edit because media was added, but the chain has not been shared: {chain}')
        return

    try:
      if chain.shared:
        await user_client.edit_message(
            entity=chain.shared.chat_id,
            message=chain.shared.message_id,  # type: ignore
            text=message.message,
            file=message.media,  # type: ignore
            formatting_entities=message.entities,
        )
      elif chain.target:
        await user_client.edit_message(
            entity=chain.target.chat_id,
            message=chain.target.message_id,  # type: ignore
            text=message.message,
            file=message.media,  # type: ignore
            formatting_entities=message.entities,
        )
    except MessageNotModifiedError:
        logging.info(f'Message not modified, skipping edit for chain: {chain}')

        if chain.source and message.reply_markup:
            markup = update_proxy_markup(
                message.reply_markup,
                sandbox_chat_id=chain.source.chat_id,
                sandbox_msg_id=chain.source.message_id,
                main_chat_id=chain.target.chat_id,
                main_msg_id=chain.target.message_id,
            )
            if markup:
                await bot_client.edit_message(
                    chain.target.chat_id,
                    chain.target.message_id,
                    buttons=markup,
                )
    if not chain.target and not chain.shared:
        logging.warning(f'Unexpected chain edit: {chain}')


@user_client.on(events.MessageDeleted())
async def user_message_deleted(event: events.MessageDeleted.Event):
    cleanup_chains()

    if not event.chat_id or not event.deleted_id:
        return
    message_id = MessageInstanceId(event.chat_id, event.deleted_id)

    logging.info(f'Processing deletion for {message_id}')

    target_chain = select_chain(target=message_id)
    if target_chain:
        if not target_chain.source:
            logging.warning(f'Deleted chain is missing source: {target_chain.source}')
            return

        logging.info(f'Deleting source message: {target_chain}')
        await bot_client.delete_messages(target_chain.source.chat_id, target_chain.source.message_id)

    source_chain = select_chain(source=message_id)
    if source_chain:
        if not source_chain.target:
            logging.warning(f'Deleted chain is missing target: {source_chain.target}')
            return

        logging.info(f'Deleting target message: {source_chain}')
        await bot_client.delete_messages(source_chain.target.chat_id, source_chain.target.message_id)


async def main():
    async with bot_client, user_client:
        await bot_client.start(bot_token=bot_token)  # type: ignore
        await user_client.start()  # type: ignore

        await asyncio.wait([
            asyncio.create_task(bot_client.run_until_disconnected()),  # type: ignore
            asyncio.create_task(user_client.run_until_disconnected()),  # type: ignore
        ], return_when=asyncio.FIRST_COMPLETED)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
