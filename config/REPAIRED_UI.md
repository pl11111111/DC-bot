# 安全修复相关界面的多语言

本次覆盖 `modules/admin.py`、`modules/social.py`、`modules/new_trading.py` 中的用户提示、管理菜单、按钮、表单、退款核查、频道关闭提醒，以及右键用户 → Apps 中的购买／出售指令。`utils/security.py` 没有用户界面文字，无需改动。

新增译文集中在 **`config/repaired_ui.json`**，按项目原有 11 种语言分组。已有交易表单、收款弹窗和交易卡片仍使用原来的语言文件，本次未覆盖其译文。

## 语言选择

- 新社群的个人交互：使用 `NEW.LANGUAGES` 中配置的语言身份组；没有语言身份组时使用英语。
- 交易双方共用的提醒：分别使用买家和卖家的语言；关闭按钮在两种语言不同时显示双语。
- 旧社群的个人交互：使用 Discord 客户端语言；没有个人语言信息时使用社群的默认语言。
- 定时通知、管理员告警和论坛通用按钮：使用社群默认语言。
- 右键 Apps 指令名称、斜杠指令名称／说明／选项：由 Discord 按客户端语言显示。Discord 的指令名称语言列表不包含马来语和 Tagalog；这两种语言的点击后提示和按钮仍通过语言身份组提供。参见 [Discord 官方语言列表](https://docs.discord.com/developers/reference#locales)。

## 修改译文

只修改语言分组下面的文字值，保留键名和所有 `{...}` 占位符。金额、地址、交易凭证、用户名和用户填写的原因会在翻译完成后填入，不能改写。

- `legacy_000`—`legacy_042`：管理、积分发放、抽奖、市场删除菜单。
- `legacy_043`—`legacy_121`：邀请、排名、boost 和积分通知。已停用的功能保持停用。
- `review_preview`、`manual_preview`、`manual_*`、`confirm_decision`：订单核查、手动退款确认及退款凭证。
- `close_*`、`idle_*`：关闭频道按钮及超时提醒。
- `event_*`、`alert_*`、`deposit_status`：管理员告警。
- `context_buy`、`context_sell`：右键用户 Apps 指令名称。
- `cmd_*`、`opt_*`、`choice_*`、`decision_*`：指令说明和选项。
- `status_*`：订单当前状态。
- `forum_prompt`、`forum_tags`：默认论坛交易提示。`new_content.json` 中自行编写的非默认论坛文案会原样保留。

配置在启动时读取；修改后需要重启机器人。已经发送的旧消息不会被批量重写，新消息及正常刷新后的面板会使用新译文。指令名称在机器人重新同步 Discord 指令后生效。

不要改变退款提示中的业务含义：买家退款包含实际到账的担保费和识别尾数，仅扣网络费；登记手动退款只记账、不再次转账；凭证本身不证明资金归属；未知结果不能重复提交。

## 同步文件

本轮运行时文件需一起同步：

- 修改：`modules/admin.py`、`modules/social.py`、`modules/new_trading.py`。
- 新增：`utils/ui_language.py`、`config/repaired_ui.json`。

本轮不需要数据库迁移。前几轮安全修复尚未上线时，仍需同时同步那些修复。本次仅修改、测试本地项目，没有部署或操作真实资金。

回归测试：`tests/test_repaired_ui.py` 为新增测试；`tests/test_new_features.py`、`tests/test_trade_recovery.py` 更新了语言相关断言。覆盖 11 种语言的键与占位符、Discord 长度限制、原始输入保留、权限与状态判断、退款通知和右键指令方向。

## 交易图片（2026-10-04）

右键用户 Apps 的购买、出售入口以及论坛发起入口，共用交易表单中的可选图片栏。每单 1 张 JPG/JPEG、PNG 或 WebP，文件不超过 5 MiB，静态图片不超过 1600 万像素，单边不超过 8192 像素。不填写图片仍可创建订单。

新增译文键 `image_label`、`image_hint`、`image_alt`、`image_invalid`、`image_limits`、`image_busy` 已配置全部 11 种语言。用户上传的图片不参与翻译。`image_hint` 应保持 100 字符以内。

`image_alt` 同时作为图片上方的可见说明及替代文字，明确图片仅用于展示、不要按图片内地址或二维码付款，避免与系统付款信息混淆。

图片验证后重新编码为 JPEG，最长边不超过 1280 像素、成品不超过 512 KiB；去除 EXIF/GPS、注释、色彩配置及追加内容。每进程最多同时处理 2 张图片，每个社群用户的图片上传预算为 3 次，持续按每分钟 3 次补充；额度不足要求稍后重新打开表单。原有订单数量及资金限制继续生效。

使用现有 `settings` 表的 `trade_image:<订单ID>` 保存编码后的图片及校验值，与订单同一事务提交。物品图片仅在第一步待确认（`pending`）的交易卡片展示，该步骤刷新或重启恢复时重新上传保存的图片，不依赖临时附件链接；后续步骤不再附带物品图片。第一步的历史消息和数据库图片保留，不自动删除。每单图片数据库占用最多约 683 KiB（Base64），备份时需包含这些记录。

本次图片功能运行时文件需一起同步：修改 `modules/new_trading.py`、`utils/trade_card.py`、`config/repaired_ui.json`；新增 `utils/trade_images.py`。无需数据库迁移或新增依赖，使用当前锁定的 Pycord 2.8.1、Pillow 12.3.0 和 aiohttp。同步后重启机器人；本地测试没有连接真实 Discord 或执行资金操作。
