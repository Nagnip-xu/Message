const $ = selector => document.querySelector(selector);
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const repo = 'https://github.com/Nagnip-xu/Message';

// All data below is illustrative. The site never connects to a relay backend.
const messages = {
  new: { title: '新版本已发布，来看看这次更新。', body: '新的任务规则，已经准备就绪。', kind: '新消息' },
  edit: { title: '发布说明已补充，安装步骤更清楚了。', body: '这是一条原消息编辑后的内容。', kind: '消息更新' },
  noise: { title: '今天聊点别的，周末准备去哪里？', body: '这条消息不包含你设置的关键词。', kind: '新消息' },
};

function keyboardTabs(container, activate) {
  const tabs = [...container.querySelectorAll('[role=tab]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activate(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); activate(tabs[next]); tabs[next].focus(); }
    });
  });
  return tab => {
    tabs.forEach(item => { item.setAttribute('aria-selected', String(item === tab)); item.tabIndex = item === tab ? 0 : -1; });
  };
}

let mode = 'relay';
let generation = 0;
const sleep = ms => new Promise(resolve => setTimeout(resolve, reducedMotion.matches ? 0 : ms));
function resetDemo() {
  generation++;
  $('.demo-routing').classList.remove('processing');
  $('#send-demo').disabled = false;
  $('#send-demo').innerHTML = '<svg class="icon"><use href="#i-plane"/></svg>模拟发送消息';
  $('.demo-destination').classList.remove('delivered');
  $('#demo-status').textContent = mode === 'relay' ? '等待演示' : '示例：消息留在 Telegram';
  $('#demo-result').innerHTML = `<div class="result-placeholder"><svg class="icon"><use href="#${mode === 'relay' ? 'i-feishu' : 'i-monitor'}"/></svg><h3>${mode === 'relay' ? '重要消息，有个好去处。' : '你需要再去看一眼。'}</h3><p>${mode === 'relay' ? '点击“模拟发送消息”，<br>看看这一条消息如何抵达。' : '没有转发时，需要主动打开<br>Telegram，查看来源中的消息。'}</p></div>`;
}
const selectMode = keyboardTabs($('.journey-mode'), tab => {
  selectMode(tab);
  mode = tab.id === 'mode-relay' ? 'relay' : 'manual';
  $('#journey-panel').classList.toggle('manual-mode', mode === 'manual');
  $('#journey-panel').setAttribute('aria-labelledby', tab.id);
  $('#rule-edit').disabled = mode === 'manual';
  $('#rule-keyword').disabled = mode === 'manual';
  resetDemo();
});
$('#message-type').addEventListener('change', () => {
  const message = messages[$('#message-type').value];
  $('#sample-title').textContent = message.title;
  $('#sample-body').textContent = message.body;
  resetDemo();
});
['#rule-edit', '#rule-keyword'].forEach(selector => $(selector).addEventListener('change', resetDemo));
$('#send-demo').addEventListener('click', async () => {
  const current = ++generation;
  const type = $('#message-type').value;
  const message = messages[type];
  const button = $('#send-demo');
  button.disabled = true;
  $('.demo-routing').classList.add('processing');
  $('#demo-status').textContent = '示例消息已出现在 Telegram';
  await sleep(500);
  if (current !== generation) return;
  if (mode === 'manual') {
    $('#demo-result').innerHTML = '<div class="result-placeholder"><svg class="icon"><use href="#i-monitor"/></svg><h3>消息在原处，等你打开。</h3><p>这次没有转发到飞书。<br>切换到“交给 Message”试试。</p></div>';
    $('#demo-status').textContent = '演示结束 · 需主动查看 Telegram';
  } else {
    $('#demo-status').textContent = 'Message 正在匹配示例规则';
    await sleep(600);
    if (current !== generation) return;
    const filtered = type === 'edit' && !$('#rule-edit').checked ? '已关闭消息编辑同步' : $('#rule-keyword').checked && !message.title.includes('发布') ? '未命中关键词“发布”' : '';
    if (filtered) {
      $('#demo-result').innerHTML = '<div class="result-placeholder"><svg class="icon"><use href="#i-check"/></svg><h3>这一条，留在原处。</h3><p></p></div>';
      $('#demo-result p').textContent = filtered + '，按规则跳过转发。';
      $('#demo-status').textContent = '演示结束 · 已按规则过滤';
    } else {
      const now = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date());
      $('#demo-result').innerHTML = `<div class="result-message"><strong>【${message.kind}】产品动态</strong><h3></h3><p>发送人：林同学<br>原消息：${now} (UTC+8)<br>转发：${now} (UTC+8)</p><span class="message-link">原消息链接会随通知展示</span></div>`;
      $('#demo-result h3').textContent = message.title;
      $('.demo-destination').classList.add('delivered');
      $('#demo-status').textContent = '演示完成 · 这是模拟通知';
    }
  }
  button.disabled = false;
  $('.demo-routing').classList.remove('processing');
  button.innerHTML = '<svg class="icon"><use href="#i-replay"/></svg>再演示一次';
});

