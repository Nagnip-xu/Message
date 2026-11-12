---
name: Message 官网
description: 以消息抵达的过程呈现 Telegram 到飞书的中继工具
colors:
  teal: "#087d76"
  teal-dark: "#065d59"
  ink: "#202c36"
  muted: "#58666c"
  mint: "#d8f9c9"
  paper: "#fafcfb"
  line: "#d8e4df"
  surface: "#ffffff"
  mint-surface: "#f7fff9"
  on-teal: "#f0fff7"
  focus: "#407cc8"
typography:
  display:
    fontFamily: "Message Sans, Segoe UI, Microsoft YaHei, Noto Sans SC, sans-serif"
    fontSize: "clamp(45px,5.25vw,78px)"
    fontWeight: 700
    lineHeight: 1.24
    letterSpacing: "-.035em"
  headline:
    fontFamily: "Message Sans, Segoe UI, Microsoft YaHei, Noto Sans SC, sans-serif"
    fontSize: "clamp(30px,3.25vw,46px)"
    fontWeight: 650
    lineHeight: 1.4
    letterSpacing: "-.025em"
  title:
    fontFamily: "Message Sans, Segoe UI, Microsoft YaHei, Noto Sans SC, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.7
  body:
    fontFamily: "Message Sans, Segoe UI, Microsoft YaHei, Noto Sans SC, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.7
  label:
    fontFamily: "Message Sans, Segoe UI, Microsoft YaHei, Noto Sans SC, sans-serif"
    fontSize: "14px"
    fontWeight: 650
    lineHeight: 1.7
rounded:
  control: "6px"
  action: "8px"
  container: "10px"
  sheet: "12px"
spacing:
  compact: "12px"
  small: "20px"
  medium: "24px"
  large: "28px"
components:
  button-primary:
    backgroundColor: "{colors.teal}"
    textColor: "{colors.surface}"
    typography: "{typography.label}"
    rounded: "{rounded.action}"
    padding: "13px 22px"
  button-primary-hover:
    backgroundColor: "{colors.teal-dark}"
  button-mint:
    backgroundColor: "{colors.mint}"
    textColor: "#144c43"
    typography: "{typography.label}"
    rounded: "{rounded.action}"
    padding: "13px 22px"
  message-sheet:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.action}"
    padding: "19px"
  select:
    backgroundColor: "transparent"
    textColor: "#375c4e"
    rounded: "{rounded.control}"
    padding: "8px"
    width: "100%"
---

# Design System: Message 官网

## Overview

**Creative North Star: "消息的交接"**

延展现有产品的中继青，以饱和色块、轻盈消息纸片、明确箭头和宽松中文排版，把路由呈现为可理解的交接过程。大标题负责承诺，消息内容与真实产品界面负责解释；细线与留白组织信息。

这是官网目录的局部设计记录，依据当前可本地审阅的实现，不代表已获发布批准。产品工作台继续遵循根目录 DESIGN.md。自托管的 Message Sans 来自 Noto Sans SC 子集；所有演示内容与截图均使用模拟数据，账号与凭据不进入展示内容。

**Key Characteristics:**

- 中继青构成宽阔舞台，浅薄荷突出行动与消息交接。
- 中文大标题与较轻说明形成明确层级。
- 消息纸片、路由线、真实产品截图共同解释机制。
- 动作响应与抵达动画有目的，减少动态偏好下可静态理解。

## Colors

单一青色家族承接产品身份，薄荷作为明亮伴色，纸白与墨字维持长页阅读。

### Primary

- **中继青**：饱和章节舞台、主要按钮、选中标签、规则开启和路由图标。
- **深中继青**：主要按钮悬停、薄荷表面上的强调文字。

### Secondary

- **抵达薄荷**：青色舞台上的主要行动、品牌图形与移动路径提示。

### Neutral

- **墨字**：纸白表面上的正文与标题。
- **说明灰**：章节说明、辅助链接与产品图注。
- **纸白**：页面背景。
- **细线灰绿**：分区、标签和信息条的边界。
- **内容白 / 薄荷纸面**：消息与产品截图容器、被选中的部署路径。
- **青底亮字**：饱和青色舞台上的标题与辅助说明共用高对比亮字；字号和字重表达层级。
- **焦点蓝**：键盘焦点的独立功能色。

普通正文与辅助说明的文字对比度底线为 4.5:1；浅色产品标签采用说明灰，青底说明采用青底亮字。

### Named Rules

**The Teal Stage Rule.** 饱和中继青可以占据整个章节；在浅色区域，青色集中表达行动、选中项与路由关系。

## Typography

**Display Font:** Message Sans，后备 Segoe UI、Microsoft YaHei、Noto Sans SC、sans-serif。

**Body Font:** 同一字体家族。

**Label/Mono Font:** 命令区使用 Consolas、monospace。

**Character:** 中文笔画清楚、标题紧凑有力；说明更轻、更疏朗。标题会随视口缩放，操作文字保持稳定尺寸。

### Hierarchy

