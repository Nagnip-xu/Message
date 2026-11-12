# Message 官网

这是 Message 的独立静态介绍页，包含消息流转演示、工作台截图、安装入口与常见问题。官网不读取 Telegram 账号、飞书机器人或监听任务；示例消息只在浏览器中演示。

**线上地址：[https://nagnip-xu.github.io/Message/](https://nagnip-xu.github.io/Message/)**

## 本地打开

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m http.server 8877 --bind 127.0.0.1 --directory website
```

浏览器进入 **http://127.0.0.1:8877/**。没有虚拟环境时，使用系统 `python` 执行相同参数即可。Ctrl+C 停止预览，监听服务不受影响。

## 文件说明

- `index.html`：中文内容与语义化页面结构。
- `styles.css`：响应式布局、青绿色主题及动效，支持减少动态效果偏好。
- `app.js`：模拟消息规则、界面预览、命令复制、键盘及移动导航。
- `assets/`：自托管字体、字体许可、网站图标与模拟数据工作台截图。

页面不需要 npm 安装或构建，不调用后端 API，也不依赖第三方字体 CDN。中文字体为 Noto Sans SC 的内容子集，新增文字可能使用系统字体回退；修改内容较多时应重新生成子集。字体许可见 `assets/OFL-NotoSansSC.txt`。

官网链接指向公开的 `Nagnip-xu/Message` 仓库。Windows 安装命令可复制；服务器区域展示的是完成安装后的 systemd 启动命令，完整安装步骤链接到项目 README。

## GitHub Pages 发布

本项目通过 `.github/workflows/pages.yml` 发布。仓库 Pages 的 Source 选择 **GitHub Actions**；提交官网修改到 `main` 后自动部署，也可以在 Actions 中手动运行 **Deploy Message website**。

工作流仅将 `index.html`、`styles.css`、`app.js` 与 `assets/` 打包发布。设计记录与本说明不进入站点产物。所有资源使用相对路径，兼容 GitHub Pages 的 `/Message/` 路径。

发布官网只提供介绍页，不会托管 Telegram 监听后台。已有服务器监听与这个页面独立运行。
