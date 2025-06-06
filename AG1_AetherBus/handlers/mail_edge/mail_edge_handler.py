import asyncio
import imaplib
import smtplib
from email.message import EmailMessage
from email import message_from_bytes
from redis.asyncio import Redis
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.bus import publish_envelope, subscribe, build_redis_url
from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions

keys = StreamKeyBuilder()
REGISTER_STREAM = keys.edge_register("mail")

registered_accounts: dict[str, dict] = {}

async def fetch_messages(cfg: dict) -> list[dict]:
    """Fetch unseen emails for the account without deleting them."""
    def _inner():
        host = cfg.get("imap_host", "imap.gmail.com")
        username = cfg.get("username")
        password = cfg.get("password")
        folder = cfg.get("folder", "INBOX")
        messages = []
        with imaplib.IMAP4_SSL(host) as M:
            M.login(username, password)
            M.select(folder)
            typ, data = M.search(None, "UNSEEN")
            if typ != 'OK':
                return []
            for num in data[0].split():
                typ, msg_data = M.fetch(num, '(RFC822)')
                if typ != 'OK':
                    continue
                msg = message_from_bytes(msg_data[0][1])
                subject = msg.get('Subject', '')
                body = ''
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        if ctype == 'text/plain' and not part.get('Content-Disposition'):
                            body = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', 'ignore')
                            break
                else:
                    body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', 'ignore')
                messages.append({'uid': num.decode(), 'subject': subject, 'body': body})
        return messages
    return await asyncio.to_thread(_inner)

async def poll_account(redis: Redis, cfg: dict):
    """Periodically check the mailbox and publish new emails to the agent."""
    seen: set[str] = set()
    agent_name = cfg.get("agent_name")
    user_stream = keys.agent_inbox(agent_name)
    reply_stream = keys.edge_response("mail", cfg.get("username"))
    while True:
        try:
            msgs = await fetch_messages(cfg)
            for m in msgs:
                if m['uid'] in seen:
                    continue
                seen.add(m['uid'])
                env = Envelope(
                    role="user",
                    user_id=cfg.get("username"),
                    agent_name=agent_name,
                    envelope_type="message",
                    content={"subject": m['subject'], "text": m['body']},
                    reply_to=reply_stream
                )
                await publish_envelope(redis, user_stream, env)
        except Exception as e:
            print(f"[MAIL_EDGE] Error polling {cfg.get('username')}: {e}")
        await asyncio.sleep(cfg.get("poll_interval", 30))

def send_email(cfg: dict, to_addr: str, subject: str, body: str):
    host = cfg.get("smtp_host", "smtp.gmail.com")
    port = int(cfg.get("smtp_port", 587))
    username = cfg.get("smtp_user", cfg.get("username"))
    password = cfg.get("smtp_password", cfg.get("password"))
    msg = EmailMessage()
    msg['From'] = username
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(username, password)
        s.send_message(msg)

async def handle_agent_reply(env: Envelope, cfg: dict):
    if env.content and isinstance(env.content, dict):
        text = env.content.get("text")
        if text:
            await asyncio.to_thread(
                send_email,
                cfg,
                cfg.get("username"),
                f"Agent reply: {env.agent_name}",
                text
            )

async def handle_register(env: Envelope, redis: Redis):
    if env.envelope_type != "register":
        return
    cfg = env.content or {}
    username = cfg.get("username")
    if not username:
        print("[MAIL_EDGE] Registration missing username")
        return
    cfg["agent_name"] = env.agent_name
    registered_accounts[username] = cfg
    asyncio.create_task(poll_account(redis, cfg))
    asyncio.create_task(
        subscribe(
            redis,
            keys.edge_response("mail", username),
            lambda e: handle_agent_reply(e, cfg),
            group=f"mail_edge_{username}"
        )
    )
    print(f"[MAIL_EDGE] Registered mail account {username} for agent {env.agent_name}")

async def main():
    redis = Redis.from_url(build_redis_url())
    await start_bus_subscriptions(
        redis=redis,
        patterns=[REGISTER_STREAM],
        group="mail_edge",
        handler=lambda env: handle_register(env, redis)
    )

from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