- **Display**：首屏承诺；桌面使用 frontmatter 的 display，手机采用 clamp(36px,9.4vw,55px)、行高 1.3，最窄视口有更小适配。
- **Headline**：章节标题；手机常规章节为 30px、行高 1.45。
- **Title**：能力条目与部署说明标题；手机为 19px。
- **Body**：正文基线；较长章节说明使用约 1.9–1.95 的行高。
- **Label**：主要行动及当前选中项；手机按钮采用 12px，桌面保持较高字重。

### Named Rules

**The Chinese First Rule.** 以中文标题和说明为主要阅读层级；技术命令使用等宽字体，不用系统字体另设装饰性展示角色。

## Layout

内容宽度最多 1240px，常规桌面两侧合计留出 112px；页头可宽至 1392px。常规章节上下留白 112px，在 1150px 以下缩为 85px，600px 以下缩为 64px，手机两侧各留 20px。

大屏采用不同权重的双栏说明与内容区；交接演示是来源、规则、目标三栏。850px 以下复杂说明区转单栏，演示规则区移到两端消息下方；600px 以下演示按来源、规则、目标顺序垂直排列。导航在手机变为可展开菜单。产品图支持放大，移动端通过大图对话框横向查看。

布局断点为 1600px、1150px、850px、600px、360px。空间遵循可见信息分组，紧密控件使用小间距，章节用明显留白分开；没有为所有局部尺寸建立统一倍数刻度。

## Elevation & Depth

以色面和细线形成大部分层次。柔和投影只支持叠放消息、产品截图、模态大图与短暂提示；普通能力列表保留平面结构。纸片倾斜和少量透视式移动表达交接，不为每块内容添加浮层效果。

### Shadow Vocabulary

- **消息抵达** (`0 24px 55px #003e372e`)：首屏抵达纸片的叠放层次。
- **产品展示** (`0 24px 65px #19372b0f`)：桌面产品截图与底色的柔和分离。
- **模态大图** (`0 25px 85px #173a3340`)：交互打开后的集中查看层。

### Named Rules

**The Handoff Motion Rule.** 动态说明消息抵达与规则处理：柔和进场、轻微纵向差动、路径移动和结果出现；保持滚动自由，并尊重 prefers-reduced-motion。

## Shapes

按钮与消息内容使用柔和小圆角；较大纸片与截图容器适当放大圆角。圆形用于头像、状态点和轨道；开关采用胶囊轨道。细线定义分组与选中标签下沿。图标使用一致的 SVG 线条，常规尺寸 21px、线宽 1.65，端点与连接圆润。

## Components

### Buttons

明确、易识别的行动。

- **Shape:** action 圆角，桌面最小高度 52px，手机 48px。
- **Primary:** 浅色区使用中继青底白字；饱和青色区使用薄荷底深字。
- **Hover / Focus:** 悬停上移 3px，按下回到原位；焦点使用 3px 焦点蓝描边、5px 外偏移。
- **Disabled:** 处理中取消位移、降低不透明度并使用等待指针。

### Cards / Containers

消息与证据优先。

- **Message:** 白色内层、action 圆角、19px 内边距；首屏交接纸片使用 sheet 圆角并允许轻微倾斜。
- **Product:** 白色截图容器、sheet 圆角、细线窗口栏；手机收紧圆角。
- **Shadow Strategy:** 仅交接舞台与截图展示采用对应柔和投影。

### Inputs / Fields

演示规则控件保持可操作、可解释。

- **Select:** 透明底、细灰绿边框、control 圆角，具有关联说明标签；手机最小高度增为 42px。
- **Switch:** 青色表示开启，灰绿表示关闭，白色圆点移动；键盘焦点环落在可见轨道上。
- **State:** 手动查看模式中禁用相关规则；切换条件会重置演示结果。

### Navigation

页头采用文字导航，悬停时青色下划线从左侧展开。手机菜单用真实按钮控制展开状态，支持 Escape 和点击外部收起。章节内标签使用底部青色线与更高字重标识选中项，支持方向键、Home、End 和 roving tabindex。

### Message Handoff

来源纸片、Message 中继标志和目标消息组成方向明确的组合。首轮进场依次出现；来源和目标随自然滚动产生轻微相反方向偏移。规则演示显式区分等待、处理中、跳过与模拟完成；当前实现没有连接真实消息后端。动态时序、缓动与断点记录在同目录 sidecar。

### Product Preview

标签切换真实产品界面的模拟数据截图；点击截图或放大按钮打开原生对话框。图注和可访问名称随当前界面切换，关闭后恢复页面滚动。

## Do's and Don'ts

### Do:

- **Do** 用中继青延续产品身份，在饱和色面上用薄荷突出行动。
- **Do** 用清楚的来源、规则、目标和显式模拟状态解释消息过程。
- **Do** 保留键盘焦点、标签键盘导航和减少动态偏好支持。
- **Do** 保持正文与辅助说明至少 4.5:1 的文字对比度。
- **Do** 展示经过账号隔离的模拟消息与产品截图。

### Don't:

- **Don't** 把模拟完成状态描述为真实账号连接或真实通知抵达。
- **Don't** 用长时间滚动锁定承载交接动画。
- **Don't** 把官网的宽松章节排版覆盖到根目录的运维工作台规范。
