# 频道面板与交易权限更新

按目录覆盖 `/opt/discordbot`：`modules/new_community.py`、`modules/new_trading.py`、`utils/notice_card.py`、`png/Language.png`。无需修改环境变量、依赖或数据库结构。

```bash
sudo systemctl restart discordbot
```

- 已发布的自定义验证面板：重新执行 `/new_panel channel:目标频道` 并保存一次，验证按钮将移到卡片下方。编辑时不填写 verification 会保留原设置。
- 语言面板：启动后的维护任务会使用 `png/Language.png` 更新。Linux 文件名区分大小写，必须保留大写 L。
- 商品交付方式、期限及特别约定改为选填，空值保存为空字符串。
- 新建交易频道明确向双方开放查看、发言、读取消息历史权限，默认身份组仍不可见。Bot 启动后会扫描数据库中记录的未结束交易频道，仅补齐双方的这些权限，保留其他权限设置；权限修复失败会写入日志并重试。
- 新建交易卡片后单独发送双方的 @ 通知，只允许提及该订单的买卖双方。通知弹窗或推送是否出现仍受用户自己的 Discord 设置影响。

39 项相关离线测试通过；未连接生产 Discord 或操作真实资金。
