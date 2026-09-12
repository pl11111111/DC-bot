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
        description='**本订单实际应到账金额**\n请严格按此金额付款，包含识别尾数。\n**网络：USDT · BSC / BEP20**',color=0x9854DE)
    hint='转出手续费由买家承担，以付款平台显示的**实际到账金额**为准。'
    if fee is not None:
        fee=Decimal(str(fee))
        hint+=f'\n若币安从填写金额中扣除 {fee:f} USDT，请填写 **{amount+fee:.6f} USDT**。'
    hint+='\n若页面显示的到账金额已等于上方金额，请勿再次加手续费。费用可能变化，请在转出前核对。'
    embed.add_field(name='币安提币提示（BEP20）',value=hint,inline=False)
    embed.add_field(name='⏳ 付款截止',value=f'<t:{stamp}:f>（<t:{stamp}:R>）\n过期请勿转账；已付款请勿重复支付。',inline=False)
    embed.add_field(name='收款地址',value=f"```\n{invoice['address']}\n```\n下方二维码仅包含地址，请核对 USDT-BEP20 网络和到账金额。",inline=False)
    embed.set_footer(text='订单 '+row['id'])
    return embed


def copy_text(invoice):
    return (f"网络：USDT-BEP20\n实际到账金额：\n```\n{Decimal(str(invoice['amount'])):.6f}\n```"
            f"\n收款地址：\n```\n{invoice['address']}\n```\n截止：<t:{deadline(invoice)}:f>\n"
            '请使用客户端复制功能；转出手续费另行核对，已付款请勿重复转账。')
