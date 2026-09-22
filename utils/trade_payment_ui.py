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


def payment_instructions(invoice,fee=None):
    amount=Decimal(str(invoice['amount']))
    hint=f'最终的**实际到账金额**必须为 **{amount:.6f} USDT**。'
    if fee is not None:
        fee=Decimal(str(fee))
        hint+=f'\n\n仅当币安从填写金额扣除 **{fee:f} USDT** 时，填写 **{amount+fee:.6f} USDT**。'
    hint+='\n\n若页面显示的到账金额已正确，**不要再次加手续费**。其他平台按其实际费用核对。'
    if fee is None: hint+='当前无法查询费用，请按提现页面核对最终到账金额。'
    return (f'### 钱包转账\n发送 **{amount:.6f} USDT**，普通 BSC 钱包的网络费另用 BNB 支付。请核对网络和金额。\n\n'
            f'### 币安或其他交易所提现\n{hint}\n\n'
            '### 为什么不能省略小数？\n小数尾数用于识别订单。请勿只付整数、商品价或保留两位小数；金额不一致可能无法自动识别。\n\n'
            '### 已付错或未识别\n点击「呼叫管理员」，在交易频道提供实际金额、交易哈希和付款截图。请勿重复付款或自行补差额。金额错误只能由管理员核实入款后手动退款；不收服务费，网络费从退款中扣除并告知。\n\n'
            '过期请勿转账；二维码仅包含地址，扫码后仍须核对网络和金额。\n\n'
            '### 邀请信息\nhttps://www.bsmkweb.cc/register?ref=1024490102\n邀请码：`1024490102`')


def copy_text(invoice):
    return (f"网络：USDT-BEP20\n实际到账金额：\n```\n{Decimal(str(invoice['amount'])):.6f}\n```"
            f"\n收款地址：\n```\n{invoice['address']}\n```\n截止：<t:{deadline(invoice)}:f>\n"
            '仅买家付款；小数尾数必须完整保留，不能四舍五入。请使用客户端复制功能，核对实际到账金额。转出手续费另行核对。\n'
            '付错金额或已付款未识别，请点击「付款有问题／呼叫管理员」，不要重复付款或自行补差额。')
