"""Compact localized payment instructions; amounts and network are never translated."""
from decimal import Decimal
from utils.payout_language import language
from utils.trade_buttons import label
from utils.trade_payment_ui import deadline,payment_instructions

COPY={
'English':('Exact receipt: {amount} USDT','Keep all 6 decimals. Only the buyer pays. Copy the order address, select USDT on BSC / BEP20 and send once.',
'Exchange: the final recipient amount must equal {amount} USDT. Binance internal transfers may be free. Check the actual fee; do not add more if the recipient amount is correct. Do not use UID, email or Binance Pay.',
'BSC wallet: send {amount} USDT; network fees are normally paid separately in BNB. The QR code contains only the address. Check the amount and network yourself.',
'Do not pay after the deadline. If paid but unrecognized or the amount is wrong, contact admin with the amount, transaction hash and screenshot. Do not pay again or top up. Wrong-amount refunds require administrator verification and manual processing; network fees are deducted and explained.',
'Never share passwords, recovery phrases, private keys or verification codes. Administrators will not send links or request downloads, software installation or remote access for payment checks, refunds or unfreezing funds. Stop and verify suspicious requests in the trading channel.'),
'日本語':('必要な着金額：{amount} USDT','小数6桁をすべて保持してください。支払うのは買い手のみです。注文のアドレスをコピーし、USDT・BSC / BEP20 を選び、1回だけ送金します。',
'取引所：最終的な受取額を {amount} USDT にしてください。Binance の内部送金は無料の場合があります。実際の手数料を確認し、受取額が正しければ追加しないでください。UID・メール・Binance Pay は使用しないでください。',
'BSC ウォレット：{amount} USDT を送ります。ネットワーク手数料は通常 BNB で別途支払います。QR コードはアドレスのみです。金額とネットワークを確認してください。',
'期限後は送金しないでください。支払済みで未検出、または金額を間違えた場合は、金額・取引ハッシュ・画像を管理者に提示してください。再送金や差額追加は禁止です。金額間違いの返金は管理者の確認後に手動で行い、ネットワーク手数料を差し引いて説明します。',
'パスワード・復元フレーズ・秘密鍵・認証コードを渡さないでください。管理者は支払確認・返金・凍結解除を理由にリンク送付、ダウンロード、ソフト導入、遠隔操作を要求しません。不審な要求は中止し取引チャンネルで確認してください。'),
'한국어':('실제 입금액: {amount} USDT','소수점 6자리를 모두 유지하세요. 구매자만 결제합니다. 주문 주소를 복사하고 USDT · BSC / BEP20을 선택해 한 번만 보내세요.',
'거래소: 최종 수령액은 {amount} USDT여야 합니다. Binance 내부 전송은 무료일 수 있습니다. 실제 수수료를 확인하고 수령액이 맞으면 더하지 마세요. UID, 이메일, Binance Pay를 사용하지 마세요.',
'BSC 지갑: {amount} USDT를 보냅니다. 네트워크 수수료는 보통 BNB로 별도 지불합니다. QR에는 주소만 있습니다. 금액과 네트워크를 확인하세요.',
'기한 후 송금하지 마세요. 이미 지불했는데 인식되지 않거나 금액이 틀리면 관리자에게 금액, 해시, 스크린샷을 보내세요. 다시 결제하거나 차액을 보내지 마세요. 잘못된 금액은 관리자 확인 후 수동 환불하며 네트워크 수수료를 차감하고 안내합니다.',
'비밀번호, 복구 구문, 개인 키, 인증 코드를 공유하지 마세요. 관리자는 결제 확인, 환불, 동결 해제를 이유로 링크, 다운로드, 프로그램 설치, 원격 제어를 요구하지 않습니다. 의심되면 중단하고 거래 채널에서 확인하세요.'),
'Bahasa Indonesia':('Jumlah yang harus diterima: {amount} USDT','Pertahankan semua 6 desimal. Hanya pembeli membayar. Salin alamat pesanan, pilih USDT · BSC / BEP20 dan kirim sekali.',
'Bursa: jumlah akhir penerima harus {amount} USDT. Transfer internal Binance mungkin gratis. Periksa biaya aktual; jangan tambah jika jumlah penerima benar. Jangan gunakan UID, email atau Binance Pay.',
'Dompet BSC: kirim {amount} USDT. Biaya jaringan biasanya dibayar terpisah dengan BNB. QR hanya berisi alamat; periksa jumlah dan jaringan.',
'Jangan bayar setelah tenggat. Jika sudah bayar tetapi belum dikenali atau salah jumlah, hubungi admin dengan jumlah, hash dan tangkapan layar. Jangan bayar lagi atau tambah selisih. Pengembalian salah jumlah diproses manual setelah pemeriksaan; biaya jaringan dipotong dan dijelaskan.',
'Jangan bagikan kata sandi, frasa pemulihan, kunci privat atau kode verifikasi. Admin tidak mengirim tautan atau meminta unduhan, instalasi atau akses jarak jauh untuk pemeriksaan, pengembalian atau membuka pembekuan. Hentikan dan verifikasi di kanal transaksi.'),
'Bahasa Melayu':('Jumlah yang mesti diterima: {amount} USDT','Kekalkan semua 6 tempat perpuluhan. Hanya pembeli membayar. Salin alamat pesanan, pilih USDT · BSC / BEP20 dan hantar sekali.',
'Bursa: jumlah akhir penerima mesti {amount} USDT. Pindahan dalaman Binance mungkin percuma. Semak yuran sebenar; jangan tambah jika jumlah penerima betul. Jangan guna UID, e-mel atau Binance Pay.',
'Dompet BSC: hantar {amount} USDT. Yuran rangkaian biasanya dibayar berasingan dengan BNB. QR hanya mengandungi alamat; semak jumlah dan rangkaian.',
'Jangan bayar selepas tarikh akhir. Jika sudah bayar tetapi tidak dikesan atau jumlah salah, hubungi pentadbir dengan jumlah, hash dan tangkap layar. Jangan bayar lagi atau tambah beza. Bayaran balik jumlah salah diproses manual selepas semakan; yuran rangkaian ditolak dan dijelaskan.',
'Jangan kongsi kata laluan, frasa pemulihan, kunci peribadi atau kod pengesahan. Pentadbir tidak menghantar pautan atau meminta muat turun, pemasangan atau akses jauh untuk semakan, bayaran balik atau nyahbeku dana. Hentikan dan sahkan dalam saluran transaksi.'),
'Tagalog':('Eksaktong matatanggap: {amount} USDT','Panatilihin ang lahat ng 6 decimal. Buyer lamang ang magbabayad. Kopyahin ang address, piliin ang USDT · BSC / BEP20 at magpadala nang isang beses.',
'Exchange: dapat {amount} USDT ang matatanggap. Maaaring libre ang internal transfer sa Binance. Suriin ang aktuwal na bayarin; huwag dagdagan kung tama na ang matatanggap. Huwag gumamit ng UID, email o Binance Pay.',
'BSC wallet: magpadala ng {amount} USDT. Karaniwang hiwalay na BNB ang network fee. Address lamang ang nasa QR; suriin ang halaga at network.',
'Huwag magbayad matapos ang deadline. Kung bayad na pero hindi nakita o mali ang halaga, ibigay sa admin ang halaga, hash at screenshot. Huwag magbayad ulit o magdagdag. Mano-mano ang refund ng maling halaga pagkatapos masuri; ibabawas at ipapaliwanag ang network fee.',
'Huwag ibigay ang password, recovery phrase, private key o verification code. Hindi magpapadala ang admin ng link o hihiling ng download, installation o remote access para sa pagsusuri, refund o pag-unfreeze. Huminto at tiyakin sa trading channel.'),
'Português':('Valor exato a receber: {amount} USDT','Mantenha todas as 6 casas decimais. Apenas o comprador paga. Copie o endereço, selecione USDT · BSC / BEP20 e envie uma vez.',
'Corretora: o valor final recebido deve ser {amount} USDT. Transferências internas da Binance podem ser gratuitas. Confira a taxa real; não acrescente se o valor recebido estiver correto. Não use UID, e-mail ou Binance Pay.',
'Carteira BSC: envie {amount} USDT. A taxa de rede normalmente é paga separadamente em BNB. O QR contém apenas o endereço; confira valor e rede.',
'Não pague após o prazo. Se pagou mas não foi reconhecido ou o valor está errado, envie valor, hash e captura ao administrador. Não pague novamente nem complete a diferença. Reembolsos de valor errado são manuais após verificação; a taxa de rede é descontada e explicada.',
'Nunca compartilhe senhas, frases de recuperação, chaves privadas ou códigos. Administradores não enviam links nem pedem downloads, instalações ou acesso remoto para verificar, reembolsar ou descongelar fundos. Pare e verifique no canal da negociação.'),
'Español':('Importe exacto a recibir: {amount} USDT','Conserva los 6 decimales. Solo paga el comprador. Copia la dirección, selecciona USDT · BSC / BEP20 y envía una vez.',
'Exchange: el importe final recibido debe ser {amount} USDT. Las transferencias internas de Binance pueden ser gratuitas. Revisa la comisión real; no añadas si el importe recibido es correcto. No uses UID, correo o Binance Pay.',
'Billetera BSC: envía {amount} USDT. La comisión de red suele pagarse por separado en BNB. El QR solo contiene la dirección; revisa importe y red.',
'No pagues después del plazo. Si pagaste pero no se reconoce o el importe es incorrecto, envía importe, hash y captura al administrador. No repitas el pago ni completes la diferencia. Los reembolsos por importe incorrecto son manuales tras verificar; se descuenta y explica la comisión de red.',
'No compartas contraseñas, frases de recuperación, claves privadas ni códigos. Los administradores no envían enlaces ni piden descargas, instalaciones o acceso remoto para verificar, reembolsar o desbloquear fondos. Detente y verifica en el canal de la operación.'),
'ภาษาไทย':('ยอดที่ต้องได้รับจริง: {amount} USDT','คงทศนิยมครบ 6 ตำแหน่ง เฉพาะผู้ซื้อเป็นผู้จ่าย คัดลอกที่อยู่ เลือก USDT · BSC / BEP20 และส่งครั้งเดียว',
'กระดานซื้อขาย: ยอดรับสุดท้ายต้องเท่ากับ {amount} USDT การโอนภายใน Binance อาจฟรี ตรวจค่าธรรมเนียมจริง อย่าเพิ่มหากยอดรับถูกแล้ว ห้ามใช้ UID อีเมล หรือ Binance Pay',
'กระเป๋า BSC: ส่ง {amount} USDT ค่าธรรมเนียมเครือข่ายปกติจ่ายแยกด้วย BNB QR มีแค่ที่อยู่ โปรดตรวจยอดและเครือข่าย',
'อย่าจ่ายหลังหมดเวลา หากจ่ายแล้วไม่พบหรือยอดผิด ให้ส่งยอด แฮช และภาพหน้าจอแก่ผู้ดูแล อย่าจ่ายซ้ำหรือเติมส่วนต่าง การคืนยอดผิดต้องตรวจสอบและคืนด้วยตนเอง โดยหักและแจ้งค่าธรรมเนียมเครือข่าย',
'ห้ามให้รหัสผ่าน วลีสำรอง กุญแจส่วนตัว หรือรหัสยืนยัน ผู้ดูแลจะไม่ส่งลิงก์หรือขอให้ดาวน์โหลด ติดตั้ง หรือเปิดควบคุมระยะไกลเพื่อเช็ก คืน หรือปลดระงับเงิน หากพบให้หยุดและตรวจสอบในช่องซื้อขาย'),
'Tiếng Việt':('Số tiền thực nhận: {amount} USDT','Giữ đủ 6 chữ số thập phân. Chỉ người mua trả tiền. Sao chép địa chỉ, chọn USDT · BSC / BEP20 và gửi một lần.',
'Sàn: số tiền nhận cuối cùng phải là {amount} USDT. Chuyển nội bộ Binance có thể miễn phí. Kiểm tra phí thực tế; không cộng thêm nếu số nhận đã đúng. Không dùng UID, email hoặc Binance Pay.',
'Ví BSC: gửi {amount} USDT. Phí mạng thường trả riêng bằng BNB. QR chỉ có địa chỉ; kiểm tra số tiền và mạng.',
'Không trả sau hạn. Nếu đã trả nhưng chưa nhận diện hoặc sai số tiền, gửi số tiền, hash và ảnh cho quản trị viên. Không trả lại hoặc tự bù. Hoàn tiền sai số cần kiểm tra và xử lý thủ công; phí mạng được trừ và thông báo.',
'Không chia sẻ mật khẩu, cụm từ khôi phục, khóa riêng hoặc mã xác minh. Quản trị viên không gửi liên kết hay yêu cầu tải tệp, cài phần mềm hoặc truy cập từ xa để kiểm tra, hoàn hay mở khóa tiền. Dừng lại và xác minh trong kênh giao dịch.')}

def render(member,invoice,guide_url=None):
    lang=language(member)
    if lang=='中文': return payment_instructions(invoice,guide_url=guide_url)
    parts=COPY[lang]
    amount=f"{Decimal(str(invoice['amount'])):.6f}"
    icons=('💰','📖','🏦','👛','🆘','🛡️')
    body='\n\n'.join(f'{icon} {part.format(amount=amount)}' for icon,part in zip(icons,parts))
    body+=f'\n\n⏳ <t:{deadline(invoice)}:f> (<t:{deadline(invoice)}:R>)'
    if guide_url:
        title=label('payment_info',{'status':'paying','_languages':[lang]})
        body+=f'\n\n📖 [{title}]({guide_url})'
    body+='\n\n🏦 [Binance](https://www.bsmkweb.cc/register?ref=1024490102) · `1024490102`'
    return body
