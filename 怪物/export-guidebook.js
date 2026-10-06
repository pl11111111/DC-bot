// Run in the console of the already-open https://mscw-guidebook.com page.
// Exports only public game data, not cookies, browser storage or account data.
(async () => {
  if (location.origin !== 'https://mscw-guidebook.com') {
    throw new Error('请在 https://mscw-guidebook.com 网页的控制台运行。');
  }
  const files = ['monsters.json', 'maps.json', 'items.json', 'translations.zh-CN.json'];
  const bundle = {
    source: location.origin,
    exported_at: new Date().toISOString(),
    data: {},
    errors: {},
  };
  for (const name of files) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60000);
    try {
      const response = await fetch('/AppData/' + name, {
        credentials: 'same-origin',
        signal: controller.signal,
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      // Challenge pages are not JSON and will be rejected here.
      const value = await response.json();
      if (value === null || typeof value !== 'object') throw new Error('数据格式不是 JSON 对象或数组');
      bundle.data[name] = value;
      console.log('已读取：' + name);
    } catch (error) {
      bundle.errors[name] = String(error.message || error);
      console.warn('读取失败：' + name, bundle.errors[name]);
    } finally {
      clearTimeout(timer);
    }
  }
  if (!Object.keys(bundle.data).length) {
    console.error('没有取得数据，未生成导出文件。', bundle.errors);
    return;
  }
  const blob = new Blob([JSON.stringify(bundle)], {type: 'application/json;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'guidebook-export.json';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  console.log('导出完成：guidebook-export.json。请放入项目的“怪物”文件夹。');
  if (Object.keys(bundle.errors).length) {
    console.warn('部分数据未取得，失败原因已记录在导出文件的 errors 字段。');
  }
})();
