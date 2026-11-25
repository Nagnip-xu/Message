// Public documentation screenshots must only use the isolated test service.
import {chromium,expect} from '@playwright/test';
import fs from 'node:fs/promises';

const base=process.env.MESSAGE_TEST_URL || 'http://127.0.0.1:8766';
const browser=await chromium.launch({headless:true,...(process.env.MESSAGE_TEST_BROWSER ? {channel:process.env.MESSAGE_TEST_BROWSER} : {})});
try {
  const page=await browser.newPage({viewport:{width:1440,height:1050}});
  const state=await (await page.request.get(`${base}/api/state`)).json();
  if(!state.telegram.user?.name.includes('集成测试账号（测试数据）'))throw new Error('Refusing to capture a real account');
  await page.request.post(`${base}/__test__/reset`);
  await page.request.put(`${base}/api/image-settings`,{data:{clear:true}});
  const robot=await (await page.request.post(`${base}/api/robots`,{data:{name:'飞书信号通知（模拟数据）',webhook:'https://open.feishu.cn/open-apis/bot/v2/hook/ui-test-only'}})).json();
  const task=await (await page.request.post(`${base}/api/tasks`,{data:{name:'示例交易话题（模拟数据）',chat_id:-100123456,topic_id:88,robot_id:robot.id,sender_filter:'77'}})).json();
  await page.request.post(`${base}/__test__/ingest`);
  await expect.poll(async()=>(await (await page.request.get(`${base}/api/deliveries`)).json()).items.filter(d=>d.status==='sent').length).toBe(2);
  await fs.mkdir('../docs/images',{recursive:true});
  await page.goto(base);
  await page.screenshot({path:'../docs/images/workbench.png',animations:'disabled'});
  await page.getByRole('button',{name:'编辑 示例交易话题（模拟数据）',exact:true}).click();
  await page.locator('details.advanced-options>summary').click();
  await page.screenshot({path:'../docs/images/rule-editor.png',animations:'disabled'});
  await page.getByRole('button',{name:'关闭面板',exact:true}).click();
  await page.getByRole('button',{name:'新建监听任务',exact:true}).click();
  await page.getByRole('radio',{name:/集成测试群/}).waitFor();
  await page.screenshot({path:'../docs/images/create-task.png',animations:'disabled'});
  await page.getByRole('button',{name:'关闭面板',exact:true}).click();
  await page.getByRole('button',{name:'发送记录',exact:true}).click();
  await page.locator('tbody tr').first().waitFor();
  await page.screenshot({path:'../docs/images/preview.png',animations:'disabled'});
  await page.getByRole('button',{name:'运行设置',exact:true}).click();
  const section=page.getByRole('region',{name:'飞书图片上传',exact:true});
  await section.locator('summary').click();
  await section.scrollIntoViewIfNeeded();
  await section.screenshot({path:'../docs/images/image-setup.png',animations:'disabled'});
  console.log('Five public documentation screenshots captured with synthetic account, messages, names, and credentials only.');
} finally {await browser.close();}
