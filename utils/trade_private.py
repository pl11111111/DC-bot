"""Short private transaction acknowledgements in the acting user's language."""
from utils.payout_language import language

ROWS={
'English':('Payment details are ready. Please check the trading channel.','Action completed.','Receipt confirmed.'),
'中文':('付款信息已生成，请查看交易频道。','操作完成。','已确认收货。'),
'日本語':('支払情報が生成されました。取引チャンネルを確認してください。','操作が完了しました。','受取を確認しました。'),
'한국어':('결제 정보가 생성되었습니다. 거래 채널을 확인하세요.','처리가 완료되었습니다.','수령이 확인되었습니다.'),
'Bahasa Indonesia':('Informasi pembayaran sudah tersedia. Silakan periksa kanal transaksi.','Tindakan selesai.','Penerimaan dikonfirmasi.'),
'Bahasa Melayu':('Maklumat bayaran telah tersedia. Sila semak saluran transaksi.','Tindakan selesai.','Penerimaan disahkan.'),
'Tagalog':('Handa na ang detalye ng bayad. Tingnan ang trading channel.','Tapos na ang aksyon.','Nakumpirma ang pagtanggap.'),
'Português':('Os dados do pagamento estão prontos. Confira o canal da negociação.','Ação concluída.','Recebimento confirmado.'),
'Español':('Los datos del pago están listos. Consulta el canal de la operación.','Acción completada.','Recepción confirmada.'),
'ภาษาไทย':('สร้างข้อมูลชำระเงินแล้ว โปรดตรวจสอบช่องซื้อขาย','ดำเนินการเสร็จแล้ว','ยืนยันรับสินค้าแล้ว'),
'Tiếng Việt':('Đã tạo thông tin thanh toán. Vui lòng kiểm tra kênh giao dịch.','Thao tác hoàn tất.','Đã xác nhận nhận hàng.')}

def text(member,key):
    return ROWS.get(language(member),ROWS['English'])[('payment_ready','done','receipt_done').index(key)]
