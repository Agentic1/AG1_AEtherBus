# AG1 Core Bus – Mail Edge Handler

The mail edge connector bridges IMAP/SMTP accounts to the AG1 bus. Each agent can register an email account so incoming emails are delivered to the agent inbox and agent replies are sent back via SMTP.

## Registration
Agents publish a registration envelope to `AG1:edge:mail:register` containing the IMAP/SMTP credentials:

```json
{
  "envelope_type": "register",
  "agent_name": "MuseMail",
  "content": {
    "username": "mybot@example.com",
    "password": "app-password",
    "imap_host": "imap.gmail.com",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587
  }
}
```
An example configuration file is available at `examples/mail_edge_config.json`.

On registration the handler starts polling the mailbox and subscribes to the agent reply channel `AG1:edge:mail:<username>:response`.

## Message Flow
1. New emails are fetched (but not deleted) via IMAP.
2. Each message is published to the agent inbox `AG1:agent:<agent_name>:inbox` with the reply stream set to `AG1:edge:mail:<username>:response`.
3. Replies from the agent on that stream are sent back to the mailbox via SMTP.

## Running
```
python -m AG1_AetherBus.handlers.mail_edge.mail_edge_handler
```
Make sure the standard Redis environment variables are configured.
