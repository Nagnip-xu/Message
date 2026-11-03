---
name: 消息中继
description: Telegram 消息路由到飞书的本地运维工作台
colors:
  teal-action: "#087d76"
  teal-action-hover: "#06655f"
  ink: "#202c36"
  text-secondary: "#65727f"
  workspace: "#f6f8fa"
  sidebar: "#f0f4f6"
  border: "#e4e9ed"
  surface: "#ffffff"
  success: "#23714e"
  warning: "#8d6416"
  danger: "#b34239"
typography:
  body:
    fontFamily: "Segoe UI, Microsoft YaHei, Noto Sans SC, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "0"
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
spacing:
  compact: "7px"
  small: "12px"
  medium: "18px"
  large: "25px"
components:
  button-primary:
    backgroundColor: "{colors.teal-action}"
    textColor: "{colors.surface}"
    rounded: "{rounded.sm}"
    padding: "9px 14px"
---

# Design System: 消息中继

## Overview

An operational workbench for setting message routes and diagnosing deliveries. Route rows, the Telegram-to-Feishu connection strip, and distinct source and destination settings expose the product's real mechanism. The existing implementation establishes a compact Chinese system-sans UI for repeated scanning; it favors real connection state, source attribution, queue state and recoverable error information over decoration.

Key characteristics: structured registry navigation; concise route tables; visible prerequisites; restrained teal actions; explicit delivery states.

## Colors

Use the single teal accent for direct actions, current navigation and live connection states. Leave workspace and sidebar neutrals distinct. Color signifies a real operational state, not decoration.

### Primary
- **中继青** (#087d76): Main actions and current route/navigation state. Hover uses #06655f.

### Neutral
- **墨字** (#202c36): Main text on light surfaces.
- **次级说明** (#65727f): Secondary text; WCAG normal-text contrast target remains 4.5:1 in use.
- **工作台底色** (#f6f8fa): Main background.
- **导航底色** (#f0f4f6): Persistent sidebar background.
- **边界灰** (#e4e9ed): Table, section and control separation.
- **内容白** (#ffffff): Primary content, dialog and input surfaces.

### Named Rules
**The State Must Be True Rule.** A green state means a confirmed active connection. A queued or failed delivery never counts as a successful Feishu response, and an API success never claims a phone notification.

**The Explicit Route Rule.** Every monitoring task names its source, selected topic scope and destination. A forum task may choose an individual topic or the entire group.

## Typography

**Body Font:** Segoe UI with Microsoft YaHei and Noto Sans SC fallbacks.

**Character:** Familiar, neutral and tuned to operational controls, Chinese message text and status rows.

### Hierarchy
- **Page title** (650, 27px/1.4): current workspace; 23–24px on smaller views.
- **Section title** (600, 14–16px/1.5): tables and grouped settings.
- **Body** (400, 14px/1.6): main UI and form content.
- **Metadata** (400–500, 10–12px/1.6): routes, status and timestamps; retain adequate contrast.

## Layout

The desktop shell uses a 224px persistent sidebar and a flexible task workspace. Desktop operation presents a compact connection strip, three-step first-run sequence, four numeric route/delivery summaries and a full-width route table. Settings pages split controls and operational explanation. Details and configuration use a right-edge modal drawer.

At 1180px and 900px, route columns scroll horizontally when their data needs it; configuration and auxiliary columns stack. At 680px, sidebar becomes an accessible drawer, connection endpoints stack, setup steps form a vertical sequence, and metrics use two columns. Fixed-size UI type is preferred over viewport-scaled type. Keep timestamps legible and avoid clipping source, delivery and error content.

## Elevation & Depth

The workbench relies on layered background and white-content surfaces with fine borders. Elevation belongs to a small amount of modal depth and transient notices; normal table rows use rules and spacing.

## Shapes

Use compact, consistent corners: 6px for controls; 7px for navigation and inputs; 8px for collection surfaces; 10px for connection strips and larger sections. Round only state indicators, avatars and switches completely.

## Components

- **Navigation:** stable icon-and-label rows; a lightly tinted fill marks the current workspace.
- **Route table:** stable columns for Telegram source, Feishu destination, task state, last success and row actions. Preserve scrolling affordance when the viewport cannot fit all columns.
- **Connection strip:** color separates Telegram and Feishu endpoints; the connecting arrow shows direction without claiming a live delivery.
- **Badges:** subdued green for accepted/success states, amber for delay/retry, red for failures, grey for inactive states.
- **Forms:** use associated labels, supporting text, visible keyboard focus and explicit save/cancel actions.
- **Drawers:** task and notification details are separate from the underlying registry and close with Escape.
- **Toggles:** switch states persist locally and do not submit their surrounding form.
- **Icons:** use the consistent Lucide stroke set; never use arbitrary Unicode symbols as tool icons.

## Do's and Don'ts

- Keep routing and recipient visible in the same scan path.
- Report Telegram connectivity and local service connectivity separately.
- Preserve UTC+8 source and delivery timestamps.
- Mark catch-up messages when they are delayed.
- Keep local processing and test results visibly distinct from live Telegram evidence.
- Do not describe Feishu acceptance as a device notification guarantee.
- Do not place secrets in source control or application logs.
- Preserve keyboard access through the responsive navigation and modal drawers.
