// Run once in the console of the open guidebook page. Downloads one JSON bundle.
(async () => {
  if (location.origin !== 'https://mscw-guidebook.com') throw new Error('请在资料站网页控制台运行。');
  if (window.guidebookAssetExportRunning) throw new Error('导出正在进行，请勿重复运行。');
  window.guidebookAssetExportRunning = true;
  try {
    const output = {source: location.origin, exported_at: new Date().toISOString(), images: {}, data: {}, scripts: {}, errors: {}, probes: []};
    async function get(path) {
      const url = new URL(path, location.origin + '/');
      if (url.origin !== location.origin) throw new Error('仅允许本站资源');
      const response = await fetch(url, {credentials: 'same-origin', signal: AbortSignal.timeout(30000)});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response;
    }
    const monsters = (await (await get('/AppData/monsters.json')).json()).monsters;
    const items = (await (await get('/AppData/items.json')).json()).items;
    if (!Array.isArray(monsters) || !Array.isArray(items)) throw new Error('数据格式已变化');
    const paths = [...new Set([...monsters, ...items].map(x => x.thumbnail).filter(Boolean))];
    let cursor = 0, completed = 0, totalBytes = 0;
    const dataUrl = blob => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('图片编码失败'));
      reader.readAsDataURL(blob);
    });
    async function readImage(path) {
      // dropCandidates maps indexed images to generated/assets before any legacy fallback.
      const relative = path.replace(/^\/+/, '').replace(/^AppData\//, '');
      if (!/^images\/(monsters|items)\/\d+\.png$/.test(relative)) throw new Error('未识别的图片路径');
      const resolved = '/generated/assets/' + relative.slice('images/'.length);
      const response = await get(resolved);
      const blob = await response.blob();
      if (!/^image\/(png|webp|gif|jpeg)(;|$)/i.test(blob.type)) {
        output.probes.push({path, resolved, final_url: response.url, type: blob.type,
          bytes: blob.size, response_preview: (await blob.slice(0, 200).text())});
        throw new Error('响应不是支持的图片：' + blob.type);
      }
      totalBytes += blob.size;
      if (blob.size > 5 * 1024 * 1024 || totalBytes > 128 * 1024 * 1024) {
        cursor = paths.length;
        throw new Error('图片体积超出导出上限，已停止后续读取');
      }
      output.images[path] = await dataUrl(blob);
    }
    let probeOK = true;
    for (const path of [monsters[0]?.thumbnail, items[0]?.thumbnail].filter(Boolean)) {
      try { await readImage(path); console.log('图片预检成功：' + path); }
      catch (error) { probeOK = false; output.errors[path] = String(error.message || error); }
    }
    async function worker() {
      while (cursor < paths.length) {
        const path = paths[cursor++];
        try {
          if (!output.images[path]) await readImage(path);
        } catch (error) {
          output.errors[path] = String(error.message || error);
        }
        completed++;
        if (completed % 50 === 0) console.log('图片进度：' + completed + '/' + paths.length);
        await new Promise(resolve => setTimeout(resolve, 150));
      }
    }
    if (probeOK) await Promise.all([worker(), worker()]);
    else console.warn('图片预检失败，已跳过批量读取。将导出路径诊断信息。');
    for (const path of ['/AppData/monster_drops.json', '/generated/data/asset-index.json']) {
      try { output.data[path] = await (await get(path)).json(); }
      catch (error) { output.errors[path] = String(error.message || error); }
    }
    // Preserve the public app entry source so the missing drop-data path can be inspected.
    // It is saved as text only and will not be executed by the importer.
    for (const script of document.querySelectorAll('script[type="module"][src]')) {
      const url = new URL(script.src);
      if (url.origin !== location.origin || !url.pathname.startsWith('/assets/')) continue;
      try {
        const content = await (await get(url.href)).text();
        if (content.length > 10000000) throw new Error('脚本资源过大');
        output.scripts[url.pathname] = content;
        const names = [...new Set(content.match(/(?:dropCandidates|MonsterIcon|OsmsDataImage|WzSprite|dropReportApi|framePackVersion)-[A-Za-z0-9_-]+\.js/g) || [])];
        for (const name of names) {
          const path = '/assets/' + name;
          try {
            const dependency = await (await get(path)).text();
            if (dependency.length > 10000000) throw new Error('脚本资源过大');
            output.scripts[path] = dependency;
          } catch (error) { output.errors[path] = String(error.message || error); }
        }
      } catch (error) { output.errors[url.pathname] = String(error.message || error); }
    }
    output.expected_images = paths.length;
    const url = URL.createObjectURL(new Blob([JSON.stringify(output)], {type: 'application/json'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'guidebook-assets.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    console.log('素材导出完成：成功 ' + Object.keys(output.images).length + '/' + paths.length + ' 张。请将 guidebook-assets.json 放入“怪物”文件夹。');
    if (Object.keys(output.errors).length) console.warn('有资源未取得，原因已保存在文件中。');
  } finally {
    window.guidebookAssetExportRunning = false;
  }
})();