const views = {
  workbench: { image: 'assets/workbench.webp', label: '监听工作台', description: '一个工作台，看清来源、目标与任务状态。', alt: 'Message 监听工作台，展示连接状态和模拟监听任务' },
  rules: { image: 'assets/rules.webp', label: '转发规则', description: '按任务调整消息编辑、断线补发和关键词规则。', alt: 'Message 规则编辑面板，展示编辑同步开关及关键词配置，使用模拟数据' },
  history: { image: 'assets/history.webp', label: '发送记录', description: '查看正文、来源和发送结果，问题可以逐条核对。', alt: 'Message 发送记录，展示三条已接收的模拟消息' },
};
let currentView = views.workbench;
const selectView = keyboardTabs($('.product-tabs'), tab => {
  selectView(tab);
  currentView = views[tab.dataset.view];
  $('#product-image').src = currentView.image;
  $('#product-image').alt = currentView.alt;
  $('#window-label').textContent = 'Message / ' + currentView.label;
  $('#product-description').textContent = currentView.description;
  $('#product-panel').setAttribute('aria-labelledby', tab.id);
});
const imageDialog = $('#image-dialog');
function openImage() {
  $('#dialog-image').src = currentView.image;
  $('#dialog-image').alt = currentView.alt;
  $('#dialog-label').textContent = currentView.label + ' · 模拟数据';
  imageDialog.showModal();
  document.body.style.overflow = 'hidden';
}
$('#expand-preview').addEventListener('click', openImage);
$('#screenshot-button').addEventListener('click', openImage);
$('#close-image').addEventListener('click', () => imageDialog.close());
imageDialog.addEventListener('close', () => { document.body.style.overflow = ''; });
imageDialog.addEventListener('click', event => { if (event.target === imageDialog) { const box = imageDialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) imageDialog.close(); } });

const setups = {
  windows: { title: '先把 Message 带到你的电脑。', description: '安装 Python 3.11+、Node.js 22.12+ 和 Git，然后在终端运行：', command: `git clone ${repo}.git\ncd Message\n.\\Start.cmd`, next: '首次启动会安装依赖并构建页面。打开后，依次配置 Telegram、飞书机器人和监听任务。', link: repo + '#windows-安装与启动', action: '查看完整 Windows 指南' },
  server: { title: '电脑关机了，服务器继续听。', description: '支持 Ubuntu 与 Alibaba Cloud Linux 3。部署后，用 systemd 保持后台运行：', command: 'sudo systemctl enable --now message-relay\nsudo systemctl status message-relay', next: '上方命令用于完成安装后的启动与检查。按完整指南安装依赖、构建前端并配置服务；通过 SSH 隧道管理，无需域名。', link: repo + '#linux-服务器部署', action: '查看完整服务器部署指南' },
};
let currentSetup = setups.windows;
const selectSetup = keyboardTabs($('.setup-tabs'), tab => {
  selectSetup(tab);
  currentSetup = setups[tab.dataset.setup];
  $('#setup-title').textContent = currentSetup.title;
  $('#setup-description').textContent = currentSetup.description;
  $('#setup-command').textContent = currentSetup.command;
  $('#setup-next').textContent = currentSetup.next;
  $('#setup-guide').href = currentSetup.link;
  $('#setup-guide').innerHTML = currentSetup.action + ' <svg class="icon"><use href="#i-external"/></svg>';
  $('#setup-panel').setAttribute('aria-labelledby', tab.id);
});
let toastTimer;
function toast(message) { clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').hidden = false; toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 3500); }
$('#copy-command').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(currentSetup.command); toast('安装命令已复制'); }
  catch { const range = document.createRange(); range.selectNodeContents($('#setup-command')); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); toast('请按 Ctrl+C 或长按复制选中的命令'); }
});
const menuButton = $('.menu-button');
function closeMenu() { $('#main-nav').classList.remove('open'); menuButton.setAttribute('aria-expanded', 'false'); menuButton.setAttribute('aria-label', '打开导航'); }
menuButton.addEventListener('click', () => { const open = $('#main-nav').classList.toggle('open'); menuButton.setAttribute('aria-expanded', String(open)); menuButton.setAttribute('aria-label', open ? '关闭导航' : '打开导航'); });
$('#main-nav').querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
document.addEventListener('keydown', event => { if (event.key === 'Escape' && $('#main-nav').classList.contains('open')) { closeMenu(); menuButton.focus(); } });
document.addEventListener('click', event => { if (!event.target.closest('.site-header')) closeMenu(); });

// A single scroll-linked handoff continues the hero's arrival; no scroll lock.
let framePending = false;
window.addEventListener('scroll', () => {
  if (framePending || reducedMotion.matches) return;
  framePending = true;
  requestAnimationFrame(() => {
    const distance = Math.min(window.scrollY, 750);
    $('.source-slip').style.translate = `0 ${-distance * .055}px`;
    $('.destination-slip').style.translate = `0 ${distance * .045}px`;
    framePending = false;
  });
}, { passive: true });
