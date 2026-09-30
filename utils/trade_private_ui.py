"""Private buyer/seller dialogs. Never expose untranslated internal exceptions."""
import re
from utils.payout_language import language
from utils.trade_buttons import label
KEYS=('form','item','amount','terms','created','member','disabled','denied','expired','help','hold','receipt','close','stale','error','price','limit','current','invoice')
ROWS={
'English':('Trade details','Item and quantity','Price USDT (min 5.01; max 2 decimals)','Delivery, deadline, special terms (optional)',
'Trade created: {channel}','Select another human member of this server.','Trading is unavailable. Contact an administrator.','You cannot perform this action on this order.',
'Payment expired or was already detected. Do not transfer. If paid, contact an administrator.',
'Administrator notified; the channel is retained and automatic processing is paused. Provide the amount, transaction hash and screenshot. Do not pay again or top up.',
'Automatic closure stopped; administrator notified. Explain why you need the channel kept. If paid, provide payment evidence.',
'Check the item first. Confirm only after receiving it: confirmation allows the seller to collect payment.',
'Confirmed. The channel will close shortly.','This action has expired. Use the latest order message.',
'Unable to complete this action. Contact an administrator for verification; do not repeat any payment or withdrawal.',
'Enter a valid amount with at most 2 decimals. Minimum: {minimum} USDT.',
'Each participant may have up to {limit} active orders. Complete or cancel existing orders first.',
'This step has ended. Current status: {status}. Use the buttons below.',
'Original payment: {amount} USDT\nUSDT · BSC / BEP20\nAddress: {address}\nDeadline (UTC): {deadline}\nDo not pay twice. Do not pay after expiry; contact an administrator.'),
'中文':('担保交易条件','商品名称及数量','商品价格 USDT（最低5.01，最多两位小数）','交付方式、期限及特别约定（选填）',
'交易已创建：{channel}','请选择本社群的其他真实成员。','交易暂不可用，请联系管理员。','你无权对该订单执行此操作。',
'账单已过期或付款已识别，请勿继续转账；已付款请联系管理员。',
'已通知管理员，频道已保留并暂停自动处理。请提供金额、交易哈希和截图，勿重复付款或补差额。',
'已停止自动关闭并通知管理员。请说明保留原因；如已付款，请提供凭证。',
'请核对商品，实际收到后才能确认；确认后卖家可领取货款。','已确认，即将关闭频道。','此操作已失效，请使用最新订单消息。',
'操作未能完成，请联系管理员核实，勿重复付款或提现。','请输入有效金额，最多两位小数，最低 {minimum} USDT。',
'每位参与者最多同时进行 {limit} 笔交易，请先完成或取消已有订单。','此步骤已结束，当前状态：{status}。请使用下方按钮。',
'原账单金额：{amount} USDT\nUSDT · BSC / BEP20\n地址：{address}\n截止时间（UTC）：{deadline}\n勿重复付款；过期勿转账，请联系管理员。'),
'日本語':('取引内容','商品名と数量','価格 USDT（最低5.01、小数2桁まで）','受渡方法・期限・特約（任意）',
'取引を作成しました：{channel}','このサーバーの別の利用者を選択してください。','取引は現在利用できません。管理者に連絡してください。','この注文に対してこの操作はできません。',
'支払期限切れ、または入金検出済みです。送金しないでください。支払済みなら管理者に連絡してください。',
'管理者に連絡し、チャンネルを保持して自動処理を停止しました。金額・取引ハッシュ・画像を提示してください。再送金や差額の追加はしないでください。',
'自動閉鎖を停止し管理者に通知しました。保持する理由と、支払済みなら支払証拠を提示してください。',
'商品を確認し、実際に受け取ってから確定してください。確定後は売り手が代金を受け取れます。','確認しました。まもなくチャンネルを閉じます。','この操作は無効です。最新の注文メッセージを使用してください。',
'操作を完了できません。管理者に確認を依頼し、支払いや出金を繰り返さないでください。','有効な金額を小数2桁以内で入力してください。最低額：{minimum} USDT。',
'各参加者の同時注文は {limit} 件までです。既存の注文を完了または取消してください。','この手順は終了しました。現在の状態：{status}。下のボタンを使用してください。',
'元の支払額：{amount} USDT\nUSDT · BSC / BEP20\nアドレス：{address}\n期限（UTC）：{deadline}\n重複支払・期限後の送金はしないでください。管理者に連絡してください。'),
'한국어':('거래 조건','상품명 및 수량','가격 USDT (최소 5.01, 소수점 2자리)','전달 방법, 기한, 특별 조건 (선택)',
'거래 생성: {channel}','이 서버의 다른 실제 사용자를 선택하세요.','거래를 사용할 수 없습니다. 관리자에게 문의하세요.','이 주문에서 해당 작업을 할 권한이 없습니다.',
'결제가 만료되었거나 입금이 감지되었습니다. 송금하지 마세요. 이미 지불했다면 관리자에게 문의하세요.',
'관리자에게 알렸습니다. 채널을 유지하고 자동 처리를 중지했습니다. 금액, 거래 해시, 스크린샷을 제공하세요. 재결제하거나 차액을 보내지 마세요.',
'자동 종료를 중지하고 관리자에게 알렸습니다. 유지 사유와 결제했다면 증빙을 제공하세요.',
'상품을 확인하고 실제 수령 후에만 확정하세요. 확정하면 판매자가 대금을 받을 수 있습니다.','확인되었습니다. 곧 채널이 닫힙니다.','이 작업은 만료되었습니다. 최신 주문 메시지를 사용하세요.',
'작업을 완료할 수 없습니다. 관리자에게 확인을 요청하고 결제나 출금을 반복하지 마세요.','소수점 2자리 이내의 유효한 금액을 입력하세요. 최소: {minimum} USDT.',
'각 참여자는 최대 {limit}개의 주문을 진행할 수 있습니다. 기존 주문을 완료하거나 취소하세요.','이 단계는 종료되었습니다. 현재 상태: {status}. 아래 버튼을 사용하세요.',
'원래 결제 금액: {amount} USDT\nUSDT · BSC / BEP20\n주소: {address}\n기한 (UTC): {deadline}\n중복 결제 또는 기한 이후 송금하지 말고 관리자에게 문의하세요.'),
'Bahasa Indonesia':('Detail transaksi','Barang dan jumlah','Harga USDT (min 5.01; 2 desimal)','Pengiriman, tenggat, syarat (opsional)',
'Transaksi dibuat: {channel}','Pilih anggota lain yang bukan bot di server ini.','Transaksi tidak tersedia. Hubungi admin.','Anda tidak berhak melakukan tindakan ini pada pesanan.',
'Pembayaran kedaluwarsa atau sudah terdeteksi. Jangan transfer. Jika sudah bayar, hubungi admin.',
'Admin diberi tahu; kanal disimpan dan proses otomatis dijeda. Kirim jumlah, hash transaksi dan tangkapan layar. Jangan bayar lagi atau tambah selisih.',
'Penutupan otomatis dihentikan dan admin diberi tahu. Jelaskan alasan penyimpanan kanal; jika sudah bayar, kirim bukti.',
'Periksa barang. Konfirmasi hanya setelah menerima; konfirmasi memungkinkan penjual mengambil dana.','Dikonfirmasi. Kanal akan segera ditutup.','Tindakan ini kedaluwarsa. Gunakan pesan pesanan terbaru.',
'Tindakan tidak selesai. Hubungi admin untuk pemeriksaan; jangan ulangi pembayaran atau pencairan.','Masukkan jumlah valid dengan maksimal 2 desimal. Minimum: {minimum} USDT.',
'Setiap peserta dapat memiliki {limit} pesanan aktif. Selesaikan atau batalkan pesanan lama dahulu.','Tahap ini selesai. Status: {status}. Gunakan tombol di bawah.',
'Pembayaran awal: {amount} USDT\nUSDT · BSC / BEP20\nAlamat: {address}\nBatas waktu (UTC): {deadline}\nJangan bayar dua kali atau setelah kedaluwarsa; hubungi admin.'),
'Bahasa Melayu':('Butiran transaksi','Barang dan kuantiti','Harga USDT (min 5.01; 2 perpuluhan)','Penghantaran, tarikh akhir, syarat (pilihan)',
'Transaksi dicipta: {channel}','Pilih ahli lain yang bukan bot dalam pelayan ini.','Transaksi tidak tersedia. Hubungi pentadbir.','Anda tidak dibenarkan melakukan tindakan ini pada pesanan.',
'Bayaran tamat tempoh atau sudah dikesan. Jangan pindahkan wang. Jika sudah bayar, hubungi pentadbir.',
'Pentadbir dimaklumkan; saluran dikekalkan dan proses automatik dijeda. Berikan jumlah, hash transaksi dan tangkap layar. Jangan bayar lagi atau tambah beza.',
'Penutupan automatik dihentikan dan pentadbir dimaklumkan. Jelaskan sebab saluran perlu dikekalkan; jika sudah bayar, berikan bukti.',
'Periksa barang. Sahkan hanya selepas menerima; pengesahan membolehkan penjual menerima bayaran.','Disahkan. Saluran akan ditutup sebentar lagi.','Tindakan ini tamat tempoh. Gunakan mesej pesanan terkini.',
'Tindakan tidak selesai. Hubungi pentadbir untuk semakan; jangan ulangi bayaran atau pengeluaran.','Masukkan jumlah sah dengan maksimum 2 perpuluhan. Minimum: {minimum} USDT.',
'Setiap peserta boleh mempunyai {limit} pesanan aktif. Selesaikan atau batalkan pesanan dahulu.','Langkah ini selesai. Status: {status}. Gunakan butang di bawah.',
'Bayaran asal: {amount} USDT\nUSDT · BSC / BEP20\nAlamat: {address}\nTarikh akhir (UTC): {deadline}\nJangan bayar dua kali atau selepas tamat tempoh; hubungi pentadbir.'),
'Tagalog':('Detalye ng trade','Item at dami','Presyo USDT (min 5.01; 2 decimal)','Paghahatid, deadline, kondisyon (opsyonal)',
'Nagawa ang trade: {channel}','Pumili ng ibang miyembro sa server na hindi bot.','Hindi magagamit ang trade. Kontakin ang admin.','Wala kang pahintulot para sa aksyong ito sa order.',
'Expired na o nakita na ang bayad. Huwag magpadala. Kung bayad na, kontakin ang admin.',
'Naabisuhan ang admin; mananatili ang channel at nakahinto ang awtomatikong proseso. Ibigay ang halaga, transaction hash at screenshot. Huwag magbayad ulit o magdagdag.',
'Itinigil ang awtomatikong pagsasara at naabisuhan ang admin. Ipaliwanag ang dahilan; kung bayad na, ibigay ang katibayan.',
'Suriin ang item. Kumpirmahin lamang matapos matanggap; maaari nang kunin ng seller ang bayad pagkatapos.','Nakumpirma. Magsasara na ang channel.','Expired na ang aksyong ito. Gamitin ang pinakabagong mensahe ng order.',
'Hindi natapos ang aksyon. Kontakin ang admin; huwag ulitin ang bayad o withdrawal.','Maglagay ng wastong halaga na hanggang 2 decimal. Minimum: {minimum} USDT.',
'Hanggang {limit} aktibong order bawat kalahok. Tapusin o kanselahin muna ang mga naunang order.','Tapos na ang hakbang na ito. Status: {status}. Gamitin ang mga button sa ibaba.',
'Orihinal na bayad: {amount} USDT\nUSDT · BSC / BEP20\nAddress: {address}\nDeadline (UTC): {deadline}\nHuwag magbayad ulit o matapos ang deadline; kontakin ang admin.'),
'Português':('Detalhes da negociação','Item e quantidade','Preço USDT (mín. 5.01; 2 decimais)','Entrega, prazo e condições (opcional)',
'Negociação criada: {channel}','Selecione outro membro deste servidor que não seja bot.','Negociações indisponíveis. Contate um administrador.','Você não pode realizar esta ação neste pedido.',
'Pagamento expirado ou já detectado. Não transfira. Se já pagou, contate um administrador.',
'Administrador avisado; canal mantido e processamento automático pausado. Envie valor, hash da transação e captura de tela. Não pague novamente nem complete a diferença.',
'Fechamento automático interrompido; administrador avisado. Explique o motivo; se pagou, envie comprovante.',
'Confira o item. Confirme apenas após recebê-lo: isso permite que o vendedor receba o pagamento.','Confirmado. O canal será fechado em breve.','Esta ação expirou. Use a mensagem mais recente do pedido.',
'Não foi possível concluir. Contate um administrador; não repita pagamentos ou saques.','Insira um valor válido com até 2 casas decimais. Mínimo: {minimum} USDT.',
'Cada participante pode ter {limit} pedidos ativos. Conclua ou cancele os existentes primeiro.','Esta etapa terminou. Status: {status}. Use os botões abaixo.',
'Pagamento original: {amount} USDT\nUSDT · BSC / BEP20\nEndereço: {address}\nPrazo (UTC): {deadline}\nNão pague duas vezes nem após o prazo; contate um administrador.'),
'Español':('Detalles de la operación','Artículo y cantidad','Precio USDT (mín. 5.01; 2 decimales)','Entrega, plazo y condiciones (opcional)',
'Operación creada: {channel}','Selecciona otro miembro de este servidor que no sea un bot.','Operaciones no disponibles. Contacta con un administrador.','No puedes realizar esta acción en este pedido.',
'El pago venció o ya fue detectado. No transfieras. Si ya pagaste, contacta con un administrador.',
'Administrador avisado; canal conservado y procesamiento automático pausado. Envía importe, hash y captura. No vuelvas a pagar ni completes la diferencia.',
'Cierre automático detenido; administrador avisado. Explica el motivo; si pagaste, aporta comprobante.',
'Revisa el artículo. Confirma solo después de recibirlo: la confirmación permite al vendedor cobrar.','Confirmado. El canal se cerrará pronto.','Esta acción venció. Usa el mensaje más reciente del pedido.',
'No se pudo completar. Contacta con un administrador; no repitas pagos ni retiros.','Introduce un importe válido con hasta 2 decimales. Mínimo: {minimum} USDT.',
'Cada participante puede tener {limit} pedidos activos. Completa o cancela los existentes primero.','Este paso terminó. Estado: {status}. Usa los botones de abajo.',
'Pago original: {amount} USDT\nUSDT · BSC / BEP20\nDirección: {address}\nPlazo (UTC): {deadline}\nNo pagues dos veces ni después del plazo; contacta con un administrador.'),
'ภาษาไทย':('รายละเอียดการซื้อขาย','สินค้าและจำนวน','ราคา USDT (ขั้นต่ำ 5.01 ทศนิยม 2 ตำแหน่ง)','วิธีส่ง กำหนดเวลา เงื่อนไข (ไม่บังคับ)',
'สร้างการซื้อขายแล้ว: {channel}','เลือกสมาชิกคนอื่นในเซิร์ฟเวอร์ที่ไม่ใช่บอต','การซื้อขายไม่พร้อมใช้งาน โปรดติดต่อผู้ดูแล','คุณไม่มีสิทธิ์ทำรายการนี้กับคำสั่งซื้อ',
'หมดเวลาชำระหรือพบการชำระแล้ว อย่าโอน หากชำระแล้วให้ติดต่อผู้ดูแล',
'แจ้งผู้ดูแลแล้ว เก็บช่องไว้และหยุดดำเนินการอัตโนมัติ โปรดส่งจำนวนเงิน แฮชธุรกรรมและภาพหน้าจอ อย่าจ่ายซ้ำหรือเติมส่วนต่าง',
'หยุดปิดช่องอัตโนมัติและแจ้งผู้ดูแลแล้ว โปรดบอกเหตุผล หากชำระแล้วให้ส่งหลักฐาน',
'ตรวจสอบสินค้าและยืนยันเมื่อได้รับจริงเท่านั้น การยืนยันอนุญาตให้ผู้ขายรับเงิน','ยืนยันแล้ว ช่องจะปิดในไม่ช้า','รายการนี้หมดอายุ โปรดใช้ข้อความคำสั่งซื้อล่าสุด',
'ดำเนินการไม่สำเร็จ โปรดติดต่อผู้ดูแล อย่าชำระหรือถอนเงินซ้ำ','กรอกจำนวนเงินที่ถูกต้อง ทศนิยมไม่เกิน 2 ตำแหน่ง ขั้นต่ำ {minimum} USDT',
'ผู้เข้าร่วมแต่ละคนมีคำสั่งซื้อที่ดำเนินอยู่ได้ {limit} รายการ โปรดทำให้เสร็จหรือยกเลิกของเดิมก่อน','ขั้นตอนนี้สิ้นสุดแล้ว สถานะ: {status} ใช้ปุ่มด้านล่าง',
'ยอดชำระเดิม: {amount} USDT\nUSDT · BSC / BEP20\nที่อยู่: {address}\nกำหนดเวลา (UTC): {deadline}\nอย่าชำระซ้ำหรือหลังหมดเวลา โปรดติดต่อผู้ดูแล'),
'Tiếng Việt':('Chi tiết giao dịch','Mặt hàng và số lượng','Giá USDT (tối thiểu 5.01; 2 số lẻ)','Cách giao, hạn chót, điều kiện (tùy chọn)',
'Đã tạo giao dịch: {channel}','Chọn thành viên khác trong máy chủ, không phải bot.','Giao dịch chưa khả dụng. Liên hệ quản trị viên.','Bạn không được thực hiện thao tác này với đơn hàng.',
'Thanh toán đã hết hạn hoặc đã được phát hiện. Không chuyển tiền. Nếu đã trả, liên hệ quản trị viên.',
'Đã báo quản trị viên; giữ kênh và tạm dừng xử lý tự động. Cung cấp số tiền, mã giao dịch và ảnh chụp. Không trả lại hoặc tự bù phần thiếu.',
'Đã dừng tự đóng và báo quản trị viên. Nêu lý do cần giữ kênh; nếu đã trả, cung cấp bằng chứng.',
'Kiểm tra hàng. Chỉ xác nhận sau khi thực nhận; xác nhận cho phép người bán nhận tiền.','Đã xác nhận. Kênh sẽ sớm đóng.','Thao tác này đã hết hạn. Dùng tin nhắn đơn hàng mới nhất.',
'Không thể hoàn tất. Liên hệ quản trị viên; không thanh toán hoặc rút tiền lặp lại.','Nhập số tiền hợp lệ, tối đa 2 chữ số thập phân. Tối thiểu: {minimum} USDT.',
'Mỗi người có tối đa {limit} đơn đang hoạt động. Hoàn tất hoặc hủy đơn cũ trước.','Bước này đã kết thúc. Trạng thái: {status}. Dùng nút bên dưới.',
'Số tiền ban đầu: {amount} USDT\nUSDT · BSC / BEP20\nĐịa chỉ: {address}\nHạn (UTC): {deadline}\nKhông trả hai lần hoặc sau hạn; liên hệ quản trị viên.')}

def text(member,key,**values):
    return ROWS[language(member)][KEYS.index(key)].format(**values)

def receipt_button(member):
    return label('receipt',{'status':'shipped','_languages':[language(member)]})

def error(member,exc):
    import logging
    logging.getLogger(__name__).warning('Private trade operation blocked: %s',exc)
    raw=str(exc)
    match=re.search(r'最多同时进行 (\d+) 笔',raw)
    if match: return text(member,'limit',limit=match[1])
    if '金额' in raw and ('低于' in raw or '两位小数' in raw or '最低需' in raw or '金额必须大于' in raw):
        minimum=re.search(r'最低需 ([\d.]+)',raw)
        return text(member,'price',minimum=minimum[1] if minimum else '5.01')
    if raw.startswith(('只有买家','只有卖家','仅发起者')): return text(member,'denied')
    return text(member,'error')
