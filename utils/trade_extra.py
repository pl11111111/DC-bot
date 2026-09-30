"""Translate only known bot-generated supplementary notices, not user content."""
from utils import trade_private_ui
from utils.trade_buttons import label

ROWS={
'English':('An administrator has verified the order and resumed automatic closure. This channel will close in about 5 minutes.',
'Payment has expired. Do not transfer. This channel closes in about 5 minutes. If already paid or a review is needed, click {button}.',
'A late deposit was detected. Wait for administrator review. Deleted channels will not be recreated automatically.',
'A deposit record was found. Automatic closure is stopped and the channel is retained. Wait for administrator review.'),
'中文':('管理员已核实并恢复自动关闭，频道约 5 分钟后关闭。','付款已超时，请勿继续转账。频道约 5 分钟后关闭。如已付款或需核对，请点击「{button}」。','发现迟到账款，请等待管理员核实；已删除的频道不会自动重建。','已找到到账记录，已停止自动关闭并保留频道，请等待管理员核实。'),
'日本語':('管理者が確認し、自動閉鎖を再開しました。チャンネルは約5分後に閉じます。','支払期限が切れました。送金しないでください。約5分後に閉じます。支払済み、または確認が必要なら「{button}」を押してください。','遅れて届いた入金を検出しました。管理者の確認をお待ちください。削除済みチャンネルは自動再作成されません。','入金記録が見つかりました。自動閉鎖を停止しチャンネルを保持しました。管理者の確認をお待ちください。'),
'한국어':('관리자가 확인하고 자동 종료를 재개했습니다. 약 5분 후 채널이 닫힙니다.','결제 시간이 만료되었습니다. 송금하지 마세요. 약 5분 후 닫힙니다. 이미 결제했거나 확인이 필요하면 {button} 버튼을 누르세요.','늦은 입금이 감지되었습니다. 관리자 확인을 기다리세요. 삭제된 채널은 자동 복구되지 않습니다.','입금 기록을 찾았습니다. 자동 종료를 중지하고 채널을 유지했습니다. 관리자 확인을 기다리세요.'),
'Bahasa Indonesia':('Admin telah memeriksa pesanan dan melanjutkan penutupan otomatis. Kanal ditutup sekitar 5 menit lagi.','Pembayaran kedaluwarsa. Jangan transfer. Kanal ditutup sekitar 5 menit lagi. Jika sudah bayar atau perlu pemeriksaan, klik {button}.','Setoran terlambat terdeteksi. Tunggu pemeriksaan admin. Kanal yang dihapus tidak dibuat ulang otomatis.','Catatan setoran ditemukan. Penutupan otomatis dihentikan dan kanal disimpan. Tunggu pemeriksaan admin.'),
'Bahasa Melayu':('Pentadbir telah menyemak dan menyambung penutupan automatik. Saluran ditutup dalam kira-kira 5 minit.','Bayaran tamat tempoh. Jangan pindahkan wang. Saluran ditutup dalam kira-kira 5 minit. Jika sudah bayar atau perlu semakan, klik {button}.','Deposit lewat dikesan. Tunggu semakan pentadbir. Saluran dipadam tidak dicipta semula secara automatik.','Rekod deposit ditemui. Penutupan automatik dihentikan dan saluran dikekalkan. Tunggu semakan pentadbir.'),
'Tagalog':('Nasuri na ng admin at ipinagpatuloy ang awtomatikong pagsasara. Magsasara ang channel sa humigit-kumulang 5 minuto.','Expired na ang bayad. Huwag magpadala. Magsasara sa humigit-kumulang 5 minuto. Kung bayad na o kailangang suriin, pindutin ang {button}.','May nakitang nahuling deposito. Hintayin ang pagsusuri ng admin. Hindi awtomatikong ibabalik ang naburang channel.','Nakita ang deposito. Itinigil ang awtomatikong pagsasara at pinanatili ang channel. Hintayin ang admin.'),
'Português':('O administrador verificou e retomou o fechamento automático. O canal será fechado em cerca de 5 minutos.','O pagamento expirou. Não transfira. O canal fecha em cerca de 5 minutos. Se já pagou ou precisa de revisão, clique em {button}.','Foi detectado um depósito tardio. Aguarde a revisão do administrador. Canais excluídos não serão recriados automaticamente.','Registro de depósito encontrado. Fechamento automático interrompido e canal mantido. Aguarde a revisão do administrador.'),
'Español':('El administrador verificó y reanudó el cierre automático. El canal se cerrará en unos 5 minutos.','El pago venció. No transfieras. El canal se cierra en unos 5 minutos. Si ya pagaste o necesitas revisión, pulsa {button}.','Se detectó un depósito tardío. Espera la revisión del administrador. Los canales eliminados no se recrearán automáticamente.','Se encontró el registro del depósito. Se detuvo el cierre automático y se conserva el canal. Espera la revisión del administrador.'),
'ภาษาไทย':('ผู้ดูแลตรวจสอบและเปิดการปิดอัตโนมัติอีกครั้งแล้ว ช่องจะปิดในประมาณ 5 นาที','หมดเวลาชำระแล้ว อย่าโอน ช่องจะปิดในประมาณ 5 นาที หากชำระแล้วหรือต้องการตรวจสอบ ให้กด {button}','พบเงินเข้าล่าช้า โปรดรอผู้ดูแลตรวจสอบ ช่องที่ลบแล้วจะไม่ถูกสร้างใหม่อัตโนมัติ','พบรายการเงินเข้าแล้ว หยุดปิดอัตโนมัติและเก็บช่องไว้ โปรดรอผู้ดูแลตรวจสอบ'),
'Tiếng Việt':('Quản trị viên đã kiểm tra và khôi phục tự đóng. Kênh sẽ đóng sau khoảng 5 phút.','Đã hết hạn thanh toán. Không chuyển tiền. Kênh đóng sau khoảng 5 phút. Nếu đã trả hoặc cần kiểm tra, nhấn {button}.','Phát hiện tiền đến muộn. Chờ quản trị viên kiểm tra. Kênh đã xóa không được tự tạo lại.','Đã tìm thấy bản ghi tiền vào. Đã dừng tự đóng và giữ kênh. Chờ quản trị viên kiểm tra.')}

SOURCES={
'管理员已核实并恢复自动关闭，频道约 5 分钟后关闭。':0,
'付款已超时，请勿继续转账。频道将在约 5 分钟后关闭。\n如已付款或需要核对，请点击下方「已付款／取消关闭，请管理员核实」按钮。':1,
'发现迟到账款，请等待管理员核实；已删除的频道不会自动重建。':2,
'已找到到账记录，频道已保留，请等待管理员核实。':3,
'已找到到账记录，自动关闭已取消，请等待管理员核实。':3,
'已呼叫管理员，频道已保留。请提供实际转账金额、交易哈希和付款截图，等待核实；请勿自行补差额或重复支付。':'help',
'已取消自动关闭，频道已保留，等待管理员核实。':'hold'}

def render(source,languages):
    if source not in SOURCES: return source
    kind=SOURCES[source]
    sections=[]
    for lang in dict.fromkeys(languages):
        lang=lang if lang in ROWS else 'English'
        if isinstance(kind,int):
            content=ROWS[lang][kind].format(button=label('keep',{'status':'payment_timeout','_languages':[lang]}))
        else:
            content=trade_private_ui.ROWS[lang][trade_private_ui.KEYS.index(kind)]
        sections.append(content)
    return '\n\n'.join(sections)
