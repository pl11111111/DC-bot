"""Private payout UI follows the acting member's configured language role."""
import config

KEYS=('title','refund_title','address','confirm','summary','refund_note','seller_note','invalid','quote','review','changed','stale')
ROWS={
'English':('Claim funds','Claim refund','Your USDT-BEP20 address','Confirm address and request payout',
'Address: {address}\nGross: {gross} USDT\nNetwork fee: {fee} USDT\nEstimated receipt: {net} USDT\nRounding difference: {rounding} USDT\nInternal-transfer fees depend on the actual transfer route.',
'Refund includes the paid escrow fee. Network fees are deducted from the refund.',
'Network fees are deducted from the seller’s proceeds.',
'Enter a valid USDT-BEP20 address.','Unable to obtain a payout quote. Contact an administrator if this continues.',
'The payout result needs verification. Contact an administrator; do not submit again.',
'Fees have changed. Open Claim funds again to review the new quote.',
'This confirmation is no longer valid. Check the latest order message; do not submit again.'),
'中文':('领取货款','领取退款','本人 USDT-BEP20 地址','确认地址与费用，申请放款',
'地址：{address}\n总额：{gross} USDT\n网络费：{fee} USDT\n预计到账：{net} USDT\n精度舍入差额：{rounding} USDT\n内部转账费用以实际转账渠道为准。',
'退款包含已付担保费，网络费从退款中扣除。','网络费从卖家货款中扣除。',
'请输入有效的 USDT-BEP20 地址。','暂时无法获取提现报价，持续失败请联系管理员。','放款结果需要核对，请联系管理员，勿重复提交。',
'费用已变化，请重新点击领取货款查看报价。','此确认已失效，请查看最新订单消息，勿重复提交。'),
'日本語':('代金を受け取る','返金を受け取る','本人の USDT-BEP20 アドレス','アドレスと手数料を確認して申請',
'アドレス：{address}\n総額：{gross} USDT\nネットワーク手数料：{fee} USDT\n受取予定額：{net} USDT\n丸め差額：{rounding} USDT\n内部送金の手数料は実際の送金経路によります。',
'返金には支払済みのエスクロー手数料を含みます。ネットワーク手数料は返金額から差し引かれます。','ネットワーク手数料は売り手の代金から差し引かれます。',
'有効な USDT-BEP20 アドレスを入力してください。','出金見積もりを取得できません。問題が続く場合は管理者に連絡してください。','送金結果の確認が必要です。管理者に連絡し、再申請しないでください。',
'手数料が変更されました。受取ボタンから見積もりを確認し直してください。','この確認は無効です。最新の注文メッセージを確認し、再申請しないでください。'),
'한국어':('대금 수령','환불 수령','본인 USDT-BEP20 주소','주소와 수수료 확인 후 신청',
'주소: {address}\n총액: {gross} USDT\n네트워크 수수료: {fee} USDT\n예상 수령액: {net} USDT\n반올림 차액: {rounding} USDT\n내부 전송 수수료는 실제 전송 경로에 따라 다릅니다.',
'환불에는 지불한 에스크로 수수료가 포함되며 네트워크 수수료는 환불액에서 차감됩니다.','네트워크 수수료는 판매 대금에서 차감됩니다.',
'유효한 USDT-BEP20 주소를 입력하세요.','출금 견적을 가져올 수 없습니다. 계속 실패하면 관리자에게 문의하세요.','송금 결과 확인이 필요합니다. 관리자에게 문의하고 다시 신청하지 마세요.',
'수수료가 변경되었습니다. 수령 버튼을 다시 열어 견적을 확인하세요.','이 확인은 유효하지 않습니다. 최신 주문 메시지를 확인하고 다시 신청하지 마세요.'),
'Bahasa Indonesia':('Terima dana','Terima pengembalian','Alamat USDT-BEP20 milik Anda','Konfirmasi alamat dan ajukan pencairan',
'Alamat: {address}\nTotal: {gross} USDT\nBiaya jaringan: {fee} USDT\nPerkiraan diterima: {net} USDT\nSelisih pembulatan: {rounding} USDT\nBiaya transfer internal bergantung pada jalur transfer.',
'Pengembalian mencakup biaya escrow yang dibayar. Biaya jaringan dipotong dari pengembalian.','Biaya jaringan dipotong dari hasil penjualan.',
'Masukkan alamat USDT-BEP20 yang valid.','Penawaran pencairan tidak tersedia. Hubungi admin jika masalah berlanjut.','Hasil pencairan perlu diperiksa. Hubungi admin; jangan ajukan lagi.',
'Biaya berubah. Buka kembali penerimaan dana untuk melihat penawaran baru.','Konfirmasi ini tidak berlaku. Periksa pesan pesanan terbaru; jangan ajukan lagi.'),
'Bahasa Melayu':('Terima bayaran','Terima bayaran balik','Alamat USDT-BEP20 anda','Sahkan alamat dan mohon bayaran',
'Alamat: {address}\nJumlah: {gross} USDT\nYuran rangkaian: {fee} USDT\nAnggaran diterima: {net} USDT\nPerbezaan pembundaran: {rounding} USDT\nYuran pindahan dalaman bergantung pada laluan pindahan.',
'Bayaran balik termasuk yuran escrow yang dibayar. Yuran rangkaian ditolak daripada bayaran balik.','Yuran rangkaian ditolak daripada hasil jualan.',
'Masukkan alamat USDT-BEP20 yang sah.','Anggaran pengeluaran tidak tersedia. Hubungi pentadbir jika masalah berterusan.','Keputusan bayaran perlu disemak. Hubungi pentadbir; jangan hantar semula.',
'Yuran telah berubah. Buka semula tuntutan bayaran untuk melihat anggaran baharu.','Pengesahan ini tidak lagi sah. Semak mesej pesanan terkini; jangan hantar semula.'),
'Tagalog':('Kunin ang bayad','Kunin ang refund','Sariling USDT-BEP20 address','Kumpirmahin at humiling ng bayad',
'Address: {address}\nKabuuan: {gross} USDT\nBayad sa network: {fee} USDT\nInaasahang matatanggap: {net} USDT\nPagkakaiba sa pag-round: {rounding} USDT\nNakadepende sa ruta ang bayad sa internal transfer.',
'Kasama sa refund ang binayarang escrow fee. Ibabawas sa refund ang bayad sa network.','Ibabawas sa bayad ng nagbebenta ang bayad sa network.',
'Maglagay ng wastong USDT-BEP20 address.','Hindi makuha ang tantiya sa payout. Kontakin ang admin kung magpatuloy ang problema.','Kailangang suriin ang resulta ng payout. Kontakin ang admin; huwag magsumite ulit.',
'Nagbago ang bayarin. Buksan muli ang pagkuha ng bayad para makita ang bagong tantiya.','Hindi na balido ang kumpirmasyong ito. Tingnan ang pinakabagong mensahe ng order; huwag magsumite ulit.'),
'Português':('Receber pagamento','Receber reembolso','Seu endereço USDT-BEP20','Confirmar endereço e solicitar pagamento',
'Endereço: {address}\nTotal: {gross} USDT\nTaxa de rede: {fee} USDT\nRecebimento estimado: {net} USDT\nDiferença de arredondamento: {rounding} USDT\nAs taxas internas dependem da rota de transferência.',
'O reembolso inclui a taxa de custódia paga. A taxa de rede é descontada do reembolso.','A taxa de rede é descontada do valor do vendedor.',
'Insira um endereço USDT-BEP20 válido.','Não foi possível obter a estimativa. Contate um administrador se o problema persistir.','O resultado do pagamento precisa ser verificado. Contate um administrador; não envie novamente.',
'As taxas mudaram. Abra novamente o recebimento para conferir a nova estimativa.','Esta confirmação não é mais válida. Confira a mensagem mais recente do pedido; não envie novamente.'),
'Español':('Cobrar fondos','Recibir reembolso','Tu dirección USDT-BEP20','Confirmar dirección y solicitar pago',
'Dirección: {address}\nTotal: {gross} USDT\nComisión de red: {fee} USDT\nRecepción estimada: {net} USDT\nDiferencia de redondeo: {rounding} USDT\nLas comisiones internas dependen de la ruta de transferencia.',
'El reembolso incluye la comisión de custodia pagada. La comisión de red se descuenta del reembolso.','La comisión de red se descuenta del importe del vendedor.',
'Introduce una dirección USDT-BEP20 válida.','No se puede obtener la estimación. Contacta con un administrador si el problema persiste.','Es necesario verificar el resultado del pago. Contacta con un administrador; no vuelvas a enviarlo.',
'Las comisiones cambiaron. Abre de nuevo el cobro para revisar la nueva estimación.','Esta confirmación ya no es válida. Consulta el último mensaje del pedido; no vuelvas a enviarlo.'),
'ภาษาไทย':('รับเงินค่าสินค้า','รับเงินคืน','ที่อยู่ USDT-BEP20 ของคุณ','ยืนยันที่อยู่และขอรับเงิน',
'ที่อยู่: {address}\nยอดรวม: {gross} USDT\nค่าธรรมเนียมเครือข่าย: {fee} USDT\nยอดรับโดยประมาณ: {net} USDT\nส่วนต่างการปัดเศษ: {rounding} USDT\nค่าธรรมเนียมโอนภายในขึ้นอยู่กับช่องทางโอนจริง',
'เงินคืนรวมค่าธรรมเนียมเอสโครว์ที่ชำระแล้ว โดยหักค่าธรรมเนียมเครือข่ายจากเงินคืน','ค่าธรรมเนียมเครือข่ายหักจากเงินค่าสินค้าของผู้ขาย',
'กรุณากรอกที่อยู่ USDT-BEP20 ที่ถูกต้อง','ไม่สามารถคำนวณยอดถอน หากยังมีปัญหาโปรดติดต่อผู้ดูแล','ต้องตรวจสอบผลการโอน โปรดติดต่อผู้ดูแลและอย่าส่งคำขอซ้ำ',
'ค่าธรรมเนียมเปลี่ยนแล้ว โปรดเปิดรับเงินอีกครั้งเพื่อตรวจสอบยอดใหม่','การยืนยันนี้ใช้ไม่ได้แล้ว โปรดดูข้อความคำสั่งซื้อล่าสุดและอย่าส่งคำขอซ้ำ'),
'Tiếng Việt':('Nhận tiền bán hàng','Nhận tiền hoàn','Địa chỉ USDT-BEP20 của bạn','Xác nhận địa chỉ và yêu cầu thanh toán',
'Địa chỉ: {address}\nTổng: {gross} USDT\nPhí mạng: {fee} USDT\nDự kiến nhận: {net} USDT\nChênh lệch làm tròn: {rounding} USDT\nPhí chuyển nội bộ tùy thuộc tuyến chuyển thực tế.',
'Tiền hoàn gồm phí ký quỹ đã trả. Phí mạng được trừ từ tiền hoàn.','Phí mạng được trừ từ tiền bán hàng của người bán.',
'Nhập địa chỉ USDT-BEP20 hợp lệ.','Không thể lấy báo giá rút tiền. Liên hệ quản trị viên nếu lỗi tiếp diễn.','Kết quả thanh toán cần được kiểm tra. Liên hệ quản trị viên; không gửi lại yêu cầu.',
'Phí đã thay đổi. Mở lại mục nhận tiền để xem báo giá mới.','Xác nhận này không còn hiệu lực. Xem tin nhắn đơn hàng mới nhất; không gửi lại yêu cầu.')}

