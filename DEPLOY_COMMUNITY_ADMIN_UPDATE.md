# 新社群指令、独立面板与论坛规则

## 部署

按更新包中的目录覆盖 `/opt/discordbot`。代码文件：

- `main.py`
- `utils/guild_isolation.py`
- `utils/new_config.py`
- `modules/new_community.py`
- `utils/notice_card.py`（新增，必须上传）
- `utils/trade_card.py`（共用组件常量）

新面板直接在指令中选择目标频道，无需新增规则或验证频道环境变量。已有 `NEW_RULES_CHANNEL_ID` / `NEW_VERIFY_CHANNEL_ID` 仍兼容；验证身份组继续使用 `NEW_INVITE_VERIFIED_ROLE_ID`。

```bash
sudo systemctl restart discordbot
```

无需迁移数据库。机器人登录时同步两边的社群指令并清理旧的全局注册。确认日志出现“斜杠命令同步完成”；Discord 客户端若仍显示旧菜单，重新打开指令菜单或重启客户端。旧社群的指令注册限定在旧社群，新模块指令限定在新社群。运行时权限检查继续保留。

## rules 与 verify

- `/new_panel channel:目标频道`：统一编辑标题、正文、横幅，预览确认后发布。新频道默认不带验证按钮。
- `/new_panel channel:目标频道 verification:True`：添加发放验证身份组的按钮。设为 False 可移除按钮；编辑时不填写则保留原设置。
- 再次选择同一频道会编辑该频道已有面板，不重复发布；每个频道各自保存一个面板。横幅填写 HTTPS 图片链接，留空移除。
- 旧 rules / verify 面板首次通过新命令编辑时会接管原消息。面板是否可以验证，以该频道保存的设置和最新消息 ID 为准，无需绑定固定验证频道。
- 编辑内容持久化存入现有 settings，重启后继续使用。后台维护不会用文件中的旧正文覆盖管理员修改。
- 未设置数据库内容时，可从 `config/new_content.json` 的 `rules_title/rules_body/rules_banner` 和 `verify_title/verify_body/verify_banner` 读取初始内容；旧的按频道 ID 的 `banners` 配置仍兼容。不要覆盖服务器已有文案文件。

## 论坛规则帖

使用 `/new_forum_rules forum:目标论坛`，输入标题、正文、横幅，以及论坛要求的标签 ID，预览确认后发布并置顶。

编辑：`/new_forum_rules forum:原论坛 message_id:机器人返回的消息ID`。会修改原规则帖的标题、正文、横幅并置顶，不重复新建。归档帖子会尝试重新打开。只支持本机器人通过通知模块发布且有保存记录的帖子。

`/new_notice` 继续用于一般通知，新增 `pin` 选项，可置顶论坛帖或普通通知；`copy_from` 仍用于复制重发。管理员可从频道选择器中选频道，不需要额外的频道白名单环境变量。

这些编辑命令要求服务器管理员或 `NEW_NOTICE_ADMIN_ROLE_IDS` 中的身份组。Bot 需要目标频道的查看、发送、嵌入链接、读取历史权限，置顶论坛帖还需要管理帖子权限。若内容保存成功但置顶失败，机器人会返回原消息 ID；修正权限后用该 ID 编辑重试，勿再次新建。

## 验证

预览、正式通知、论坛规则以及频道面板统一使用单卡片：顶部横幅和正文在同一个容器中。现有消息在下次编辑时转换。更新重启后请重新打开指令生成预览，旧临时预览的按钮不会跨重启恢复。

修正了用普通频道接口编辑临时预览引发的 404 Unknown Message：正式发布不再依赖修改预览按钮。权限或内容错误允许修正后重试；已有成功发送的消息会复用，不因保存记录或置顶失败而重复新建；结果未知时提示核对。

31 项针对性离线测试通过，包括指令归属、面板隔离、论坛规则及置顶、临时预览 webhook 与按钮回调注册、预览编辑不可用仍能发布、权限修正后重试和重复点击不重复发帖。实际 Discord 显示和权限需部署后验证。
