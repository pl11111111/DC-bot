"""Payment presentation only: no invoice creation or account mutations."""
from datetime import timezone
from decimal import Decimal
from io import BytesIO
import re
import discord
import qrcode


def deadline(invoice):
    value=invoice['expires_at']
    if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def qr_file(address):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}',address):
        raise ValueError('账单收款地址格式异常，请管理员核对')
    code=qrcode.QRCode(box_size=6,border=4,error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(address)
    code.make(fit=True)
    buffer=BytesIO()
    code.make_image(fill_color='black',back_color='white').save(buffer,format='PNG')
    buffer.seek(0)
    return discord.File(buffer,filename='payment-qr.png')


def payment_embed(row,invoice,fee=None):
    amount=Decimal(str(invoice['amount']))
    stamp=deadline(invoice)
    embed=discord.Embed(title=f'{amount:.6f} USDT',
        description='**网络：BSC / BEP20 · USDT**',color=0x9854DE)
    embed.add_field(name='链上必须实际到账',value=f'```\n{amount:.6f}\n```',inline=False)
    embed.add_field(name='收款地址',value=f"```\n{invoice['address']}\n```",inline=False)
    embed.add_field(name='付款截止',value=f'<t:{stamp}:f>（<t:{stamp}:R>）\n\n付错金额或到账未识别？请呼叫管理员，**不要重复付款或自行补差额**。',inline=False)
    embed.set_footer(text='订单 '+row['id'])
    return embed


def payment_instructions(invoice,fee=None,guide_url=None):
    amount=Decimal(str(invoice['amount']))
    guide=(f'[打开交易BOT指南]({guide_url})' if guide_url else '请联系管理员获取交易BOT指南。')
    return (f'### 实际应到账：{amount:.6f} USDT\n**必须完整保留 6 位小数，不要自行增加到账金额。**\n\n'
            '### 第一次付款？\n交易所可购买并发送 USDT；已有钱包也可付款，选择一种即可。\n'
            '复制订单地址 → 选择 USDT、BSC / BEP20 → 核对实际到账金额 → 确认一次付款。\n'
            f'{guide}\n\n'
            '### 币安或其他交易所\n币安若显示内部转账免手续费，不需要额外加钱。选择链上提现或使用其他交易所时，费用以页面为准。\n'
            f'最终显示的**实际到账金额必须为 {amount:.6f} USDT**；金额正确就不要再加手续费。\n\n'
            f'### 已有 BSC 钱包\n发送 **{amount:.6f} USDT**。普通钱包的网络费另用 BNB 支付，不加进 USDT 金额。\n\n'
            '### 付错或未识别\n点击「呼叫管理员」，提供实际金额、转账记录编号／交易哈希和截图。**不要重复付款或自行补差额。**\n'
            '金额错误须管理员核实后手动退款，网络费从退款中扣除并告知。\n\n'
            '过期请勿付款；二维码只有地址，扫码后仍须核对网络和金额。\n\n'
            '### 管理员核实提醒\n管理员不需要你的密码、助记词、私钥或验证码，请勿提供。\n'
            '**管理员不会以核对付款、退款或解冻资金为由发送链接，也不会要求你下载任何文件、安装软件或开启远程控制。**\n'
            '遇到此类要求，请停止操作，回到社群交易频道核实。\n\n'
            '### 推荐交易所\n[Binance（币安）注册邀请链接](https://www.bsmkweb.cc/register?ref=1024490102)\n邀请码：`1024490102`')


def copy_text(invoice):
    return (f"网络：USDT-BEP20\n实际到账金额：\n```\n{Decimal(str(invoice['amount'])):.6f}\n```"
            f"\n收款地址：\n```\n{invoice['address']}\n```\n截止：<t:{deadline(invoice)}:f>\n"
            '仅买家付款；小数尾数必须完整保留，不能四舍五入。请使用客户端复制功能，核对实际到账金额。转出手续费另行核对。\n'
            '付错金额或已付款未识别，请点击「付款有问题／呼叫管理员」，不要重复付款或自行补差额。')
