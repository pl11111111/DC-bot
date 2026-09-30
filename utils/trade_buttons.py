"""Compact bilingual labels; custom IDs and action authorization stay unchanged."""
ACTIONS=('confirm','cancel','pay','payment_info','payment_help','ship','receipt','collect','refund','dispute','keep')
ROWS={
'English':('Confirm trade','Cancel trade','Payment details','Payment help','Contact admin','Mark as shipped','Confirm receipt','Claim funds','Claim refund','Open dispute','Keep channel / Admin'),
'中文':('确认交易','取消交易','获取付款信息','付款说明','呼叫管理员','标记为已发货','确认收货','领取货款','领取退款','发起争议','保留频道／通知管理员'),
'日本語':('取引を確認','取引を取消','支払情報','支払方法','管理者に連絡','発送済みにする','受取を確認','代金を受取','返金を受取','異議を申立','チャンネル保持／管理者'),
'한국어':('거래 확인','거래 취소','결제 정보','결제 안내','관리자 문의','발송 완료','수령 확인','대금 수령','환불 수령','분쟁 신청','채널 유지 / 관리자'),
'Bahasa Indonesia':('Konfirmasi transaksi','Batalkan transaksi','Info pembayaran','Panduan bayar','Hubungi admin','Tandai terkirim','Konfirmasi penerimaan','Terima dana','Terima pengembalian','Ajukan sengketa','Simpan kanal / Admin'),
'Bahasa Melayu':('Sahkan transaksi','Batal transaksi','Maklumat bayaran','Panduan bayaran','Hubungi pentadbir','Tandakan dihantar','Sahkan penerimaan','Terima bayaran','Terima bayaran balik','Buka pertikaian','Kekalkan saluran / Admin'),
'Tagalog':('Kumpirmahin ang trade','Kanselahin ang trade','Detalye ng bayad','Gabay sa pagbabayad','Kontakin ang admin','Markahang naipadala','Kumpirmahing natanggap','Kunin ang bayad','Kunin ang refund','Magbukas ng dispute','Panatilihin / Admin'),
'Português':('Confirmar negociação','Cancelar negociação','Dados do pagamento','Como pagar','Contatar admin','Marcar como enviado','Confirmar recebimento','Receber pagamento','Receber reembolso','Abrir disputa','Manter canal / Admin'),
'Español':('Confirmar operación','Cancelar operación','Datos del pago','Cómo pagar','Contactar admin','Marcar como enviado','Confirmar recepción','Cobrar fondos','Recibir reembolso','Abrir disputa','Mantener canal / Admin'),
'ภาษาไทย':('ยืนยันการซื้อขาย','ยกเลิกการซื้อขาย','ข้อมูลชำระเงิน','วิธีชำระเงิน','ติดต่อผู้ดูแล','ทำเครื่องหมายว่าส่งแล้ว','ยืนยันรับสินค้า','รับเงินค่าสินค้า','รับเงินคืน','เปิดข้อพิพาท','เก็บช่อง / ติดต่อผู้ดูแล'),
'Tiếng Việt':('Xác nhận giao dịch','Hủy giao dịch','Thông tin thanh toán','Hướng dẫn trả tiền','Liên hệ quản trị','Đánh dấu đã giao','Xác nhận đã nhận','Nhận tiền','Nhận tiền hoàn','Mở tranh chấp','Giữ kênh / Quản trị')}

def label(action,row):
    if action=='collect' and row['status']=='refund_ready': action='refund'
    index=ACTIONS.index(action)
    languages=list(dict.fromkeys(lang if lang in ROWS else 'English' for lang in row.get('_languages',['English'])))
    return ' / '.join(ROWS[lang][index] for lang in languages)

def localize_references(value,language):
    """Resolve button names from the same labels used to render the buttons."""
    import re
    if isinstance(value,dict): return {k:localize_references(v,language) for k,v in value.items()}
    if isinstance(value,list): return [localize_references(v,language) for v in value]
    if not isinstance(value,str): return value
    aliases={'Get payment information':'pay','Payment details':'pay','Confirm trade':'confirm',
        'Cancel trade':'cancel','Payment instructions':'payment_info','Contact admin':'payment_help',
        'Mark as shipped':'ship','Confirm receipt':'receipt','Claim funds':'collect','Claim refund':'refund',
        'Open dispute':'dispute','Keep channel / Contact admin':'keep','Keep channel':'keep'}
    if language=='中文': value=value.replace('Get payment information（获取付款信息）','获取付款信息')
    names={name.lower():action for name,action in aliases.items()}
    pattern='|'.join(re.escape(name) for name in sorted(aliases,key=len,reverse=True))
    return re.sub(pattern,lambda m:ROWS.get(language,ROWS['English'])[ACTIONS.index(names[m[0].lower()])],value,flags=re.IGNORECASE)
