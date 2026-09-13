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
    embed=discord.Embed(title=f'实际应到账：{amount:.6f} USDT',
        description=(f'**由买家付款。卖家仅查看进度，请等待系统确认到账，暂勿发货或代付。**\n\n'
                     f'**必须实际到账 {amount:.6f} USDT，所有小数位都要保留。**\n'
                     '小数尾数用于识别本订单。请勿只付整数、只付商品价、保留两位小数或四舍五入。\n'
                     '**先点击「复制付款信息」，再核对付款页面的实际到账金额，完全一致后才转账。**\n\n'
                     '**已付错金额或已付款未识别：不要重复付款、不要自行补差额，点击下方「付款有问题／呼叫管理员」。**\n\n'
                     '**网络：USDT · BSC / BEP20**\n请勿使用其他币种或网络。\n\n'
                     '下方二维码仅包含收款地址。扫码后，请核对币种、网络和付款金额。'),color=0x9854DE)
    embed.add_field(name='收款地址',value=f"```\n{invoice['address']}\n```",inline=False)
    hint=f'请以提现页面的 **“实际到账金额”** 为准，必须等于 **{amount:.6f} USDT**。'
    if fee is not None:
        fee=Decimal(str(fee))
        hint+=f'\n\n仅当币安从填写金额中扣除 **{fee:f} USDT** 时，请填写 **{amount+fee:.6f} USDT**。'
        hint+=f'\n\n若页面显示的实际到账已是 **{amount:.6f} USDT**，**请勿再次加 {fee:f}**。'
    else:
        hint+='\n\n请核对付款平台的实际到账金额，转出手续费由买家承担。\n\n若到账金额已等于上方金额，**请勿重复加手续费**。'
    hint+='\n\n其他交易所的费用可能不同，请按该平台的实际费用核对**实际到账金额**。'
    embed.add_field(name='🏦 使用币安或其他交易所提现',value=hint,inline=False)
    embed.add_field(name='binance邀请链接',value='https://www.bsmkweb.cc/register?ref=1024490102',inline=False)
    embed.add_field(name='邀请码',value='```\n1024490102\n```',inline=False)
    embed.add_field(name='⏳ 付款截止',value=f'<t:{stamp}:f>（<t:{stamp}:R>）\n\n过期请勿转账；已付款请勿重复支付。金额错误或未识别到账，请点击「付款有问题／呼叫管理员」。呼叫后会保留频道并暂停自动履约，管理员核实后处理；不会自动退款。',inline=False)
    embed.set_footer(text='订单 '+row['id'])
    return embed


def copy_text(invoice):
    return (f"网络：USDT-BEP20\n实际到账金额：\n```\n{Decimal(str(invoice['amount'])):.6f}\n```"
            f"\n收款地址：\n```\n{invoice['address']}\n```\n截止：<t:{deadline(invoice)}:f>\n"
            '仅买家付款；小数尾数必须完整保留，不能四舍五入。请使用客户端复制功能，核对实际到账金额。转出手续费另行核对。\n'
            '付错金额或已付款未识别，请点击「付款有问题／呼叫管理员」，不要重复付款或自行补差额。')
