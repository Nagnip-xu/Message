# Telegram → 飞书消息中继

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

User approved the proposed Python / Telethon / FastAPI, React / TypeScript and SQLite stack. Windows-first local background process; retain a server deployment path.

## Users

A Chinese-speaking Telegram member who needs timely trading signal notifications in Feishu and wants to change monitored groups, channels and forum topics without editing code.

## Product Purpose

Continuously receive messages accessible to the signed-in Telegram account, match explicit monitoring tasks, persist delivery work and send clearly attributed Feishu notifications.

## Operating Context

Release 1.1.0 runs as a single background process on Windows or a Linux server. Each installation supplies its own Telegram application credentials, account login and Feishu group custom-bot Webhooks through the management interface. Private settings and account state are excluded from the source repository. Sources and forum topics are discovered from the signed-in account.

## Capabilities and Constraints

One Telegram account, multiple groups/channels/topics, multiple Feishu robots, new and edited text, caption and attachment notices, optional keyword filtering, durable delivery queue, reconnect/catch-up, explicit delayed-message labels, logs and manual retries. No automatic trading. No bypass of Telegram content protection. First run does not replay historical messages. No fabricated connected states, real messages or performance claims. Feishu acceptance is distinct from a mobile notification.

## Brand Commitments

Chinese interface; readable operational controls and explicit connection/error states, as approved in the plan. Working name: 消息中继.

## Product Principles

- Source and delivery scope are always explicit.
- A queued, failed or delayed signal must never be presented as a successfully delivered fresh signal.
- Background processing does not depend on the browser remaining open.
- Credentials are configurable locally and are excluded from version control.