PAYEE_ONLY={
    'English':('Only the seller can claim these funds. As the buyer, no action is needed; please wait for the seller to claim them.','Only the buyer can claim this refund. As the seller, no action is needed; please wait for the buyer to claim it.'),
    '中文':('货款由卖家领取。你是买家，无需操作，请等待卖家领取货款。','退款由买家领取。你是卖家，无需操作，请等待买家领取退款。'),
    '日本語':('代金を受け取れるのは売り手のみです。買い手の操作は不要です。売り手が代金を受け取るまでお待ちください。','返金を受け取れるのは買い手のみです。売り手の操作は不要です。買い手が返金を受け取るまでお待ちください。'),
    '한국어':('판매 대금은 판매자만 수령할 수 있습니다. 구매자는 별도로 조작할 필요가 없습니다. 판매자가 대금을 수령할 때까지 기다려 주세요.','환불금은 구매자만 수령할 수 있습니다. 판매자는 별도로 조작할 필요가 없습니다. 구매자가 환불금을 수령할 때까지 기다려 주세요.'),
    'Bahasa Indonesia':('Hanya penjual yang dapat menerima hasil penjualan ini. Sebagai pembeli, Anda tidak perlu melakukan apa pun; tunggu penjual mengambil dananya.','Hanya pembeli yang dapat menerima pengembalian dana ini. Sebagai penjual, Anda tidak perlu melakukan apa pun; tunggu pembeli mengambil pengembaliannya.'),
    'Bahasa Melayu':('Hanya penjual boleh menerima bayaran ini. Sebagai pembeli, anda tidak perlu berbuat apa-apa; tunggu penjual menerima bayaran.','Hanya pembeli boleh menerima bayaran balik ini. Sebagai penjual, anda tidak perlu berbuat apa-apa; tunggu pembeli menerima bayaran balik.'),
    'Tagalog':('Nagbebenta lamang ang maaaring kumuha ng bayad na ito. Bilang mamimili, wala ka nang kailangang gawin; hintaying kunin ng nagbebenta ang bayad.','Mamimili lamang ang maaaring kumuha ng refund na ito. Bilang nagbebenta, wala ka nang kailangang gawin; hintaying kunin ng mamimili ang refund.'),
    'Português':('Somente o vendedor pode receber este pagamento. Como comprador, você não precisa fazer nada; aguarde o vendedor solicitar o recebimento.','Somente o comprador pode receber este reembolso. Como vendedor, você não precisa fazer nada; aguarde o comprador solicitar o reembolso.'),
    'Español':('Solo el vendedor puede cobrar este pago. Como comprador, no necesitas hacer nada; espera a que el vendedor lo cobre.','Solo el comprador puede recibir este reembolso. Como vendedor, no necesitas hacer nada; espera a que el comprador lo reciba.'),
    'ภาษาไทย':('เฉพาะผู้ขายเท่านั้นที่รับเงินค่าสินค้านี้ได้ คุณเป็นผู้ซื้อจึงไม่ต้องดำเนินการใด ๆ โปรดรอผู้ขายรับเงินค่าสินค้า','เฉพาะผู้ซื้อเท่านั้นที่รับเงินคืนนี้ได้ คุณเป็นผู้ขายจึงไม่ต้องดำเนินการใด ๆ โปรดรอผู้ซื้อรับเงินคืน'),
    'Tiếng Việt':('Chỉ người bán mới có thể nhận khoản tiền bán hàng này. Bạn là người mua nên không cần thao tác; vui lòng chờ người bán nhận tiền.','Chỉ người mua mới có thể nhận khoản hoàn tiền này. Bạn là người bán nên không cần thao tác; vui lòng chờ người mua nhận tiền hoàn.')
}

def payee_only(member,refund=False):
    return PAYEE_ONLY[language(member)][1 if refund else 0]

def language(member):
    roles={r.id for r in getattr(member,'roles',[])}
    return next((name for name,rid in config.NEW.LANGUAGES.items() if rid and rid in roles and name in ROWS),'English')

def texts(member): return dict(zip(KEYS,ROWS[language(member)]))

def error(member,exc,quote=False):
    ui=texts(member)
    if str(exc)=='请输入有效的 USDT-BEP20 地址': return ui['invalid']
    if str(exc)=='费用已变化，请重新领取查看报价': return ui['changed']
    return ui['quote' if quote else 'review']
