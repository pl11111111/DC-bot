"""Verification interface translations; rule articles are configured separately."""
KEYS=('choose','intro','next','back','agree','done','fallback','read','only')
ROWS={
'English':('Choose your language','Select your language, then continue to read the rules. Verification is completed only after you agree.','Next','Back','I Have Read & Agree to the Rules','Verification complete','Translation unavailable; showing the original text.','Read the rules, then agree.','Translation only; no roles are granted.'),
'中文':('选择语言','请选择语言，然后阅读规则。点击同意后才会完成验证。','下一步','返回','我已阅读并同意规则','验证完成','暂无最新译文，以下显示原文。','请阅读规则，然后点击同意。','仅查看译文，不发放身份组。'),
'日本語':('言語を選択','言語を選んでからルールをお読みください。同意すると認証が完了します。','次へ','戻る','ルールを読み、同意します','認証完了','翻訳がないため、原文を表示しています。','ルールを読んで同意してください。','翻訳の閲覧のみ。ロールは付与されません。'),
'한국어':('언어 선택','언어를 선택한 후 규칙을 읽어 주세요. 동의해야 인증이 완료됩니다.','다음','뒤로','규칙을 읽었으며 동의합니다','인증 완료','번역이 없어 원문을 표시합니다.','규칙을 읽고 동의해 주세요.','번역만 표시하며 역할을 부여하지 않습니다.'),
'Bahasa Indonesia':('Pilih bahasa','Pilih bahasa, lalu baca peraturan. Verifikasi selesai setelah Anda menyetujuinya.','Lanjut','Kembali','Saya telah membaca dan menyetujui peraturan','Verifikasi selesai','Terjemahan belum tersedia; menampilkan teks asli.','Baca peraturan, lalu setujui.','Hanya terjemahan; tidak memberikan peran.'),
'Tagalog':('Piliin ang wika','Piliin ang wika, pagkatapos ay basahin ang mga patakaran. Matatapos ang beripikasyon kapag sumang-ayon ka.','Susunod','Bumalik','Nabasa ko at sumasang-ayon ako sa mga patakaran','Tapos na ang beripikasyon','Walang salin; ipinapakita ang orihinal na teksto.','Basahin ang mga patakaran at sumang-ayon.','Salin lamang; walang ibibigay na role.'),
'Bahasa Melayu':('Pilih bahasa','Pilih bahasa, kemudian baca peraturan. Pengesahan selesai selepas anda bersetuju.','Seterusnya','Kembali','Saya telah membaca dan bersetuju dengan peraturan','Pengesahan selesai','Terjemahan tiada; teks asal dipaparkan.','Baca peraturan, kemudian bersetuju.','Terjemahan sahaja; tiada peranan diberikan.'),
'Português':('Escolha seu idioma','Escolha o idioma e leia as regras. A verificação só será concluída após sua concordância.','Avançar','Voltar','Li e concordo com as regras','Verificação concluída','Tradução indisponível; exibindo o texto original.','Leia as regras e concorde.','Somente tradução; nenhum cargo será atribuído.'),
'Español':('Elige tu idioma','Elige el idioma y lee las reglas. La verificación se completará cuando las aceptes.','Siguiente','Volver','He leído y acepto las reglas','Verificación completada','Traducción no disponible; se muestra el texto original.','Lee las reglas y acepta.','Solo traducción; no se asignan roles.'),
'ภาษาไทย':('เลือกภาษา','เลือกภาษาแล้วอ่านกฎ การยืนยันจะเสร็จสมบูรณ์เมื่อคุณยอมรับกฎ','ถัดไป','ย้อนกลับ','ฉันได้อ่านและยอมรับกฎ','ยืนยันสำเร็จ','ยังไม่มีคำแปล จะแสดงข้อความต้นฉบับ','โปรดอ่านกฎแล้วยอมรับ','แสดงคำแปลเท่านั้น ไม่มอบบทบาท'),
'Tiếng Việt':('Chọn ngôn ngữ','Chọn ngôn ngữ rồi đọc nội quy. Việc xác minh chỉ hoàn tất sau khi bạn đồng ý.','Tiếp theo','Quay lại','Tôi đã đọc và đồng ý với nội quy','Xác minh hoàn tất','Chưa có bản dịch; hiển thị nội dung gốc.','Đọc nội quy rồi đồng ý.','Chỉ xem bản dịch; không cấp vai trò.')}

def text(language):
    return dict(zip(KEYS,ROWS.get(language,ROWS['English'])))
